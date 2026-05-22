"""媒体下载：图片/视频下载 + 自动下载 + 视频分析后处理。

从 xhs.py 拆分出来的媒体处理层：
- guess_ext(): URL 扩展名猜测
- download_media(): 下载笔记的图片/视频（自动使用博主/标题路径）
- auto_download_note(): 抓取笔记后自动下载图片（轻量，不打扰用户）
- post_process_note(): crawl 时的 --download / --analyze 后处理
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
from pathlib import Path

import xhs_config
import xhs_storage

# curl_cffi 可用性检测（同 xhs_fetcher）
try:
    from curl_cffi.requests import get as _cget  # type: ignore
    _CURL_CFFI = True
except ImportError:
    _CURL_CFFI = False


def guess_ext(url: str, default: str = ".jpg") -> str:
    url_low = url.lower().split("?")[0]
    for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mov", ".webm"):
        if url_low.endswith(ext):
            return ext
    return default


def _media_out_dir(note_id: str, conn: sqlite3.Connection) -> Path:
    """计算媒体输出目录: media/<博主名>/<笔记标题>/。"""
    return xhs_config.note_media_dir(note_id, conn)


def download_media(
    note_id: str, conn: sqlite3.Connection, with_video: bool = True, overwrite: bool = False
) -> tuple[int, int, int, Path]:
    """下载某笔记的图片/视频。
    返回 (n_img, n_video, n_err, out_dir)。
    """
    row = xhs_storage.get_note(conn, note_id)
    if not row:
        print(f"[WARN] 笔记 {note_id} 不在 DB，跳过下载", file=sys.stderr)
        return 0, 0, 1, xhs_config.MEDIA_DIR / note_id
    raw = json.loads(row["raw_json"] or "{}")
    out = _media_out_dir(note_id, conn)
    out.mkdir(parents=True, exist_ok=True)

    if _CURL_CFFI:
        def _fetch_bytes(url: str) -> bytes:
            r = _cget(url, impersonate=xhs_config.IMPERSONATE_PROFILE, timeout=30)
            r.raise_for_status()
            return r.content
    else:
        import requests as _rq  # type: ignore
        def _fetch_bytes(url: str) -> bytes:
            r = _rq.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.content

    # 1) 图片
    images = xhs_storage._extract_images(raw)
    n_ok = 0
    n_err = 0
    for i, url in enumerate(images, 1):
        ext = guess_ext(url, default=".jpg")
        dst = out / f"img_{i:02d}{ext}"
        if dst.exists() and not overwrite:
            n_ok += 1  # 已有算成功
            continue
        try:
            data = _fetch_bytes(url)
            tmp = dst.with_suffix(dst.suffix + '.tmp')
            try:
                tmp.write_bytes(data)
                tmp.replace(dst)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            n_ok += 1
            time.sleep(random.uniform(0.3, 1.0))
        except Exception:
            n_err += 1

    # 2) 视频（流式）
    n_video = 0
    if with_video:
        video_url = row["video_url"] or xhs_storage._extract_video_url(raw)
        if video_url:
            dst = out / "video.mp4"
            if dst.exists() and not overwrite:
                n_video = 1
            else:
                try:
                    tmp = dst.with_suffix('.mp4.tmp')
                    try:
                        if _CURL_CFFI:
                            r = _cget(video_url, impersonate=xhs_config.IMPERSONATE_PROFILE, timeout=60, stream=True)
                            r.raise_for_status()
                            with open(tmp, 'wb') as vf:
                                for chunk in r.iter_content(chunk_size=8192):
                                    vf.write(chunk)
                        else:
                            import requests as _rq2  # type: ignore
                            r = _rq2.get(video_url, timeout=60, stream=True,
                                         headers={"User-Agent": "Mozilla/5.0"})
                            r.raise_for_status()
                            with open(tmp, 'wb') as vf:
                                for chunk in r.iter_content(chunk_size=8192):
                                    vf.write(chunk)
                        tmp.replace(dst)
                        n_video = 1
                    except Exception:
                        tmp.unlink(missing_ok=True)
                        raise
                except Exception:
                    n_err += 1

    return n_ok, n_video, n_err, out


def auto_download_note(note: dict, conn: sqlite3.Connection) -> None:
    """抓取笔记后自动下载图片（轻量，仅图片，不打扰用户）。

    - 只下图片，不下视频（视频大，需要用户主动）
    - 静默跳过已下载的
    - 一行输出让用户知道在做什么
    """
    note_id = note.get("note_id", "")
    if not note_id:
        return
    try:
        n_img, _, n_err, out = download_media(note_id, conn, with_video=False)
        if n_img > 0 and n_err == 0:
            title = note.get("title") or note.get("description", "")[:20] or note_id
            # 计算可读的相对路径
            rel = out.relative_to(xhs_config.MEDIA_DIR)
            print(f"     图片: {rel} ({n_img}张)")
    except Exception as e:
        print(f"     [auto-download] {note_id}: {e}", file=sys.stderr)


def find_video_local(note_id: str, conn: sqlite3.Connection) -> Path | None:
    """查找笔记的本地视频文件路径（兼容新旧目录结构）。"""
    # 新路径: media/<博主>/<标题>/video.mp4
    out = _media_out_dir(note_id, conn)
    video = out / "video.mp4"
    if video.exists():
        return video
    # 旧路径兼容: media/<note_id>/video.mp4
    legacy = xhs_config.MEDIA_DIR / note_id / "video.mp4"
    if legacy.exists():
        return legacy
    return None


def _refresh_video_url(note_id: str, conn: sqlite3.Connection) -> None:
    """当 video_url 为空时，提示用户需要先抓笔记详情。

    列表 API（user_posted / search）返回摘要数据，不含完整 video URL。
    需要通过 `note <id>` 命令获取完整数据才能下载视频。
    """
    print(f"    [refresh] 笔记 {note_id} 缺少 video_url，请先运行: "
          f"python scripts/xhs.py note {note_id}", file=sys.stderr)


def post_process_note(note: dict, conn: sqlite3.Connection, args) -> None:
    """根据 --download / --analyze 标志对单条笔记做后处理。"""
    do_download = getattr(args, "download", False)
    do_analyze = getattr(args, "analyze", False)
    if not do_download and not do_analyze:
        return
    note_id = note["note_id"]
    is_video = note.get("type") == "video"
    title = note.get("title") or note.get("description", "")[:20] or note_id

    if do_download:
        try:
            n_img, n_vid, n_err, out = download_media(note_id, conn, with_video=True)
            if n_img + n_vid > 0:
                parts = []
                if n_img:
                    parts.append(f"{n_img}张图")
                if n_vid:
                    parts.append("视频")
                rel = out.relative_to(xhs_config.MEDIA_DIR)
                print(f"    [download] 《{title}》{'+'.join(parts)} → {rel}", file=sys.stderr)
        except Exception as e:
            print(f"    [download] 《{title}》失败: {e}", file=sys.stderr)

    if do_analyze and is_video:
        try:
            import xhs_video
            video_local = find_video_local(note_id, conn)
            if not video_local:
                # video_url 可能为空（列表 API 不返回完整 video 信息）
                # 先尝试抓笔记详情补全 video_url
                if not note.get("video_url"):
                    _refresh_video_url(note_id, conn)
                _, _, _, out = download_media(note_id, conn, with_video=True)
                video_local = out / "video.mp4"
            if video_local.exists():
                cfg = xhs_video.load_config()
                result = xhs_video.analyze_video(video_local, cfg)
                ocr_text = " ".join(r["text"] for r in result.get("ocr_results", []))
                xhs_storage.update_video_analysis(
                    conn, note_id,
                    transcript=result.get("transcript", ""),
                    ocr_text=ocr_text,
                    summary=result.get("summary", ""),
                )
                updates = []
                if result.get("transcript"):
                    updates.append(f"转录{len(result['transcript'])}字")
                if result.get("ocr_results"):
                    updates.append(f"OCR {len(result['ocr_results'])}帧")
                if result.get("summary"):
                    updates.append("摘要")
                detail = "、".join(updates) if updates else "无新内容"
                print(f"    [analyze] 《{title}》: {detail}", file=sys.stderr)
            else:
                print(f"    [analyze] 《{title}》视频下载失败，跳过分析", file=sys.stderr)
        except Exception as e:
            print(f"    [analyze] 《{title}》失败: {e}", file=sys.stderr)
    elif do_analyze and not is_video:
        # 图文笔记 → 图片分析
        try:
            import xhs_image
            cfg = xhs_image.load_config()
            result = xhs_image.analyze_images(note_id, conn, cfg)
            xhs_storage.update_image_analysis(
                conn, note_id,
                ocr_text=result.get("ocr_text", ""),
                summary=result.get("image_summary", ""),
                mermaid=result.get("mermaid", ""),
            )
            updates = []
            if result.get("ocr_text"):
                updates.append(f"OCR {len(result['ocr_text'])} 字")
            if result.get("image_summary"):
                updates.append("AI 描述")
            if result.get("mermaid"):
                updates.append("路线图/流程图")
            detail = "、".join(updates) if updates else "无新内容"
            print(f"    [analyze-image] 《{title}》: {detail}", file=sys.stderr)
        except Exception as e:
            print(f"    [analyze-image] 《{title}》失败: {e}", file=sys.stderr)
