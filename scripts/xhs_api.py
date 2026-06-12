"""API 抓取层 + 数据标准化函数。

从 xhs.py 拆分出来的纯函数层：
- 所有 fetch_* API 调用函数
- _normalize_* 数据标准化函数
- _to_int / _ts_to_str 辅助函数
"""

from __future__ import annotations

import hashlib
import math
import random
import sys
import time
from typing import Any

import xhs_storage
import xhs_config


# ---------------------------------------------------------------------------
# API 抓取函数
# ---------------------------------------------------------------------------

def fetch_note_detail(fetcher, note_id: str, xsec_token: str = "", xsec_source: str = "pc_search") -> dict:
    api = "/api/sns/web/v1/feed"
    data = {
        "source_note_id": note_id,
        "image_formats": ["jpg", "webp", "avif"],
        "extra": {"need_body_topic": "1"},
        "xsec_source": xsec_source or "pc_search",
        "xsec_token": xsec_token,
    }
    payload = fetcher.post(api, data)
    items = (payload.get("data") or {}).get("items") or []
    if not items:
        print(f"[API] 笔记 {note_id} 返回空 items（可能已删除/私密/被风控）", file=sys.stderr)
        return {}
    return items[0]


def fetch_user_info(fetcher, user_id: str) -> dict:
    api = "/api/sns/web/v1/user/otherinfo"
    payload = fetcher.get(api, {"target_user_id": user_id})
    return (payload.get("data") or {}).get("basic_info") or {}


def fetch_user_notes(
    fetcher, user_id: str, cursor: str = "", page_size: int = 30
) -> dict:
    api = "/api/sns/web/v1/user_posted"
    payload = fetcher.get(api, {
        "num": page_size,
        "cursor": cursor,
        "user_id": user_id,
        "image_formats": "jpg,webp,avif",
    })
    return payload.get("data") or {}


def fetch_search(
    fetcher, keyword: str, page: int = 1, page_size: int = 20
) -> dict:
    # 决定本次搜索模式（不修改全局 SEARCH_MODE）
    _use_dom = xhs_config.SEARCH_MODE == "dom"

    # curl_cffi 缺失时，本次请求强制走 DOM（不修改全局配置）
    if not _use_dom:
        try:
            from curl_cffi.requests import Session  # noqa: F401 – 检测是否可用
        except ImportError:
            print("[SEARCH-FALLBACK] curl_cffi 不可用，API 搜索不安全，本次走 DOM",
                  file=sys.stderr)
            _use_dom = True

    # SEARCH_MODE="dom"：始终走浏览器 DOM 搜索（降低 API 指纹暴露）
    if _use_dom:
        print(f"[SEARCH-DOM] 浏览器搜索模式：keyword={keyword} page={page}",
              file=sys.stderr)
        result = fetcher.search_dom(keyword, page)
        return result.get("data") or {}

    # 搜索 API 小时配额：预占配额，超出则直接走 DOM
    slot = fetcher._reserve_search_slot()
    if slot is None:
        print(f"[SEARCH-QUOTA] 配额已满，走 DOM：keyword={keyword} page={page}",
              file=sys.stderr)
        result = fetcher.search_dom(keyword, page)
        return result.get("data") or {}

    api = "/api/sns/web/v1/search/notes"
    data = {
        "keyword": keyword,
        "page": page,
        "page_size": page_size,
        "search_id": _make_search_id(),
        "sort": "general",
        "note_type": 0,
    }
    try:
        payload = fetcher.post(api, data)
        slot.confirm()  # API 成功，确认消耗配额
    except Exception:
        slot.release()  # API 失败，释放预占配额
        raise
    return payload.get("data") or {}


def fetch_feed(
    fetcher,
    category: str = "homefeed_recommend",
    cursor_score: str = "",
    num: int = 18,
) -> dict:
    """推荐流 / 分类流。cursor_score 翻页。"""
    api = "/api/sns/web/v1/homefeed"
    data = {
        "cursor_score": cursor_score,
        "num": num,
        "refresh_type": 1 if not cursor_score else 2,
        "note_index": 0,
        "unread_begin_note_id": "",
        "unread_end_note_id": "",
        "unread_note_count": 0,
        "category": category,
        "search_key": "",
        "image_formats": ["jpg", "webp", "avif"],
    }
    payload = fetcher.post(api, data)
    return payload.get("data") or {}


def fetch_comments(
    fetcher, note_id: str, xsec_token: str, cursor: str = "", top_comment_id: str = ""
) -> dict:
    """主评论分页。返回 data: {comments: [...], cursor: str, has_more: bool}"""
    api = "/api/sns/web/v2/comment/page"
    params = {
        "note_id": note_id,
        "cursor": cursor,
        "top_comment_id": top_comment_id,
        "image_formats": "jpg,webp,avif",
        "xsec_token": xsec_token,
    }
    payload = fetcher.get(api, params)
    return payload.get("data") or {}


def fetch_sub_comments(
    fetcher, note_id: str, root_comment_id: str, xsec_token: str,
    cursor: str = "", num: int = 10,
) -> dict:
    """子评论分页"""
    api = "/api/sns/web/v2/comment/sub/page"
    params = {
        "note_id": note_id,
        "root_comment_id": root_comment_id,
        "num": num,
        "cursor": cursor,
        "image_formats": "jpg,webp,avif",
        "top_comment_id": "",
        "xsec_token": xsec_token,
    }
    payload = fetcher.get(api, params)
    return payload.get("data") or {}


def _make_search_id() -> str:
    timestamp_ms = int(time.time() * 1000)
    random_part = math.ceil(0x7ffffffe * random.random())
    value = (timestamp_ms << 64) + random_part
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    result = ""
    while value:
        value, rem = divmod(value, 36)
        result = chars[rem] + result
    return result


# ---------------------------------------------------------------------------
# 数据标准化（纯函数）
# ---------------------------------------------------------------------------

def _compute_content_hash(note: dict) -> str:
    """计算笔记关键字段的轻量 hash，用于增量更新检测。

    覆盖：标题、描述、互动数据、类型、话题、xsec_token、视频URL。
    不含 raw_json（太大且包含不稳定的临时字段）。
    """
    payload = "|".join(str(v) for v in [
        note.get("title", ""),
        note.get("description", ""),
        note.get("liked_count", 0),
        note.get("collected_count", 0),
        note.get("comment_count", 0),
        note.get("share_count", 0),
        note.get("type", ""),
        note.get("xsec_token", ""),
        note.get("video_url", ""),
        "|".join(note.get("topics", []) or []),
    ])
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _normalize_note(item: dict) -> dict:
    if not item:
        return {"note_id": "", "user_id": "", "title": "", "description": "",
                "type": "", "liked_count": 0, "collected_count": 0, "comment_count": 0,
                "share_count": 0, "ip_location": "", "topics": [], "published_at": "",
                "xsec_token": "", "xsec_source": "", "video_url": "", "cover_url": "",
                "video_duration": 0, "raw": {}, "content_hash": ""}
    note = item.get("note_card") or item
    user = note.get("user") or {}
    interact = note.get("interact_info") or {}
    # xsec_token 可能在 item 顶层（search 返回）或 note_card 内
    xsec_token = item.get("xsec_token") or note.get("xsec_token", "")
    xsec_source = item.get("xsec_source") or note.get("xsec_source", "")

    # 提前提取 video_url 用于 type 兜底
    video_url = xhs_storage._extract_video_url(note) or ""

    # type 判定：优先用 API 返回值，兜底用 video 字段存在性
    note_type = note.get("type", "")
    if not note_type or note_type not in ("video", "normal", "note"):
        # API 未返回 type 或值异常 → 通过 video 字段推断
        note_type = "video" if (video_url or note.get("video")) else "note"

    ret = {
        "note_id": item.get("id") or note.get("note_id") or note.get("id"),
        "user_id": user.get("user_id") or user.get("userid", ""),
        "title": note.get("title") or note.get("display_title", ""),
        "description": note.get("desc", ""),
        "type": note_type,
        "liked_count": _to_int(interact.get("liked_count")),
        "collected_count": _to_int(interact.get("collected_count")),
        "comment_count": _to_int(interact.get("comment_count")),
        "share_count": _to_int(interact.get("share_count")),
        "ip_location": note.get("ip_location", ""),
        "topics": [t.get("name", "") for t in (note.get("tag_list") or []) if t.get("name")],
        "published_at": _ts_to_str(note.get("time") or note.get("last_update_time")),
        "xsec_token": xsec_token,
        "xsec_source": xsec_source,
        "video_url": video_url,
        "cover_url": xhs_storage._extract_cover_url(note) or "",
        "video_duration": xhs_storage._extract_video_duration(note),
        "raw": note,
    }
    ret["content_hash"] = _compute_content_hash(ret)
    return ret


def _normalize_user(info: dict) -> dict:
    return {
        "user_id": info.get("red_id") or info.get("user_id") or info.get("userid", ""),
        "nickname": info.get("nickname") or info.get("nick_name", ""),
        "avatar": info.get("imageb") or info.get("avatar", ""),
        "description": info.get("desc", ""),
        "fans_count": _to_int(info.get("fans")),
        "follow_count": _to_int(info.get("follows")),
        "notes_count": _to_int(info.get("notes")),
        "location": info.get("ip_location", ""),
        "raw": info,
    }


def _normalize_comment(c: dict, note_id: str, parent_id: str = "") -> dict:
    user = c.get("user_info") or {}
    pictures = [p.get("url_default") or p.get("url") or "" for p in (c.get("pictures") or [])]
    target = c.get("target_comment") or {}
    return {
        "comment_id": c.get("id", ""),
        "note_id": note_id,
        "parent_id": parent_id,
        "user_id": user.get("user_id", ""),
        "nickname": user.get("nickname", ""),
        "content": c.get("content", ""),
        "like_count": _to_int(c.get("like_count")),
        "ip_location": c.get("ip_location", ""),
        "pictures": [p for p in pictures if p],
        "target_comment_id": target.get("id", ""),
        "created_at": _ts_to_str(c.get("create_time")),
        "raw": c,
    }


def _to_int(v: Any) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return 0
    if s.endswith("万"):
        try:
            return int(float(s[:-1]) * 10000)
        except ValueError:
            return 0
    try:
        return int(s.replace(",", ""))
    except ValueError:
        return 0


def _ts_to_str(ts: Any) -> str:
    if not ts:
        return ""
    try:
        from datetime import datetime, timezone, timedelta
        ts = int(ts)
        if ts > 1e12:
            ts //= 1000
        cst = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(ts, tz=cst).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)
