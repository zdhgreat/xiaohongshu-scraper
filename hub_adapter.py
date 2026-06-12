"""小红书 Hub Adapter — Adapter B 模式
用法: python hub_adapter.py [--target-id ID]

功能:
1. 从 Hub crawl_targets 获取启用的 xiaohongshu 目标
2. subprocess 调用 xhs.py crawl-search/crawl-feed/crawl-user
3. 读取本地 SQLite (xhs.db) 同步到 Hub PG
4. 管理 Hub lifecycle (notify_start/end)

target_identifier 约定:
  search:KEYWORD  → xhs.py crawl-search --keyword KEYWORD
  user:USER_ID    → xhs.py crawl-user --user-id USER_ID
  feed            → xhs.py crawl-feed
"""
import json
import os
import re
import sqlite3
import sys
import time
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv
from financial_hub_postgres import FinancialHubClient

load_dotenv()

COMPONENT_NAME = "xiaohongshu_crawler"
SOURCE_TYPE = "xiaohongshu"

_ADAPTER_DIR = Path(__file__).resolve().parent
SKILL_DIR = _ADAPTER_DIR / "scripts"
SKILL_SCRIPT = SKILL_DIR / "xhs.py"
SCHEMA_PATH = _ADAPTER_DIR / "schema.sql"
DB_PATH = _ADAPTER_DIR / "data" / "xhs.db"  # xhs_config.ROOT / data / xhs.db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hub_xhs_adapter")


def get_pg_connection():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "hub_user"),
        password=os.getenv("POSTGRES_PASSWORD", "hub_password"),
        dbname=os.getenv("POSTGRES_DB", "financial_hub"),
    )


def init_schema(conn):
    if not SCHEMA_PATH.exists():
        return
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _pg_upsert(conn, table, data, conflict_cols):
    cols = list(data.keys())
    vals = [_pg_val(data[c]) for c in cols]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in conflict_cols)
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
           f"ON CONFLICT ({', '.join(conflict_cols)}) DO UPDATE SET {updates}")
    with conn.cursor() as cur:
        cur.execute(sql, vals)
    conn.commit()


def _pg_val(v):
    if isinstance(v, str):
        v = v.replace("\x00", "")
    if isinstance(v, (dict, list)):
        return Json(v)
    return v


def _sqlite_json(val, context: str = ""):
    """SQLite TEXT → PG JSONB。解析失败时记录警告而非静默丢弃。"""
    if val is None:
        return Json({})
    if isinstance(val, str):
        try:
            return Json(json.loads(val))
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("JSON 解析失败%s，数据将被替换为空对象: %s (原始值前80字: %.80s)",
                           f" ({context})" if context else "", e, val)
            return Json({})
    return Json(val)


def _extract_from_raw(raw_json_str):
    """从 raw_json 中提取增强字段（参考 by_luzhe/substack 的字段设计）。

    提取：
    - content: 正文（raw_json.desc，比 description 更完整）
    - image_urls: 图片 URL 列表（从 image_list 提取）
    - note_url: 笔记原始链接
    - video_url: 如果 SQLite 字段为空，从 raw_json 提取
    """
    result = {}
    if not raw_json_str:
        return result

    try:
        raw = json.loads(raw_json_str) if isinstance(raw_json_str, str) else raw_json_str
    except (json.JSONDecodeError, TypeError):
        return result

    # 正文：优先用 raw_json.desc
    desc = raw.get("desc", "")
    if desc:
        result["content"] = desc

    # 笔记链接
    note_id = raw.get("note_id", "")
    if note_id:
        result["note_url"] = f"https://www.xiaohongshu.com/explore/{note_id}"

    # 图片 URL 列表
    image_urls = []
    for img in raw.get("image_list", []):
        url = img.get("url_default") or img.get("url") or ""
        if not url:
            for info in img.get("info_list", []):
                if info.get("image_scene") in ("WB_DFT", "WB_PRV"):
                    url = info.get("url", "")
                    break
        if url:
            image_urls.append(url)
    if image_urls:
        result["image_urls"] = image_urls

    # 视频链接（fallback）
    video = raw.get("video", {})
    media = video.get("media", {})
    stream = media.get("stream", {})
    for codec in ("h264", "h265", "av1"):
        items = stream.get(codec, [])
        if items:
            url = items[0].get("master_url") or ""
            if url:
                result["video_url_fallback"] = url
                break

    return result


_METRICS_RE = re.compile(r'__CRAWL_METRICS__\s*:\s*(\{.*\})')


def _parse_metrics(stdout):
    """从子进程 stdout 解析最后一行 __CRAWL_METRICS__，返回 (found, new, failed)。
    找不到/解析失败返回 (0, 0, 0)（容错：FatalRiskError 分支不打印该行）。"""
    if not stdout:
        return 0, 0, 0
    matches = _METRICS_RE.findall(stdout)
    if not matches:
        return 0, 0, 0
    try:
        d = json.loads(matches[-1])
        return (int(d.get("items_found", 0)),
                int(d.get("items_new", 0)),
                int(d.get("items_failed", 0)))
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0, 0, 0


def _scan_media_dirs(sq_conn):
    """对每条笔记正向计算预期媒体目录，检查存在性 → note_id → (相对路径, 是否有文件)。

    用 xhs_config.note_media_dir（与写入时完全一致的 sanitize 口径）算目录，
    避免旧实现用原始 title 反推导致的 sanitize/截断/同名标题匹配失败。
    """
    # xhs_config 位于 scripts/，不在 hub_adapter 默认搜索路径，动态加载
    import sys as _sys
    if str(SKILL_DIR) not in _sys.path:
        _sys.path.insert(0, str(SKILL_DIR))
    try:
        import xhs_config
    except Exception:
        return {}

    data_root = _ADAPTER_DIR / "data"
    mapping = {}
    try:
        note_ids = [r["note_id"] for r in sq_conn.execute("SELECT note_id FROM notes")]
    except Exception:
        return mapping

    for note_id in note_ids:
        # note_media_dir 按 sanitize(博主)/sanitize(标题) 算目录；查不到 title 时 fallback media/<note_id>/
        expected = xhs_config.note_media_dir(note_id, sq_conn)
        has_files = expected.exists() and expected.is_dir() and any(expected.iterdir())
        if not has_files:
            mapping[note_id] = ("", False)
            continue
        try:
            base = data_root if expected.is_relative_to(data_root) else _ADAPTER_DIR
            rel_path = str(expected.relative_to(base)).replace("\\", "/")
        except Exception:
            rel_path = ""
        mapping[note_id] = (rel_path, True)
    return mapping


# ── SQLite → PG 同步 ──

def sync_sqlite_to_pg(pg_conn):
    """读取 xhs SQLite 数据库，同步到 Hub PG。"""
    db_path = DB_PATH
    if not db_path.exists():
        logger.warning("  未找到 xhs.db")
        return

    sq = sqlite3.connect(str(db_path))
    sq.row_factory = sqlite3.Row
    # 与 xhs_storage.py 保持一致：WAL 模式 + busy_timeout
    sq.execute("PRAGMA journal_mode=WAL")
    sq.execute("PRAGMA busy_timeout=30000")
    # 确保 xhs.py 子进程的 WAL 已刷入主库
    sq.execute("PRAGMA wal_checkpoint(FULL)")

    # 预扫描 media 目录（传入 SQLite 连接用于 title→note_id 匹配）
    media_map = _scan_media_dirs(sq)

    now = datetime.now(timezone.utc)
    synced = 0

    # 同步 notes
    for row in sq.execute("SELECT * FROM notes"):
        # 从 raw_json 提取增强字段
        enriched = _extract_from_raw(row["raw_json"])

        video_url = row["video_url"] or enriched.get("video_url_fallback", "")
        _media = media_map.get(row["note_id"], ("", False))

        _pg_upsert(pg_conn, "xhs_notes", {
            "note_id": row["note_id"],
            "user_id": row["user_id"] or "",
            "title": row["title"] or "",
            "description": row["description"] or "",
            "content": enriched.get("content", row["description"] or ""),
            "type": row["type"] or "",
            "liked_count": row["liked_count"] or 0,
            "collected_count": row["collected_count"] or 0,
            "comment_count": row["comment_count"] or 0,
            "share_count": row["share_count"] or 0,
            "ip_location": row["ip_location"] or "",
            "topics": _sqlite_json(row["topics"]),
            "published_at": row["published_at"] or "",
            "xsec_token": row["xsec_token"] or "",
            "xsec_source": row["xsec_source"] or "",
            "video_url": video_url,
            "cover_url": row["cover_url"] or "",
            "video_duration": row["video_duration"] or 0,
            "video_transcript": row["video_transcript"] or "",
            "video_ocr_text": row["video_ocr_text"] or "",
            "video_summary": row["video_summary"] or "",
            "image_ocr_text": row["image_ocr_text"] or "",
            "image_summary": row["image_summary"] or "",
            "image_mermaid": row["image_mermaid"] or "",
            "image_urls": Json(enriched.get("image_urls", [])),
            "note_url": enriched.get("note_url", f"https://www.xiaohongshu.com/explore/{row['note_id']}"),
            "content_hash": row["content_hash"] or "",
            "raw_data": _sqlite_json(row["raw_json"], context=f"note {row['note_id']}"),
            "status": "ready",
            "media_path": _media[0],
            "has_local_media": _media[1],
            "updated_at": now,
            "crawled_at": row["crawled_at"] or now,
        }, ["note_id"])
        synced += 1

    # 同步 users
    for row in sq.execute("SELECT * FROM users"):
        _pg_upsert(pg_conn, "xhs_users", {
            "user_id": row["user_id"],
            "nickname": row["nickname"] or "",
            "avatar": row["avatar"] or "",
            "description": row["description"] or "",
            "fans_count": row["fans_count"] or 0,
            "follow_count": row["follow_count"] or 0,
            "notes_count": row["notes_count"] or 0,
            "location": row["location"] or "",
            "raw_data": _sqlite_json(row["raw_json"], context=f"user {row['user_id']}"),
            "status": "ready",
            "updated_at": now,
            "crawled_at": row["crawled_at"] or now,
        }, ["user_id"])
        synced += 1

    # 同步 comments
    for row in sq.execute("SELECT * FROM comments"):
        _pg_upsert(pg_conn, "xhs_comments", {
            "comment_id": row["comment_id"],
            "note_id": row["note_id"] or "",
            "parent_id": row["parent_id"] or "",
            "user_id": row["user_id"] or "",
            "nickname": row["nickname"] or "",
            "content": row["content"] or "",
            "like_count": row["like_count"] or 0,
            "ip_location": row["ip_location"] or "",
            "pictures": _sqlite_json(row["pictures_json"], context=f"comment {row['comment_id']}"),
            "target_comment_id": row["target_comment_id"] or "",
            "created_at": row["created_at"] or "",
            "raw_data": _sqlite_json(row["raw_json"], context=f"comment {row['comment_id']}"),
            "status": "ready",
            "updated_at": now,
            "crawled_at": row["crawled_at"] or now,
        }, ["comment_id"])
        synced += 1

    # 同步 search_cache
    for row in sq.execute("SELECT * FROM search_cache"):
        _pg_upsert(pg_conn, "xhs_search_cache", {
            "keyword": row["keyword"],
            "page": row["page"],
            "note_ids": _sqlite_json(row["note_ids_json"]),
        }, ["keyword", "page"])
        synced += 1

    # 同步 crawl_state
    for row in sq.execute("SELECT * FROM crawl_state"):
        _pg_upsert(pg_conn, "xhs_crawl_state", {
            "task_id": row["task_id"],
            "task_type": row["task_type"] or "",
            "target_id": row["target_id"] or "",
            "cursor": row["cursor"] or "",
            "status": row["status"] or "",
            "last_error": row["last_error"] or "",
        }, ["task_id"])
        synced += 1

    sq.close()
    logger.info(f"  同步 {synced} rows from SQLite to PG")
    return synced


# ── 构建命令 ──

def build_crawl_cmd(target_identifier):
    """根据 target_identifier 构建子进程命令。"""
    if target_identifier.startswith("search:"):
        keyword = target_identifier[len("search:"):]
        return [sys.executable, str(SKILL_SCRIPT), "crawl-search", keyword,
                "--max-pages", "1", "--no-download", "--no-analyze"]
    elif target_identifier.startswith("user:"):
        user_id = target_identifier[len("user:"):]
        return [sys.executable, str(SKILL_SCRIPT), "crawl-user", user_id,
                "--max-pages", "1", "--no-download", "--no-analyze"]
    else:
        return [sys.executable, str(SKILL_SCRIPT), "crawl-feed"]


# ── 双向同步：Hub → 本地 ──

def sync_hub_targets_to_local(targets):
    """从 Hub 读取目标，将 target_identifier 写入本地 SQLite crawl_state 表。"""
    if not DB_PATH.exists():
        return
    sq = sqlite3.connect(str(DB_PATH))
    try:
        for t in targets:
            ident = t.target_identifier
            # 构建 task_id 格式与 xhs.py 一致
            if ident.startswith("search:"):
                task_id = f"search:{ident[len('search:'):]}:"
            elif ident.startswith("user:"):
                task_id = f"user:{ident[len('user:'):]}:"
            else:
                task_id = f"feed:{ident}:"
            # 仅插入不存在的记录（不覆盖已有状态）
            existing = sq.execute("SELECT 1 FROM crawl_state WHERE task_id LIKE ?",
                                  (f"{task_id}%",)).fetchone()
            if not existing:
                sq.execute(
                    "INSERT OR IGNORE INTO crawl_state (task_id, task_type, target_id, cursor, status) "
                    "VALUES (?, ?, ?, '', 'pending')",
                    (task_id + "hub", ident.split(":")[0] if ":" in ident else "feed", ident),
                )
        sq.commit()
        logger.info(f"  同步 {len(targets)} 个 Hub 目标到本地 SQLite")
    except Exception as e:
        logger.warning(f"  Hub→本地同步失败: {e}")
    finally:
        sq.close()


# ── 主流程 ──

def run_single(target, client, hub_conn):
    target_identifier = target.target_identifier
    target_name = target.target_name

    run = client.notify_crawl_start(
        target_id=target.id, component_name=COMPONENT_NAME,
        metadata={"trigger": "hub_adapter", "target": target_identifier},
    )
    logger.info(f"  开始: {target_name} ({target_identifier}) run_id={run.id}")

    cmd = build_crawl_cmd(target_identifier)
    start_time = time.time()
    success = False
    error_msg = None
    items_found = items_new = items_failed = 0

    # 强制子进程使用 DOM 搜索模式（降低风控风险）
    child_env = os.environ.copy()
    child_env["XHS_SEARCH_MODE"] = "dom"

    try:
        result = subprocess.run(cmd, check=True, capture_output=True,
                                cwd=str(SKILL_DIR), encoding="utf-8", errors="replace",
                                env=child_env)
        success = result.returncode == 0
        items_found, items_new, items_failed = _parse_metrics(result.stdout)
        if not success:
            error_msg = f"xhs.py 返回非零: {result.returncode}"
    except subprocess.CalledProcessError as e:
        error_msg = f"子进程错误: {str(e)[:200]}"
        items_found, items_new, items_failed = _parse_metrics(e.output)
    except Exception as e:
        error_msg = f"异常: {str(e)[:200]}"

    duration_ms = int((time.time() - start_time) * 1000)

    # 同步数据
    if success:
        try:
            sync_sqlite_to_pg(hub_conn)
        except Exception as e:
            logger.error(f"  数据同步失败: {e}")

    client.notify_crawl_end(
        run_id=run.id, target_id=target.id,
        component_name=COMPONENT_NAME,
        success=success,
        error_message=(error_msg or "")[:500] or None,
        duration_ms=duration_ms,
        items_found=items_found,
        items_new=items_new,
        items_failed=items_failed,
    )
    logger.info(f"  {'OK' if success else 'FAIL'}: {target_name} ({duration_ms}ms)")


def main():
    hub_conn = get_pg_connection()
    try:
        init_schema(hub_conn)
        client = FinancialHubClient(hub_conn)

        logger.info(f"扫描 Hub 中 {SOURCE_TYPE} 目标...")
        targets = client.get_crawl_targets(source_type=SOURCE_TYPE, enabled=True)
        if not targets:
            logger.info("没有启用的 xiaohongshu 目标")
            return

        for t in targets:
            logger.info(f"  [{t.id}] {t.target_name} -> {t.target_identifier}")

        # 双向同步：Hub 目标 → 本地 SQLite
        sync_hub_targets_to_local(targets)

        target_id = None
        for arg in sys.argv[1:]:
            if arg.startswith("--target-id="):
                target_id = int(arg.split("=")[1])
                break

        for target in targets:
            if target_id and target.id != target_id:
                continue
            run_single(target, client, hub_conn)
    finally:
        hub_conn.close()


if __name__ == "__main__":
    main()
