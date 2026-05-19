"""SQLite + Markdown/CSV 渲染。

MVP 用 4 张表：notes / users / search_cache / crawl_state。
评论、图片、视频本地化延后到 P2。
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from xhs_config import DB_PATH, MEDIA_DIR, OUTPUT_DIR, note_media_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    note_id TEXT PRIMARY KEY,
    user_id TEXT,
    title TEXT,
    description TEXT,
    type TEXT,
    liked_count INTEGER DEFAULT 0,
    collected_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    ip_location TEXT,
    topics TEXT,
    published_at TEXT,
    xsec_token TEXT DEFAULT '',
    xsec_source TEXT DEFAULT '',
    video_url TEXT DEFAULT '',
    cover_url TEXT DEFAULT '',
    video_duration INTEGER DEFAULT 0,
    video_transcript TEXT DEFAULT '',
    video_ocr_text TEXT DEFAULT '',
    video_summary TEXT DEFAULT '',
    image_ocr_text TEXT DEFAULT '',
    image_summary TEXT DEFAULT '',
    image_mermaid TEXT DEFAULT '',
    raw_json TEXT,
    crawled_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    nickname TEXT,
    avatar TEXT,
    description TEXT,
    fans_count INTEGER DEFAULT 0,
    follow_count INTEGER DEFAULT 0,
    notes_count INTEGER DEFAULT 0,
    location TEXT,
    raw_json TEXT,
    crawled_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS search_cache (
    keyword TEXT,
    page INTEGER,
    note_ids_json TEXT,
    crawled_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (keyword, page)
);

CREATE TABLE IF NOT EXISTS crawl_state (
    task_id TEXT PRIMARY KEY,
    task_type TEXT,
    target_id TEXT,
    cursor TEXT,
    status TEXT,
    last_error TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS comments (
    comment_id TEXT PRIMARY KEY,
    note_id TEXT,
    parent_id TEXT DEFAULT '',
    user_id TEXT,
    nickname TEXT,
    content TEXT,
    like_count INTEGER DEFAULT 0,
    ip_location TEXT,
    pictures_json TEXT DEFAULT '[]',
    target_comment_id TEXT DEFAULT '',
    created_at TEXT,
    raw_json TEXT,
    crawled_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id);
CREATE INDEX IF NOT EXISTS idx_notes_crawled ON notes(crawled_at);
CREATE INDEX IF NOT EXISTS idx_comments_note ON comments(note_id);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """无损添加新字段，老 db 也兼容。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notes)").fetchall()}
    if "xsec_token" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN xsec_token TEXT DEFAULT ''")
    if "xsec_source" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN xsec_source TEXT DEFAULT ''")
    if "video_url" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN video_url TEXT DEFAULT ''")
    if "cover_url" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN cover_url TEXT DEFAULT ''")
    if "video_duration" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN video_duration INTEGER DEFAULT 0")
    if "video_transcript" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN video_transcript TEXT DEFAULT ''")
    if "video_ocr_text" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN video_ocr_text TEXT DEFAULT ''")
    if "video_summary" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN video_summary TEXT DEFAULT ''")
    if "image_ocr_text" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN image_ocr_text TEXT DEFAULT ''")
    if "image_summary" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN image_summary TEXT DEFAULT ''")
    if "image_mermaid" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN image_mermaid TEXT DEFAULT ''")
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN content_hash TEXT DEFAULT ''")
    conn.commit()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL 模式允许读写并发，大幅减少 "database is locked"
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _create_in_memory() -> sqlite3.Connection:
    """创建内存 SQLite 数据库（用于测试）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def upsert_note(conn: sqlite3.Connection, note: dict[str, Any]) -> None:
    content_hash = note.get("content_hash", "")
    # 增量跳过：如果 content_hash 相同则不更新
    if content_hash:
        existing = conn.execute(
            "SELECT content_hash FROM notes WHERE note_id = ?",
            (note.get("note_id"),),
        ).fetchone()
        if existing and existing["content_hash"] == content_hash:
            return  # 无变更，跳过

    cols = (
        "note_id user_id title description type liked_count collected_count "
        "comment_count share_count ip_location topics published_at "
        "xsec_token xsec_source video_url cover_url video_duration "
        "video_transcript video_ocr_text video_summary "
        "image_ocr_text image_summary image_mermaid raw_json content_hash"
    ).split()
    values = [
        note.get("note_id"),
        note.get("user_id"),
        note.get("title", ""),
        note.get("description", ""),
        note.get("type", "note"),
        int(note.get("liked_count", 0) or 0),
        int(note.get("collected_count", 0) or 0),
        int(note.get("comment_count", 0) or 0),
        int(note.get("share_count", 0) or 0),
        note.get("ip_location", ""),
        json.dumps(note.get("topics", []), ensure_ascii=False),
        note.get("published_at", ""),
        note.get("xsec_token", ""),
        note.get("xsec_source", ""),
        note.get("video_url", ""),
        note.get("cover_url", ""),
        int(note.get("video_duration", 0) or 0),
        note.get("video_transcript", ""),
        note.get("video_ocr_text", ""),
        note.get("video_summary", ""),
        note.get("image_ocr_text", ""),
        note.get("image_summary", ""),
        note.get("image_mermaid", ""),
        json.dumps(note.get("raw", {}), ensure_ascii=False),
        content_hash,
    ]
    placeholders = ",".join("?" * len(cols))
    # 用 INSERT...ON CONFLICT，避免 INSERT OR REPLACE 把已有字段清成空
    conn.execute(
        f"INSERT INTO notes ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(note_id) DO UPDATE SET "
        f"user_id=excluded.user_id, title=excluded.title, description=excluded.description, "
        f"type=excluded.type, liked_count=excluded.liked_count, collected_count=excluded.collected_count, "
        f"comment_count=excluded.comment_count, share_count=excluded.share_count, "
        f"ip_location=excluded.ip_location, topics=excluded.topics, published_at=excluded.published_at, "
        f"xsec_token=CASE WHEN excluded.xsec_token != '' THEN excluded.xsec_token ELSE notes.xsec_token END, "
        f"xsec_source=CASE WHEN excluded.xsec_source != '' THEN excluded.xsec_source ELSE notes.xsec_source END, "
        f"video_url=CASE WHEN excluded.video_url != '' THEN excluded.video_url ELSE notes.video_url END, "
        f"cover_url=CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE notes.cover_url END, "
        f"video_duration=CASE WHEN excluded.video_duration > 0 THEN excluded.video_duration ELSE notes.video_duration END, "
        f"video_transcript=CASE WHEN excluded.video_transcript != '' THEN excluded.video_transcript ELSE notes.video_transcript END, "
        f"video_ocr_text=CASE WHEN excluded.video_ocr_text != '' THEN excluded.video_ocr_text ELSE notes.video_ocr_text END, "
        f"video_summary=CASE WHEN excluded.video_summary != '' THEN excluded.video_summary ELSE notes.video_summary END, "
        f"image_ocr_text=CASE WHEN excluded.image_ocr_text != '' THEN excluded.image_ocr_text ELSE notes.image_ocr_text END, "
        f"image_summary=CASE WHEN excluded.image_summary != '' THEN excluded.image_summary ELSE notes.image_summary END, "
        f"image_mermaid=CASE WHEN excluded.image_mermaid != '' THEN excluded.image_mermaid ELSE notes.image_mermaid END, "
        f"raw_json=excluded.raw_json, content_hash=excluded.content_hash, crawled_at=CURRENT_TIMESTAMP",
        values,
    )


def update_video_analysis(
    conn: sqlite3.Connection,
    note_id: str,
    transcript: str = "",
    ocr_text: str = "",
    summary: str = "",
) -> None:
    """更新视频分析结果（转录 / OCR / 摘要），不影响其他字段。"""
    conn.execute(
        "UPDATE notes SET video_transcript=?, video_ocr_text=?, video_summary=? "
        "WHERE note_id=?",
        (transcript, ocr_text, summary, note_id),
    )
    conn.commit()


def update_image_analysis(
    conn: sqlite3.Connection,
    note_id: str,
    ocr_text: str = "",
    summary: str = "",
    mermaid: str = "",
) -> None:
    """更新图片分析结果（OCR / AI 描述 / Mermaid），不影响其他字段。"""
    conn.execute(
        "UPDATE notes SET image_ocr_text=?, image_summary=?, image_mermaid=? "
        "WHERE note_id=?",
        (ocr_text, summary, mermaid, note_id),
    )
    conn.commit()


def upsert_user(conn: sqlite3.Connection, user: dict[str, Any]) -> None:
    user_id = user.get("user_id")
    if not user_id:
        return
    conn.execute(
        """INSERT INTO users (user_id, nickname, avatar, description,
                              fans_count, follow_count, notes_count, location, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               nickname = COALESCE(NULLIF(excluded.nickname, ''), users.nickname),
               avatar = COALESCE(NULLIF(excluded.avatar, ''), users.avatar),
               description = COALESCE(NULLIF(excluded.description, ''), users.description),
               fans_count = CASE WHEN excluded.fans_count > 0 THEN excluded.fans_count ELSE users.fans_count END,
               follow_count = CASE WHEN excluded.follow_count > 0 THEN excluded.follow_count ELSE users.follow_count END,
               notes_count = CASE WHEN excluded.notes_count > 0 THEN excluded.notes_count ELSE users.notes_count END,
               location = COALESCE(NULLIF(excluded.location, ''), users.location),
               raw_json = excluded.raw_json
        """,
        (
            user_id,
            user.get("nickname", ""),
            user.get("avatar", ""),
            user.get("description", ""),
            int(user.get("fans_count", 0) or 0),
            int(user.get("follow_count", 0) or 0),
            int(user.get("notes_count", 0) or 0),
            user.get("location", ""),
            json.dumps(user.get("raw", {}), ensure_ascii=False),
        ),
    )


def save_search_page(
    conn: sqlite3.Connection, keyword: str, page: int, note_ids: list[str]
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO search_cache (keyword, page, note_ids_json) VALUES (?, ?, ?)",
        (keyword, page, json.dumps(note_ids, ensure_ascii=False)),
    )


def update_crawl_state(
    conn: sqlite3.Connection,
    task_id: str,
    task_type: str,
    target_id: str,
    cursor: str,
    status: str,
    last_error: str = "",
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO crawl_state
           (task_id, task_type, target_id, cursor, status, last_error, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (task_id, task_type, target_id, cursor, status, last_error,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_crawl_state(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM crawl_state WHERE task_id = ?", (task_id,))
    return cur.fetchone()


def get_note(conn: sqlite3.Connection, note_id: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,))
    return cur.fetchone()


def get_user(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return cur.fetchone()


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

def upsert_comment(conn: sqlite3.Connection, comment: dict[str, Any]) -> None:
    comment_id = comment.get("comment_id")
    if not comment_id:
        return
    conn.execute(
        """INSERT INTO comments (comment_id, note_id, parent_id, user_id, nickname,
                                 content, like_count, ip_location, pictures_json,
                                 target_comment_id, created_at, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(comment_id) DO UPDATE SET
               content = excluded.content,
               like_count = CASE WHEN excluded.like_count > 0 THEN excluded.like_count ELSE comments.like_count END,
               ip_location = COALESCE(NULLIF(excluded.ip_location, ''), comments.ip_location),
               pictures_json = excluded.pictures_json,
               raw_json = excluded.raw_json
        """,
        (
            comment_id,
            comment.get("note_id"),
            comment.get("parent_id", ""),
            comment.get("user_id", ""),
            comment.get("nickname", ""),
            comment.get("content", ""),
            int(comment.get("like_count", 0) or 0),
            comment.get("ip_location", ""),
            json.dumps(comment.get("pictures", []), ensure_ascii=False),
            comment.get("target_comment_id", ""),
            comment.get("created_at", ""),
            json.dumps(comment.get("raw", {}), ensure_ascii=False),
        ),
    )


def iter_comments(conn: sqlite3.Connection, note_id: str) -> Iterable[sqlite3.Row]:
    """主评论按 like_count desc，子评论挂在主评论下面（按 created_at asc）"""
    main = conn.execute(
        "SELECT * FROM comments WHERE note_id = ? AND parent_id = '' "
        "ORDER BY like_count DESC, created_at ASC",
        (note_id,),
    ).fetchall()
    for m in main:
        yield m
        subs = conn.execute(
            "SELECT * FROM comments WHERE note_id = ? AND parent_id = ? "
            "ORDER BY created_at ASC",
            (note_id, m["comment_id"]),
        ).fetchall()
        yield from subs


def count_comments(conn: sqlite3.Connection, note_id: str) -> tuple[int, int]:
    """返回 (主评论数, 子评论数)"""
    main = conn.execute("SELECT COUNT(*) FROM comments WHERE note_id = ? AND parent_id = ''", (note_id,)).fetchone()[0]
    sub = conn.execute("SELECT COUNT(*) FROM comments WHERE note_id = ? AND parent_id != ''", (note_id,)).fetchone()[0]
    return main, sub


def iter_notes(
    conn: sqlite3.Connection, user_id: str | None = None
) -> Iterable[sqlite3.Row]:
    if user_id:
        cur = conn.execute(
            "SELECT * FROM notes WHERE user_id = ? ORDER BY published_at DESC",
            (user_id,),
        )
    else:
        cur = conn.execute("SELECT * FROM notes ORDER BY published_at DESC")
    yield from cur


def render_markdown(conn: sqlite3.Connection, note_id: str) -> str:
    note = get_note(conn, note_id)
    if not note:
        raise FileNotFoundError(f"note {note_id} not in DB")
    user = get_user(conn, note["user_id"]) if note["user_id"] else None
    raw = json.loads(note["raw_json"] or "{}")

    nickname = (user["nickname"] if user else "") or raw.get("user", {}).get("nickname", "")
    topics = json.loads(note["topics"] or "[]")
    topic_str = " ".join(f"#{t}" for t in topics) if topics else "—"

    # 优先使用本地化媒体，否则用远程 URL
    # 查找本地文件（兼容新旧两种目录结构）
    media_dir = _find_media_dir(note_id, conn)
    local_images = sorted(media_dir.glob("img_*")) if media_dir.exists() else []
    local_video = next(iter(media_dir.glob("video.*")), None) if media_dir.exists() else None
    if local_images:
        images = [str(Path("..") / media_dir.relative_to(OUTPUT_DIR.parent) / p.name).replace("\\", "/") for p in local_images]
    else:
        images = _extract_images(raw)
    if local_video:
        video_url = str(Path("..") / media_dir.relative_to(OUTPUT_DIR.parent) / local_video.name).replace("\\", "/")
    else:
        video_url = _extract_video_url(raw)

    lines = [
        f"# {note['title'] or '(无标题)'}",
        "",
        f"- **作者**: {nickname} (@{note['user_id']})",
        f"- **发布时间**: {note['published_at'] or '—'}",
        f"- **IP属地**: {note['ip_location'] or '—'}",
        f"- **互动**: 赞 {note['liked_count']} | 藏 {note['collected_count']} | 评 {note['comment_count']} | 分享 {note['share_count']}",
        f"- **话题**: {topic_str}",
        f"- **类型**: {note['type']}",
        f"- **笔记链接**: https://www.xiaohongshu.com/explore/{note['note_id']}",
        "",
        "## 正文",
        "",
        note["description"] or "(无正文)",
        "",
    ]
    if images:
        lines.append("## 图片")
        lines.append("")
        md_title = (note['title'] or '(无标题)').replace('[', '(').replace(']', ')')
        for i, url in enumerate(images, 1):
            lines.append(f"![{md_title}·图{i}]({url})")
        lines.append("")

    # 图片分析结果
    image_summary = note["image_summary"] or ""
    image_ocr = note["image_ocr_text"] or ""
    image_mermaid = note["image_mermaid"] or ""
    if image_summary or image_ocr or image_mermaid:
        lines.append("### 图片分析")
        lines.append("")
        if image_summary:
            lines.append("#### AI 描述")
            lines.append("")
            lines.append(image_summary)
            lines.append("")
        if image_ocr:
            lines.append("#### 图片文字")
            lines.append("")
            lines.append(image_ocr)
            lines.append("")
        if image_mermaid:
            lines.append("#### 路线图 / 流程图")
            lines.append("")
            lines.append("```mermaid")
            lines.append(image_mermaid)
            lines.append("```")
            lines.append("")
    if video_url:
        lines.append("## 视频")
        lines.append("")
        # 封面图
        cover = note["cover_url"] or _extract_cover_url(raw) or ""
        if cover:
            # 优先本地封面
            local_cover = next(iter(media_dir.glob("cover.*")), None) if media_dir.exists() else None
            if local_cover:
                cover_ref = str(Path("..") / media_dir.relative_to(OUTPUT_DIR.parent) / local_cover.name).replace("\\", "/")
            else:
                cover_ref = cover
            lines.append(f"![封面]({cover_ref})")
            lines.append("")
        # 时长
        duration = note["video_duration"] or 0
        if duration:
            mins, secs = divmod(duration, 60)
            lines.append(f"- **时长**: {mins:02d}:{secs:02d}")
        lines.append(f"[视频链接]({video_url})")
        # 本地视频文件
        if local_video:
            lines.append(f"- **本地文件**: `{local_video.name}`")
        lines.append("")
        # 视频分析结果
        transcript = note["video_transcript"] or ""
        ocr_text = note["video_ocr_text"] or ""
        summary = note["video_summary"] or ""
        if summary:
            lines.append("### 视频摘要")
            lines.append("")
            lines.append(summary)
            lines.append("")
        if transcript:
            lines.append("### 语音转录")
            lines.append("")
            lines.append(transcript)
            lines.append("")
        if ocr_text:
            lines.append("### 画面文字")
            lines.append("")
            lines.append(ocr_text)
            lines.append("")

    # 评论区
    main_n, sub_n = count_comments(conn, note_id)
    if main_n:
        lines.append(f"## 评论 ({main_n} 主 + {sub_n} 回复)")
        lines.append("")
        for c in iter_comments(conn, note_id):
            indent = "" if c["parent_id"] == "" else "  "
            head = f"**{c['nickname']}** ({c['ip_location'] or '?'}, 赞 {c['like_count']})"
            lines.append(f"{indent}- {head}: {c['content']}")
        lines.append("")
    return "\n".join(lines)


def write_markdown(conn: sqlite3.Connection, note_id: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = _human_filename(conn, note_id, ".md")
    path = OUTPUT_DIR / filename
    path.write_text(render_markdown(conn, note_id), encoding="utf-8")
    return path


def _human_filename(conn: sqlite3.Connection, note_id: str, ext: str) -> str:
    """生成人类可读的文件名：{note_id}_{作者}_{标题前50字}.ext

    标题去除特殊字符，空格替换为下划线。
    无标题时退化到 {note_id}_{描述前50字}.ext。
    """
    row = get_note(conn, note_id)
    if not row:
        return f"{note_id}{ext}"
    title = row["title"] or ""
    if not title:
        desc = row["description"] or ""
        title = desc[:50] if desc else ""
    # 清理标题：去掉文件名不合法字符
    title = re.sub(r'[<>:"/\\|?*\n\r\t]', '', title)
    title = title.replace(' ', '_')
    title = title[:50]  # 截断
    if not title:
        return f"{note_id}{ext}"
    # 加博主名
    author = ""
    user = get_user(conn, row["user_id"]) if row["user_id"] else None
    if user and user["nickname"]:
        author = re.sub(r'[<>:"/\\|?*\n\r\t]', '', user["nickname"]).replace(' ', '_')[:30]
    if author:
        return f"{note_id}_{author}_{title}{ext}"
    return f"{note_id}_{title}{ext}"


def render_update_summary(conn: sqlite3.Connection, note_id: str) -> str:
    """生成笔记当前状态摘要，用于更新通知。"""
    row = get_note(conn, note_id)
    if not row:
        return ""
    parts = []
    title = row["title"] or "(无标题)"

    # 基本信息
    type_label = "视频" if row["type"] == "video" else "图文"
    parts.append(f"《{title}》({type_label})")

    # 媒体状态
    media_dir = _find_media_dir(note_id, conn)
    local_imgs = len(list(media_dir.glob("img_*"))) if media_dir.exists() else 0
    local_video = next(iter(media_dir.glob("video.*")), None) is not None if media_dir.exists() else False
    media_items = []
    if local_imgs:
        media_items.append(f"{local_imgs} 张图片")
    if local_video:
        media_items.append("1 个视频")
    if media_items:
        parts.append("已下载: " + "、".join(media_items))

    # 分析状态
    analysis_items = []
    if row["video_transcript"]:
        analysis_items.append(f"语音转录 {len(row['video_transcript'])} 字")
    if row["video_ocr_text"]:
        analysis_items.append(f"画面文字 {len(row['video_ocr_text'])} 字")
    if row["video_summary"]:
        analysis_items.append("AI 摘要")
    if analysis_items:
        parts.append("视频分析: " + "、".join(analysis_items))

    # 图片分析状态
    image_items = []
    if row["image_ocr_text"]:
        image_items.append(f"图片OCR {len(row['image_ocr_text'])} 字")
    if row["image_summary"]:
        image_items.append("图片AI描述")
    if row["image_mermaid"]:
        image_items.append("路线图/流程图")
    if image_items:
        parts.append("图片分析: " + "、".join(image_items))

    # 评论
    main_n, sub_n = count_comments(conn, note_id)
    if main_n:
        parts.append(f"{main_n} 条评论")

    return " | ".join(parts)


CSV_HEADERS = [
    "序号", "笔记ID", "标题", "正文摘要", "正文全文", "作者", "作者ID",
    "发布时间", "话题标签", "点赞", "收藏", "评论", "分享",
    "IP属地", "图片链接", "视频链接", "封面链接", "视频时长(秒)",
    "视频摘要", "图片OCR文字", "图片分析摘要", "图片Mermaid图",
    "笔记链接", "内容类型",
    "状态", "备注",
]


def write_csv(conn: sqlite3.Connection, path: Path | None = None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # 统计 DB 内容，生成描述性文件名
    total_notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    video_count = conn.execute("SELECT COUNT(*) FROM notes WHERE type='video'").fetchone()[0]
    note_count = total_notes - video_count
    date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    desc_parts = []
    if note_count:
        desc_parts.append(f"{note_count}图文")
    if video_count:
        desc_parts.append(f"{video_count}视频")
    desc = "+".join(desc_parts) if desc_parts else "export"
    path = path or (OUTPUT_DIR / f"xhs_{desc}_{date_str}.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(CSV_HEADERS)
        for i, note in enumerate(iter_notes(conn), 1):
            user = get_user(conn, note["user_id"]) if note["user_id"] else None
            raw = json.loads(note["raw_json"] or "{}")
            topics = json.loads(note["topics"] or "[]")
            images = _extract_images(raw)
            # 优先用本地图片路径
            csv_media_dir = _find_media_dir(note["note_id"], conn)
            if csv_media_dir.exists():
                local_imgs = sorted(csv_media_dir.glob("img_*"))
                if local_imgs:
                    images = [str(p) for p in local_imgs]
            # 优先用 DB 列，fallback 到从 raw_json 提取
            video_url = note["video_url"] or _extract_video_url(raw) or ""
            cover_url = note["cover_url"] or _extract_cover_url(raw) or ""
            video_duration = note["video_duration"] or 0
            video_summary = note["video_summary"] or ""
            desc = note["description"] or ""
            image_ocr = note["image_ocr_text"] or ""
            image_summary = note["image_summary"] or ""
            image_mermaid = note["image_mermaid"] or ""
            writer.writerow([
                i,
                note["note_id"],
                note["title"] or "",
                desc[:200],
                desc,
                (user["nickname"] if user else "") if user else "",
                note["user_id"] or "",
                note["published_at"] or "",
                " ".join(f"#{t}" for t in topics),
                note["liked_count"],
                note["collected_count"],
                note["comment_count"],
                note["share_count"],
                note["ip_location"] or "",
                "\n".join(images),
                video_url,
                cover_url,
                video_duration,
                video_summary,
                image_ocr,
                image_summary,
                image_mermaid,
                f"https://www.xiaohongshu.com/explore/{note['note_id']}",
                "视频" if note["type"] == "video" else "图文",
                "待处理",
                "",
            ])
    return path


def write_json(conn: sqlite3.Connection, path: Path | None = None) -> Path:
    """导出全部笔记为 JSON 文件。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = path or (OUTPUT_DIR / f"xhs_export_{date_str}.json")
    notes = []
    for note in iter_notes(conn):
        d = dict(note)
        # 解析 JSON 字段
        d["topics"] = json.loads(d.get("topics") or "[]")
        d["raw_json"] = json.loads(d.get("raw_json") or "{}")
        notes.append(d)
    path.write_text(
        json.dumps(notes, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def write_xlsx(conn: sqlite3.Connection, path: Path | None = None) -> Path:
    """导出全部笔记为 XLSX 文件（多 sheet: notes / users / comments）。需要 openpyxl。"""
    try:
        import openpyxl  # type: ignore
    except ImportError:
        print("[ERR] 导出 xlsx 需要 openpyxl。pip install openpyxl", file=sys.stderr)
        raise
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = path or (OUTPUT_DIR / f"xhs_export_{date_str}.xlsx")
    wb = openpyxl.Workbook()

    # Sheet 1: Notes
    ws_notes = wb.active
    ws_notes.title = "Notes"
    note_cols = [
        "note_id", "user_id", "title", "description", "type",
        "liked_count", "collected_count", "comment_count", "share_count",
        "ip_location", "published_at", "xsec_token",
        "video_url", "cover_url", "video_duration",
        "video_summary", "image_ocr_text", "image_summary",
        "crawled_at",
    ]
    ws_notes.append(note_cols)
    for note in iter_notes(conn):
        ws_notes.append([note.get(c, "") for c in note_cols])

    # Sheet 2: Users
    ws_users = wb.create_sheet("Users")
    user_cols = ["user_id", "nickname", "avatar", "fans_count", "notes_count", "desc"]
    ws_users.append(user_cols)
    for row in conn.execute("SELECT * FROM users ORDER BY fans_count DESC"):
        d = dict(row)
        ws_users.append([d.get(c, "") for c in user_cols])

    # Sheet 3: Comments
    ws_comments = wb.create_sheet("Comments")
    comment_cols = ["comment_id", "note_id", "content", "nickname", "like_count", "created_at"]
    ws_comments.append(comment_cols)
    for row in conn.execute("SELECT * FROM comments ORDER BY created_at DESC"):
        d = dict(row)
        ws_comments.append([d.get(c, "") for c in comment_cols])

    wb.save(path)
    return path


def _extract_images(raw: dict) -> list[str]:
    images: list[str] = []
    for img in raw.get("image_list", []) or []:
        url = img.get("url_default") or img.get("url") or ""
        info_list = img.get("info_list") or []
        for info in info_list:
            if info.get("image_scene") in ("WB_DFT", "WB_PRV"):
                url = info.get("url") or url
                break
        if url:
            images.append(url)
    return images


def _extract_video_url(raw: dict) -> str | None:
    video = raw.get("video") or {}
    media = video.get("media") or {}
    stream = media.get("stream") or {}
    for codec_key in ("h264", "h265", "av1"):
        items = stream.get(codec_key) or []
        if items:
            return items[0].get("master_url") or items[0].get("backup_urls", [None])[0]
    return None


def _extract_cover_url(raw: dict) -> str:
    """从 raw_json 提取视频封面图 URL。"""
    video = raw.get("video") or {}
    # 尝试多种路径
    for path in [
        lambda: video.get("cover", ""),
        lambda: (video.get("image_list") or [{}])[0].get("url_default", "") if video.get("image_list") else "",
        lambda: (raw.get("image_list") or [{}])[0].get("url_default", "") if raw.get("image_list") else "",
    ]:
        try:
            url = path()
            if url:
                return url
        except (IndexError, TypeError):
            continue
    return ""


def _extract_video_duration(raw: dict) -> int:
    """从 raw_json 提取视频时长（秒）。"""
    video = raw.get("video") or {}
    dur = video.get("duration")
    if dur:
        return int(dur) // 1000 if int(dur) > 1000 else int(dur)
    return 0


def _find_media_dir(note_id: str, conn: sqlite3.Connection) -> Path:
    """查找笔记的本地媒体目录（兼容新旧两种结构）。

    新结构: media/<博主名>/<笔记标题>/
    旧结构: media/<note_id>/
    """
    # 先尝试新路径
    new_dir = note_media_dir(note_id, conn)
    if new_dir.exists() and any(new_dir.iterdir()):
        return new_dir
    # 旧路径兼容
    legacy_dir = MEDIA_DIR / note_id
    if legacy_dir.exists():
        return legacy_dir
    # 都不存在，返回新路径（作为目标目录）
    return new_dir
