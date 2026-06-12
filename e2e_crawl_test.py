"""End-to-end PG crawl test: fetch user + latest 4 notes, store in SQLite + PG."""
import sys, json, os
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent / 'scripts'))

# Fix Windows encoding
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import xhs_sign, xhs_config, xhs_accounts, xhs_storage, sqlite3
from curl_cffi.requests import Session
from xhs_api import _normalize_note, _normalize_user

mgr = xhs_accounts.AccountManager()
acc = mgr.get('default')
signer = xhs_sign.EmbedJsSigner()
session = Session(impersonate='chrome136')
session.headers.update(xhs_config.base_headers())
session.cookies.update(acc.cookies)

# === CONFIG ===
# Change this to the desired user ID (must be a valid 24-char hex XHS user_id)
# 11547643017 does NOT exist on XHS — use a valid user_id instead
TEST_USER_ID = '5b3b8eb1e8ac2b21af76f146'

def api_get(path, params, referer_suffix=''):
    full_api = path + '?' + '&'.join(f'{k}={v}' for k,v in params.items())
    headers = signer.sign(full_api, '', acc.cookies.get('a1',''), 'GET', platform='Windows')
    req_headers = {**session.headers, **headers}
    req_headers['referer'] = xhs_config.WEB_BASE + referer_suffix
    resp = session.get(xhs_config.BASE + full_api, headers=req_headers, timeout=20)
    return resp.json()

def safe_str(s, max_len=60):
    """Safely convert to string, replacing non-ASCII if needed."""
    if s is None:
        return '?'
    s = str(s)
    if len(s) > max_len:
        s = s[:max_len-3] + '...'
    return s

print('='*60)
print(f'END-TO-END PG CRAWL FOR USER: {TEST_USER_ID}')
print('='*60)

# ── Step 1: Fetch user info ──
print()
print('[Step 1/5] Fetching user info...')
info_resp = api_get('/api/sns/web/v1/user/otherinfo', {'target_user_id': TEST_USER_ID}, f'/user/profile/{TEST_USER_ID}')
code = info_resp.get('code')
if code != 0:
    print(f'  ERROR: API returned code={code}, msg={info_resp.get("msg","?")}')
    print(f'  This user ID may not exist on XHS. Please verify the user ID.')
    sys.exit(1)

basic = (info_resp.get('data') or {}).get('basic_info', {})
nickname = safe_str(basic.get('nickname', '?'))
fans = basic.get('fans', '?')
print(f'  Status: code=0 nickname={nickname} fans={fans}')

# ── Step 2: Fetch latest 4 notes ──
print()
print('[Step 2/5] Fetching latest 4 notes...')
notes_resp = api_get('/api/sns/web/v1/user_posted',
    {'num': '4', 'cursor': '', 'user_id': TEST_USER_ID, 'image_formats': 'jpg,webp,avif'},
    f'/user/profile/{TEST_USER_ID}')
notes = (notes_resp.get('data') or {}).get('notes', [])
print(f'  Notes fetched: {len(notes)}')
for i, n in enumerate(notes):
    note_id = n.get('note_id', '?')
    nc = n.get('note_card', {}) or {}
    title = safe_str(nc.get('display_title', n.get('display_title', '')) or '?')
    likes = (nc.get('interact_info', {}) or {}).get('liked_count', 0)
    ptype = 'video' if n.get('type') == 'video' else 'image'
    print(f'  [{i+1}] [{ptype}] {note_id} | {title} | likes={likes}')

# ── Step 3: Fetch note details ──
print()
print('[Step 3/5] Fetching note details...')
detailed_notes = []
for i, n in enumerate(notes):
    note_id = n.get('note_id', '?')
    xsec = n.get('xsec_token', '')
    detail_api = '/api/sns/web/v1/feed'
    detail_data = {'source_note_id': note_id, 'image_formats': ['jpg', 'webp', 'avif'],
                   'extra': {'need_body_topic': '1'}, 'xsec_source': 'pc_user', 'xsec_token': xsec}
    headers = signer.sign(detail_api, json.dumps(detail_data, separators=(',', ':'), ensure_ascii=False),
                         acc.cookies.get('a1',''), 'POST', platform='Windows')
    req_headers = {**session.headers, **headers}
    req_headers['referer'] = xhs_config.WEB_BASE + f'/explore/{note_id}'
    resp = session.post(xhs_config.BASE + detail_api, headers=req_headers,
                       data=json.dumps(detail_data, separators=(',', ':'), ensure_ascii=False).encode(), timeout=20)
    detail = resp.json()
    items = (detail.get('data') or {}).get('items', [])
    if items:
        detailed_notes.append(items[0])
        nc = items[0].get('note_card', {})
        desc = safe_str(nc.get('desc', '') or '', 50)
        print(f'  [{i+1}] {note_id} OK | {desc}')
    else:
        print(f'  [{i+1}] {note_id} EMPTY (deleted/private)')
        detailed_notes.append(n)

# ── Step 4: Store in SQLite ──
print()
print('[Step 4/5] Storing in SQLite...')
conn = sqlite3.connect(str(xhs_config.DB_PATH))
basic['user_id'] = TEST_USER_ID
user_norm = _normalize_user(basic)
xhs_storage.upsert_user(conn, user_norm)
print(f'  User upserted: {nickname} (id={TEST_USER_ID})')

stored = 0
for n in detailed_notes:
    note_norm = _normalize_note(n)
    if note_norm.get('note_id'):
        note_norm['user_id'] = TEST_USER_ID
        xhs_storage.upsert_note(conn, note_norm)
        stored += 1
        title = safe_str(note_norm.get('title', ''), 40)
        print(f'  Note upserted: {note_norm["note_id"]} | {title}')

conn.commit()
conn.close()
print(f'  Total: {stored} notes stored in SQLite ({xhs_config.DB_PATH})')

# ── Step 5: Sync to PostgreSQL ──
print()
print('[Step 5/5] Syncing SQLite -> PostgreSQL...')
import importlib.util
adapter_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hub_adapter.py')
spec = importlib.util.spec_from_file_location('hub_adapter', adapter_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
pg_conn = mod.get_pg_connection()
try:
    count = mod.sync_sqlite_to_pg(pg_conn)
    print(f'  PG sync OK: {count} total rows synchronized')
finally:
    pg_conn.close()

# ── Verify in PG ──
print()
print('='*60)
print('VERIFICATION: Querying PostgreSQL (financial_hub)')
print('='*60)
import psycopg2
vconn = psycopg2.connect(host='127.0.0.1', port=5432, user='postgres', password='postgres', dbname='financial_hub')
vcur = vconn.cursor()
vcur.execute(
    'SELECT note_id, title, type, liked_count, published_at FROM xhs_notes WHERE user_id=%s ORDER BY published_at DESC LIMIT 4',
    (TEST_USER_ID,))
for row in vcur.fetchall():
    ntype = row[2] or '?'
    nid = row[0]
    ntitle = safe_str(row[1] or '', 60)
    nlikes = row[3]
    ndate = row[4]
    print(f'  [{ntype}] {nid} | {ntitle} | likes={nlikes} | {ndate}')
vcur.close()
vconn.close()

print()
print('='*60)
print('END-TO-END PG CRAWL COMPLETE')
print(f'User: {nickname} ({TEST_USER_ID})')
print(f'Latest {stored} notes stored in:')
print(f'  - SQLite: {xhs_config.DB_PATH}')
print(f'  - PostgreSQL: financial_hub.xhs_notes')
print('='*60)
