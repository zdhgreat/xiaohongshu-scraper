"""
小红书数据库查询工具 (只读)。

提供 CLI 访问 xhs_notes, xhs_users, xhs_comments 表。
严格只读 — 不执行 INSERT, UPDATE, DELETE 操作。

用法:
    python query_db.py notes [--user USER_ID] [--type TYPE] [--search KEYWORD]
                             [--since DATE] [--until DATE] [--limit N] [--offset N]
                             [--id NOTE_ID] [--full]
    python query_db.py users [--search KEYWORD]
    python query_db.py stats
"""

import os
import sys
import json
import argparse

# Fix Windows console encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv


# Load environment variables from .env (search upward)
_env_dir = os.path.dirname(__file__)
for _ in range(4):
    _env_path = os.path.join(_env_dir, ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path, encoding="utf-8")
        break
    _env_dir = os.path.dirname(_env_dir)


def get_db_connection():
    """Create a read-only database connection using .env config (readonly user)."""
    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_READONLY_USER", "hub_readonly"),
        password=os.getenv("POSTGRES_READONLY_PASSWORD", "hub_password"),
        dbname=os.getenv("POSTGRES_DB", "financial_hub"),
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


# ---------------------------------------------------------------------------
# Output formatting — stable structure for AI Agent consumption
# ---------------------------------------------------------------------------

ITEM_SEPARATOR = "\n" + "=" * 60 + "\n"


def format_note(row: dict, full: bool = False) -> str:
    """Format a single note record."""
    title = row.get("title") or "(无标题)"
    note_type = row.get("type") or "unknown"
    user_id = row.get("user_id") or ""
    pub_date = row.get("published_at") or ""
    note_url = row.get("note_url") or f"https://www.xiaohongshu.com/explore/{row.get('note_id', '')}"
    ip_location = row.get("ip_location") or ""
    liked = row.get("liked_count") or 0
    collected = row.get("collected_count") or 0
    comments = row.get("comment_count") or 0

    lines = [
        f"标题: {title}",
        f"来源: xiaohongshu",
        f"类型: {note_type}",
        f"作者ID: {user_id}",
        f"发布时间: {pub_date}",
        f"原始链接: {note_url}",
        f"IP属地: {ip_location}",
        f"点赞: {liked}  收藏: {collected}  评论: {comments}",
    ]

    # Topics
    topics = row.get("topics") or []
    if isinstance(topics, str):
        try:
            topics = json.loads(topics)
        except (json.JSONDecodeError, TypeError):
            topics = []
    if topics:
        lines.append(f"话题: {', '.join(str(t) for t in topics[:10])}")

    # Body text
    body = row.get("content") or row.get("description") or ""
    if full:
        lines.append("")
        lines.append("--- 正文 ---")
        lines.append(body if body else "(无正文)")
    else:
        preview = body[:200].replace("\n", " ") if body else "(无正文)"
        if len(body) > 200:
            preview += "..."
        lines.append(f"正文预览: {preview}")

    return "\n".join(lines)


def format_user(row: dict) -> str:
    """Format a single user record."""
    lines = [
        f"用户ID: {row.get('user_id')}",
        f"昵称: {row.get('nickname') or ''}",
        f"简介: {row.get('description') or ''}",
        f"粉丝: {row.get('fans_count') or 0}",
        f"关注: {row.get('follow_count') or 0}",
        f"笔记数: {row.get('notes_count') or 0}",
        f"地区: {row.get('location') or ''}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------

def cmd_notes(conn, args):
    """Query notes with optional filters."""
    conditions = []
    params = []

    if args.user:
        conditions.append("user_id = %s")
        params.append(args.user)

    if args.type:
        conditions.append("type = %s")
        params.append(args.type)

    if args.search:
        conditions.append("(content ILIKE %s OR title ILIKE %s OR description ILIKE %s)")
        pattern = f"%{args.search}%"
        params.extend([pattern, pattern, pattern])

    if args.since:
        conditions.append("published_at >= %s")
        params.append(args.since)

    if args.until:
        conditions.append("published_at <= %s")
        params.append(args.until)

    if args.id:
        conditions.append("note_id = %s")
        params.append(args.id)

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    limit = min(args.limit, 500)
    offset = args.offset

    sql = f"""
        SELECT *
        FROM xhs_notes
        {where}
        ORDER BY published_at DESC NULLS LAST
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    if not rows:
        print("没有找到匹配的笔记。")
        return

    # Count total
    count_sql = f"SELECT COUNT(*) FROM xhs_notes {where}"
    with conn.cursor() as cur:
        cur.execute(count_sql, params[:-2])
        total = cur.fetchone()[0]

    print(f"查询结果: {len(rows)} 条 (共 {total} 条匹配, offset={offset}, limit={limit})\n")
    print(ITEM_SEPARATOR.join(format_note(r, full=args.full) for r in rows))


def cmd_users(conn, args):
    """List users."""
    conditions = []
    params = []

    if args.search:
        conditions.append("(nickname ILIKE %s OR description ILIKE %s)")
        pattern = f"%{args.search}%"
        params.extend([pattern, pattern])

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    sql = f"SELECT * FROM xhs_users {where} ORDER BY fans_count DESC NULLS LAST LIMIT 100"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    if not rows:
        print("没有找到匹配的用户。")
        return

    print(f"共 {len(rows)} 位用户:\n")
    print(ITEM_SEPARATOR.join(format_user(r) for r in rows))


def cmd_stats(conn, args):
    """Show statistics overview."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM xhs_notes")
        note_count = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) AS cnt FROM xhs_users")
        user_count = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) AS cnt FROM xhs_comments")
        comment_count = cur.fetchone()["cnt"]

        cur.execute(
            "SELECT type, COUNT(*) AS cnt FROM xhs_notes GROUP BY type ORDER BY cnt DESC"
        )
        type_rows = cur.fetchall()

    lines = [
        "统计概览",
        f"笔记总数: {note_count}",
        f"用户总数: {user_count}",
        f"评论总数: {comment_count}",
        "",
        "按类型统计:",
    ]
    for r in type_rows:
        lines.append(f"  {r['type'] or '(未知)'}: {r['cnt']} 条")

    print("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="小红书数据库只读查询工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- notes ---
    p_notes = subparsers.add_parser("notes", help="查询笔记")
    p_notes.add_argument("--user", type=str, default=None, help="按用户ID过滤")
    p_notes.add_argument("--type", type=str, default=None, help="按类型过滤 (图文/video)")
    p_notes.add_argument("--search", type=str, default=None, help="按关键词搜索正文和标题")
    p_notes.add_argument("--since", type=str, default=None, help="起始日期 (含), 格式 YYYY-MM-DD")
    p_notes.add_argument("--until", type=str, default=None, help="截止日期 (含), 格式 YYYY-MM-DD")
    p_notes.add_argument("--limit", type=int, default=20, help="返回条数上限 (默认 20, 最大 500)")
    p_notes.add_argument("--offset", type=int, default=0, help="跳过前 N 条 (分页用)")
    p_notes.add_argument("--id", type=str, default=None, help="按笔记ID精确查询单条")
    p_notes.add_argument("--full", action="store_true", help="显示完整正文 (默认只显示预览)")

    # --- users ---
    p_users = subparsers.add_parser("users", help="列出用户")
    p_users.add_argument("--search", type=str, default=None, help="按昵称或简介搜索")

    # --- stats ---
    subparsers.add_parser("stats", help="查看统计信息")

    args = parser.parse_args()

    conn = get_db_connection()
    try:
        if args.command == "notes":
            cmd_notes(conn, args)
        elif args.command == "users":
            cmd_users(conn, args)
        elif args.command == "stats":
            cmd_stats(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
