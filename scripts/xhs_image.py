"""图片 OCR 文字提取。

依赖（可选，缺失时优雅降级）：
  - rapidocr-onnxruntime  画面文字 OCR（纯 Python，CPU 可跑）

配置文件：data/image_config.json
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG_PATH = DATA / "image_config.json"

from xhs_config import Heartbeat

DEFAULT_CONFIG: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    """加载图片分析配置，合并默认值。"""
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return {**DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    return {**DEFAULT_CONFIG}


def save_config(cfg: dict[str, Any]) -> None:
    """保存图片分析配置。"""
    DATA.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def has_ocr() -> bool:
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Image discovery
# ---------------------------------------------------------------------------

def find_local_images(note_id: str, conn) -> list[Path]:
    """查找笔记的本地图片文件列表。

    复用 xhs_storage._find_media_dir() 兼容新旧目录结构。
    """
    import xhs_storage
    media_dir = xhs_storage._find_media_dir(note_id, conn)
    if not media_dir.exists():
        return []
    return sorted(p for p in media_dir.iterdir()
                  if p.name.startswith("img_") and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif"))


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

_ocr_engine = None


def _get_ocr_engine():
    """获取或初始化 RapidOCR 单例。"""
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            _ocr_engine = RapidOCR()
        except ImportError:
            return None
    return _ocr_engine


def _do_ocr(image_paths: list[Path]) -> str:
    """对所有图片做 OCR，返回合并的文字。复用 OCR 实例。"""
    if not image_paths:
        return ""
    ocr = _get_ocr_engine()
    if not ocr:
        _msg("rapidocr 未安装，跳过 OCR")
        return ""
    try:
        parts: list[str] = []
        for fp in image_paths:
            try:
                img_result, _ = ocr(str(fp))
                if img_result:
                    texts = [item[1].strip() for item in img_result if len(item) >= 2 and item[1].strip()]
                    if texts:
                        parts.append(f"[{fp.name}] {' '.join(texts)}")
            except Exception:
                continue
        _msg(f"OCR 完成: {len(parts)}/{len(image_paths)} 张图有文字")
        return "\n".join(parts)
    except Exception as e:
        _msg(f"OCR 失败: {e}")
        return ""


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def analyze_images(
    note_id: str,
    conn,
    cfg: dict[str, Any] | None = None,
    **_kwargs,
) -> dict[str, Any]:
    """分析图文笔记的图片内容（仅 OCR）。"""
    import xhs_storage
    import xhs_media

    cfg = cfg or load_config()

    result: dict[str, Any] = {
        "ocr_text": "",
        "image_summary": "",
        "mermaid": "",
        "image_count": 0,
    }

    # 1. 查找本地图片
    image_paths = find_local_images(note_id, conn)
    if not image_paths:
        _msg("本地无图片，先下载...")
        try:
            n_img, _, _, _ = xhs_media.download_media(note_id, conn, with_video=False)
            if n_img > 0:
                image_paths = find_local_images(note_id, conn)
        except Exception as e:
            _msg(f"图片下载失败: {e}")

    if not image_paths:
        _msg("无图片可分析")
        return result

    result["image_count"] = len(image_paths)
    _msg(f"找到 {len(image_paths)} 张图片")

    # 2. OCR
    _msg("OCR 识别中...")
    ocr_text = _do_ocr(image_paths)
    result["ocr_text"] = ocr_text
    if ocr_text:
        _msg(f"OCR 完成: {len(ocr_text)} 字")
    else:
        _msg("OCR 未识别到文字")

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(text: str) -> None:
    print(f"[IMAGE] {text}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------

def cmd_analyze_images(args) -> int:
    """对已入库的图文笔记做图片 OCR 文字提取。"""
    import xhs_storage

    hb = Heartbeat()
    conn = xhs_storage.connect()
    try:
        row = xhs_storage.get_note(conn, args.note_id)
        if not row:
            print(f"[ERR] 笔记 {args.note_id} 不在 DB，请先抓取入库", file=sys.stderr)
            return 1

        title = row["title"] or "(无标题)"

        # 检查依赖
        if not has_ocr():
            print("[WARN] rapidocr 未安装，OCR 不可用。pip install rapidocr-onnxruntime",
                  file=sys.stderr)

        # 执行分析
        print(f"[IMAGE] 开始分析《{title}》...", file=sys.stderr)
        result = analyze_images(args.note_id, conn)

        # 将结果存入 DB
        xhs_storage.update_image_analysis(
            conn,
            args.note_id,
            ocr_text=result.get("ocr_text", ""),
            summary="",
            mermaid="",
        )

        # 打印更新内容摘要
        print(f"\n[OK] 《{title}》图片分析完成:")
        ocr_text = result.get("ocr_text", "")
        if ocr_text:
            print(f"     OCR: {len(ocr_text)} 字")
            print(f"     预览: {ocr_text[:100]}{'...' if len(ocr_text) > 100 else ''}")
        else:
            print("     (无 OCR 文字)")
        image_count = result.get("image_count", 0)
        if image_count:
            print(f"     共 {image_count} 张图片")

        # 重新渲染 MD
        try:
            md = xhs_storage.write_markdown(conn, args.note_id)
            print(f"     MD 已更新: {md.parent.name}/{md.name}")
        except Exception:
            pass

        return 0
    finally:
        conn.close()
        hb.stop()
