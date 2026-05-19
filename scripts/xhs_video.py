"""视频内容智能分析：语音转文字 + 关键帧 OCR + AI 摘要（五档可选）。

依赖（可选，缺失时优雅降级）：
  - ffmpeg          音频提取 / 关键帧抽帧
  - faster-whisper   语音转文字（本地，支持中文）
  - rapidocr-onnxruntime  画面文字 OCR（纯 Python，CPU 可跑）

AI 摘要五档：
  1) none   — 不生成摘要，仅返回转录+OCR 结构化数据（由调用方 AI 分析）
  2) local  — 基于转录文本的本地摘要（jieba 关键词 + 句子评分，无额外依赖）
  3) ollama — 调用本地 Ollama 多模态模型（需安装 Ollama + 下载模型）
  4) openai — 调用 OpenAI GPT-4o API（需 API Key）
  5) mcp    — MCP 视觉工具（AI Agent 提供，零配置）

配置文件：data/video_config.json
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG_PATH = DATA / "video_config.json"
MEDIA_DIR = DATA / "media"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "summary_mode": "none",       # none / ollama / openai / local / mcp
    "whisper_model": "base",       # base / small / medium / large-v3
    "frame_interval": 5,           # 关键帧间隔（秒）
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    "openai_base_url": "",         # 留空用官方，可填兼容端点
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen2.5:7b",
}


# ----------------------------------------------------------------
# Config
# ----------------------------------------------------------------

def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # 合并默认值（防止旧配置缺字段）
            merged = {**DEFAULT_CONFIG, **cfg}
            return merged
        except Exception:
            pass
    return {**DEFAULT_CONFIG}


def save_config(cfg: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------
# Dependency checks
# ----------------------------------------------------------------

def has_ffmpeg() -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def has_whisper() -> bool:
    try:
        from faster_whisper import WhisperModel  # noqa: F401
        return True
    except ImportError:
        return False


def has_ocr() -> bool:
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401
        return True
    except ImportError:
        return False


def deps_status() -> dict[str, bool]:
    cfg = load_config()
    status = {
        "ffmpeg": has_ffmpeg(),
        "faster-whisper": has_whisper(),
        "rapidocr": has_ocr(),
        "jieba": has_jieba(),
    }
    if cfg.get("summary_mode") == "mcp":
        status["mcp"] = True  # 由 AI Agent 运行时提供
    return status


def has_jieba() -> bool:
    try:
        import jieba  # noqa: F401
        return True
    except ImportError:
        return False


# ----------------------------------------------------------------
# Audio extraction
# ----------------------------------------------------------------

def extract_audio(video_path: Path, output_path: Path | None = None) -> Path:
    """用 ffmpeg 从视频中提取音频（16kHz mono WAV，whisper 最优格式）。"""
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg 未安装。安装: brew install ffmpeg / sudo apt install ffmpeg / winget install Gyan.FFmpeg")
    if output_path is None:
        output_path = video_path.with_suffix(".wav")
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn",                   # 不要视频流
        "-acodec", "pcm_s16le",  # 16-bit PCM
        "-ar", "16000",          # 16kHz 采样率
        "-ac", "1",              # 单声道
        str(output_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 音频提取失败: {r.stderr[:200]}")
    return output_path


# ----------------------------------------------------------------
# Speech-to-text (faster-whisper)
# ----------------------------------------------------------------

# 模型缓存：避免每次调用都重新加载（140MB-3GB）
_whisper_cache: tuple[str, Any] | None = None


def _get_whisper_model(model_size: str = "base"):
    """获取或加载 Whisper 模型（带缓存）。"""
    global _whisper_cache
    if _whisper_cache is not None and _whisper_cache[0] == model_size:
        return _whisper_cache[1]
    from faster_whisper import WhisperModel  # type: ignore
    _msg(f"加载 Whisper 模型 ({model_size})...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    _whisper_cache = (model_size, model)
    return model


def transcribe(audio_path: Path, model_size: str = "base") -> dict[str, Any]:
    """转录音频，返回 {text: str, segments: [{start, end, text}], duration: float}。"""
    model = _get_whisper_model(model_size)

    _msg("转录中...")
    segments_iter, info = model.transcribe(
        str(audio_path),
        language="zh",        # 中文优先，自动检测也可
        beam_size=5,
        vad_filter=True,      # 过滤静音段
    )

    segments: list[dict] = []
    full_text_parts: list[str] = []
    for seg in segments_iter:
        segments.append({"start": round(seg.start, 1), "end": round(seg.end, 1), "text": seg.text.strip()})
        full_text_parts.append(seg.text.strip())

    full_text = " ".join(full_text_parts)
    _msg(f"转录完成: {len(full_text)} 字, {info.duration:.0f}s")

    return {
        "text": full_text,
        "segments": segments,
        "duration": round(info.duration, 1),
    }


# ----------------------------------------------------------------
# Keyframe extraction
# ----------------------------------------------------------------

def extract_keyframes(video_path: Path, output_dir: Path, interval: int = 5) -> list[Path]:
    """每隔 interval 秒抽一帧，返回帧图片路径列表。"""
    if not has_ffmpeg():
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(output_dir / "frame_%04d.jpg")
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vf", f"fps=1/{interval}",
        "-q:v", "2",   # 高质量 JPEG
        pattern,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as proc:
        proc.kill()
        _msg("关键帧提取超时（5分钟），使用已提取的帧")
    else:
        if r.returncode != 0:
            _msg(f"关键帧提取警告: {r.stderr[:100]}")
    frames = sorted(output_dir.glob("frame_*.jpg"))
    _msg(f"提取 {len(frames)} 个关键帧（每 {interval}s）")
    return frames


# ----------------------------------------------------------------
# OCR
# ----------------------------------------------------------------

def ocr_frames(frame_paths: list[Path]) -> list[dict[str, str]]:
    """对帧图片做 OCR，返回 [{"frame": "frame_0001.jpg", "text": "..."}]。"""
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except ImportError:
        _msg("rapidocr 未安装，跳过 OCR")
        return []
    ocr = RapidOCR()
    results: list[dict[str, str]] = []
    for fp in frame_paths:
        try:
            img_result, _ = ocr(str(fp))
            texts = []
            if img_result:
                for item in img_result:
                    # item: [bbox, text, confidence]
                    if len(item) >= 2 and item[1].strip():
                        texts.append(item[1].strip())
            if texts:
                results.append({"frame": fp.name, "text": " ".join(texts)})
        except Exception:
            continue
    _msg(f"OCR 完成: {len(results)}/{len(frame_paths)} 帧有文字")
    return results


# ----------------------------------------------------------------
# AI Summary — 五档实现
# ----------------------------------------------------------------

def summarize_none(transcript: str, ocr_results: list[dict], frame_paths: list[Path]) -> str:
    """方案 1：不生成 AI 摘要，返回结构化数据文档。"""
    parts = ["【视频内容提取结果】"]
    if transcript:
        parts.append(f"\n转录全文 ({len(transcript)} 字):\n{transcript}")
    if ocr_results:
        parts.append("\n画面文字:")
        for r in ocr_results:
            parts.append(f"  [{r['frame']}] {r['text']}")
    parts.append(f"\n关键帧图片: {len(frame_paths)} 张")
    return "\n".join(parts)


def summarize_local(transcript: str, ocr_results: list[dict], frame_paths: list[Path]) -> str:
    """方案 4：基于转录文本的本地摘要（jieba 关键词 + 句子评分，无额外依赖）。"""
    if not transcript:
        return "(无转录内容，无法生成摘要)"

    # 分句
    sentences = re.split(r'[。！？；\n]', transcript)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 3]
    if not sentences:
        return transcript[:500]

    # 提取关键词
    try:
        import jieba.analyse  # type: ignore
        keywords = set(jieba.analyse.extract_tags(transcript, topK=20))
    except ImportError:
        # 简单 fallback：高频词
        words = re.findall(r'[\u4e00-\u9fff]{2,4}', transcript)
        from collections import Counter
        keywords = set(w for w, _ in Counter(words).most_common(20))

    # 评分每个句子
    scored: list[tuple[float, str]] = []
    for i, sent in enumerate(sentences):
        score = 0.0
        # 关键词命中
        for kw in keywords:
            if kw in sent:
                score += 1.0
        # 位置加分（开头和结尾的句子更重要）
        if i < 3:
            score += 0.5
        if i >= len(sentences) - 2:
            score += 0.3
        # 长度适中加分
        if 10 < len(sent) < 80:
            score += 0.3
        scored.append((score, sent))

    # 取 top 句子
    scored.sort(key=lambda x: x[0], reverse=True)
    top_n = min(8, len(scored))
    # 用 enumerate 避免重复句子的 index 问题
    sent_positions = {s: i for i, s in enumerate(sentences)}
    top = sorted(scored[:top_n], key=lambda x: sent_positions.get(x[1], 0))
    summary = "。".join(s for _, s in top) + "。"

    # 补充 OCR 信息
    ocr_text = " ".join(r["text"] for r in ocr_results)
    if ocr_text:
        summary += f"\n\n画面文字信息: {ocr_text[:200]}"

    return summary


def summarize_ollama(
    transcript: str,
    ocr_results: list[dict],
    frame_paths: list[Path],
    cfg: dict[str, Any],
) -> str:
    """方案 2：调用本地 Ollama 生成摘要。"""
    url = cfg.get("ollama_url", "http://localhost:11434")
    model = cfg.get("ollama_model", "qwen2.5:7b")

    ocr_text = " ".join(r["text"] for r in ocr_results)
    prompt = (
        "你是一个内容分析助手。请根据以下小红书视频的内容信息，生成 200-500 字的中文内容摘要，"
        "包括：视频主题、主要内容要点、关键信息。\n\n"
    )
    if transcript:
        prompt += f"【语音转录】\n{transcript}\n\n"
    if ocr_text:
        prompt += f"【画面文字】\n{ocr_text}\n\n"
    prompt += "请生成摘要："

    # 构建请求
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }

    # 如果有帧图片，用多模态方式发送（需要 Ollama 支持视觉模型）
    if frame_paths and _ollama_supports_vision(url, model):
        images_b64: list[str] = []
        for fp in frame_paths[:5]:
            images_b64.append(base64.b64encode(fp.read_bytes()).decode())
        payload["images"] = images_b64

    try:
        import requests as _req  # type: ignore
        resp = _req.post(f"{url}/api/generate", json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        _msg(f"Ollama 调用失败: {e}")
        return summarize_local(transcript, ocr_results, frame_paths)


def _ollama_supports_vision(url: str, model: str) -> bool:
    """检查 Ollama 模型是否支持视觉。"""
    try:
        import requests as _req
        resp = _req.post(f"{url}/api/show", json={"name": model}, timeout=10)
        if resp.status_code == 200:
            info = resp.json()
            families = info.get("details", {}).get("families", [])
            return "clip" in families or "llava" in families or "qwen2-vl" in str(info)
    except Exception:
        pass
    return False


def summarize_mcp(
    transcript: str,
    ocr_results: list[dict],
    frame_paths: list[Path],
    cfg: dict[str, Any],
) -> str:
    """MCP 视觉后端：输出结构化任务清单，由 AI Agent 调用 MCP 工具完成视频分析。

    Python 不直接调用 MCP Server，而是输出任务描述到 stderr，
    AI Agent（Claude Code / GLM Coding / Cursor 等）解析后使用自己环境中的 MCP 视觉工具。
    """
    lines = [
        "[MCP_VIDEO_TASK]",
        f"transcript_length: {len(transcript)}",
        f"keyframe_count: {len(frame_paths)}",
    ]
    if transcript:
        lines.append("transcript: |")
        for line in transcript.strip().splitlines():
            lines.append(f"  {line}")
    else:
        lines.append("transcript: ''")
    if ocr_results:
        lines.append("ocr_text: |")
        for r in ocr_results:
            lines.append(f"  [{r['frame']}] {r['text']}")
    else:
        lines.append("ocr_text: ''")
    if frame_paths:
        lines.append("keyframes:")
        for fp in frame_paths[:10]:
            lines.append(f"  - {fp}")
    lines.append("prompt: |")
    suggested = (
        "请根据以上视频转录和画面文字信息，生成 200-500 字的中文内容摘要，"
        "包括：视频主题、主要内容要点、关键信息。"
        "如果有关键帧图片，请也分析图片内容。"
    )
    for line in suggested.splitlines():
        lines.append(f"  {line}")
    lines.append("[/MCP_VIDEO_TASK]")

    task_text = "\n".join(lines)
    _msg(task_text)
    _msg(f"[MCP_PENDING] 已输出视频分析任务（转录 {len(transcript)} 字 + {len(frame_paths)} 关键帧），请 AI Agent 使用 MCP 视觉工具完成分析")
    return f"[MCP_PENDING] 已输出视频分析任务，请 AI Agent 使用 MCP 视觉工具完成分析"


def summarize_openai(
    transcript: str,
    ocr_results: list[dict],
    frame_paths: list[Path],
    cfg: dict[str, Any],
) -> str:
    """方案 3：调用 OpenAI GPT-4o API 生成摘要。"""
    api_key = cfg.get("openai_api_key", "")
    if not api_key:
        return "(未配置 OpenAI API Key)"

    model = cfg.get("openai_model", "gpt-4o-mini")
    base_url = cfg.get("openai_base_url", "") or "https://api.openai.com/v1"

    ocr_text = " ".join(r["text"] for r in ocr_results)

    # 构建消息
    text_content = (
        "你是一个内容分析助手。请根据以下小红书视频的内容信息，生成 200-500 字的中文内容摘要，"
        "包括：视频主题、主要内容要点、关键信息。\n\n"
    )
    if transcript:
        text_content += f"【语音转录】\n{transcript}\n\n"
    if ocr_text:
        text_content += f"【画面文字】\n{ocr_text}\n\n"
    text_content += "请生成摘要："

    content: list[dict] = [{"type": "text", "text": text_content}]

    # 附加帧图片（最多 3 张，控制 token 消耗）
    for fp in frame_paths[:3]:
        b64 = base64.b64encode(fp.read_bytes()).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    try:
        import requests as _req
        resp = _req.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 1000,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return reply.strip() or summarize_local(transcript, ocr_results, frame_paths)
    except Exception as e:
        _msg(f"OpenAI API 调用失败: {e}")
        return summarize_local(transcript, ocr_results, frame_paths)


# ----------------------------------------------------------------
# Router
# ----------------------------------------------------------------

_SUMMARY_BACKENDS = {
    "none": summarize_none,
    "local": summarize_local,
    "ollama": summarize_ollama,
    "openai": summarize_openai,
    "mcp": summarize_mcp,
}


def generate_summary(
    transcript: str,
    ocr_results: list[dict],
    frame_paths: list[Path],
    cfg: dict[str, Any],
) -> str:
    """根据配置选择摘要后端。"""
    mode = cfg.get("summary_mode", "none")
    fn = _SUMMARY_BACKENDS.get(mode, summarize_none)

    if mode in ("ollama", "openai", "mcp"):
        return fn(transcript, ocr_results, frame_paths, cfg)
    return fn(transcript, ocr_results, frame_paths)


# ----------------------------------------------------------------
# Main: analyze a video
# ----------------------------------------------------------------

def analyze_video(
    video_path: Path,
    cfg: dict[str, Any] | None = None,
    steps: list[str] | None = None,
) -> dict[str, Any]:
    """分析单个视频文件，返回完整结果。

    steps: 控制执行哪些阶段，None 表示全部执行。
           ["extract"]  → 仅提取音频+关键帧
           ["transcribe"] → 仅转录（需先 extract）
           ["ocr"]      → 仅 OCR（需先 extract）
           ["summary"]  → 仅生成摘要（需先 transcribe/ocr）
           支持任意组合，如 ["extract", "transcribe"]
    """
    cfg = cfg or load_config()
    all_steps = steps is None
    cache_dir = video_path.parent / "_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "transcript": "",
        "segments": [],
        "ocr_results": [],
        "summary": "",
        "audio_duration": 0.0,
        "keyframes": 0,
    }

    # 加载已有缓存结果
    audio_cache = cache_dir / "audio.wav"
    transcript_cache = cache_dir / "transcript.json"
    ocr_cache = cache_dir / "ocr.json"

    if transcript_cache.exists():
        try:
            d = json.loads(transcript_cache.read_text(encoding="utf-8"))
            result["transcript"] = d.get("text", "")
            result["segments"] = d.get("segments", [])
            result["audio_duration"] = d.get("duration", 0.0)
        except Exception:
            pass
    if ocr_cache.exists():
        try:
            result["ocr_results"] = json.loads(ocr_cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    # 1. 提取音频 + 关键帧（extract 步骤）
    if all_steps or "extract" in steps:
        if has_ffmpeg():
            try:
                _msg("提取音频...")
                extract_audio(video_path, output_path=audio_cache)
                _msg(f"音频已缓存: {audio_cache}")
            except Exception as e:
                _msg(f"音频提取失败: {e}")
            try:
                frame_dir = cache_dir / "frames"
                frame_paths = extract_keyframes(
                    video_path, frame_dir, cfg.get("frame_interval", 5)
                )
                result["keyframes"] = len(frame_paths)
                _msg(f"关键帧已缓存: {len(frame_paths)} 帧")
            except Exception as e:
                _msg(f"关键帧提取失败: {e}")
        else:
            _msg("跳过提取（缺少 ffmpeg）")

    # 2. 转录（transcribe 步骤）
    if all_steps or "transcribe" in steps:
        if audio_cache.exists() and has_whisper():
            try:
                whisper_result = transcribe(audio_cache, cfg.get("whisper_model", "base"))
                result["transcript"] = whisper_result["text"]
                result["segments"] = whisper_result["segments"]
                result["audio_duration"] = whisper_result["duration"]
                # 缓存转录结果
                transcript_cache.write_text(json.dumps(whisper_result, ensure_ascii=False, indent=2), encoding="utf-8")
                _msg(f"转录已缓存: {len(whisper_result['text'])} 字")
            except Exception as e:
                _msg(f"转录失败: {e}")
        elif not audio_cache.exists():
            _msg("跳过转录（需先执行 extract 步骤提取音频）")
        else:
            _msg("跳过转录（缺少 faster-whisper）")

    # 3. OCR（ocr 步骤）
    if all_steps or "ocr" in steps:
        frame_dir = cache_dir / "frames"
        frame_paths = sorted(frame_dir.glob("frame_*.jpg")) if frame_dir.exists() else []
        if not frame_paths:
            # 尝试提取关键帧
            if has_ffmpeg():
                try:
                    frame_paths = extract_keyframes(
                        video_path, frame_dir, cfg.get("frame_interval", 5)
                    )
                except Exception:
                    pass
        if frame_paths and has_ocr():
            try:
                result["ocr_results"] = ocr_frames(frame_paths)
                ocr_cache.write_text(json.dumps(result["ocr_results"], ensure_ascii=False, indent=2), encoding="utf-8")
                _msg(f"OCR 已缓存: {len(result['ocr_results'])} 帧")
            except Exception as e:
                _msg(f"OCR 失败: {e}")

    # 4. AI 摘要（summary 步骤）
    if all_steps or "summary" in steps:
        frame_dir = cache_dir / "frames"
        frame_paths = sorted(frame_dir.glob("frame_*.jpg")) if frame_dir.exists() else []
        try:
            result["summary"] = generate_summary(
                result["transcript"],
                result["ocr_results"],
                frame_paths,
                cfg,
            )
        except Exception as e:
            _msg(f"摘要生成失败: {e}")

    # 清理缓存目录（仅在完整执行时）
    if all_steps and cache_dir.exists():
        try:
            shutil.rmtree(cache_dir, ignore_errors=True)
        except Exception:
            pass

    return result


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------

def _msg(text: str) -> None:
    print(f"[VIDEO] {text}", file=sys.stderr, flush=True)


def extract_video_cover(raw: dict) -> str:
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


def extract_video_duration(raw: dict) -> int:
    """从 raw_json 提取视频时长（秒）。"""
    video = raw.get("video") or {}
    # 优先用 video.duration（毫秒）
    dur = video.get("duration")
    if dur:
        return int(dur) // 1000 if int(dur) > 1000 else int(dur)
    return 0


# ---------------------------------------------------------------------------
# CLI command handlers (moved from xhs.py)
# ---------------------------------------------------------------------------

def cmd_analyze_video(args) -> int:
    """对已入库的视频笔记做智能分析：语音转文字 + 关键帧 OCR + AI 摘要。"""
    import xhs_storage
    import xhs_media

    conn = xhs_storage.connect()
    try:
        row = xhs_storage.get_note(conn, args.note_id)
        if not row:
            print(f"[ERR] 笔记 {args.note_id} 不在 DB，请先抓取入库", file=sys.stderr)
            return 1
        if row["type"] != "video":
            print(f"[ERR] 笔记 {args.note_id} 不是视频类型（type={row['type']}）", file=sys.stderr)
            return 1

        title = row["title"] or "(无标题)"

        # 确保有本地视频文件
        video_local = xhs_media.find_video_local(args.note_id, conn)
        if not video_local:
            print(f"[VIDEO] 《{title}》本地无视频文件，先下载...", file=sys.stderr)
            _, _, _, out = xhs_media.download_media(args.note_id, conn, with_video=True)
            video_local = out / "video.mp4"
            if not video_local.exists():
                print("[ERR] 视频下载失败", file=sys.stderr)
                return 1

        # 检查依赖
        deps = deps_status()
        missing = [k for k, v in deps.items() if not v]
        if missing:
            print(f"[WARN] 缺少依赖: {', '.join(missing)}，部分功能不可用", file=sys.stderr)

        # 加载配置
        cfg = load_config()
        if args.mode:
            cfg["summary_mode"] = args.mode
        if args.whisper_model:
            cfg["whisper_model"] = args.whisper_model
        if args.frame_interval:
            cfg["frame_interval"] = args.frame_interval

        # 解析 --step 参数
        steps = None
        if hasattr(args, "step") and args.step is not None:
            valid_steps = {"extract", "transcribe", "ocr", "summary"}
            steps = [s for s in args.step if s in valid_steps]
            if not steps:
                print(f"[WARN] --step 无有效值，执行全部阶段", file=sys.stderr)
                steps = None
            else:
                print(f"[VIDEO] 分段模式: {' → '.join(steps)}", file=sys.stderr)
                if "transcribe" in steps and "extract" not in steps:
                    print("[VIDEO] 提示: transcribe 需要 extract 先完成（或音频已缓存）", file=sys.stderr)

        # 执行分析
        mode_label = cfg.get("summary_mode", "none")
        if mode_label == "mcp":
            print("[INFO] 使用 MCP 视觉后端（由 AI Agent 提供，零配置）", file=sys.stderr)
        print(f"[VIDEO] 开始分析《{title}》...", file=sys.stderr)
        result = analyze_video(video_local, cfg, steps=steps)

        # 将结果存入 DB
        ocr_text = " ".join(r["text"] for r in result.get("ocr_results", []))
        xhs_storage.update_video_analysis(
            conn,
            args.note_id,
            transcript=result.get("transcript", ""),
            ocr_text=ocr_text,
            summary=result.get("summary", ""),
        )

        # 打印更新内容摘要
        is_partial = steps is not None
        if is_partial:
            print(f"\n[OK] 《{title}》分段完成 ({' → '.join(steps)})")
        else:
            print(f"\n[OK] 《{title}》视频分析完成:")
        updates = []
        transcript = result.get("transcript", "")
        if transcript:
            updates.append(f"语音转录 {len(transcript)} 字")
            print(f"     转录预览: {transcript[:100]}{'...' if len(transcript) > 100 else ''}")
        ocr_results = result.get("ocr_results", [])
        if ocr_results:
            updates.append(f"画面文字 {len(ocr_results)} 帧")
        summary = result.get("summary", "")
        if summary:
            updates.append(f"AI 摘要 {len(summary)} 字")
            print(f"     摘要: {summary[:100]}{'...' if len(summary) > 100 else ''}")
        if not updates:
            print("     (无新内容)")
        else:
            print(f"     更新项: {'、'.join(updates)}")

        # 分段模式提示下一步
        if is_partial:
            all_stages = ["extract", "transcribe", "ocr", "summary"]
            done = set(steps)
            remaining = [s for s in all_stages if s not in done]
            if remaining:
                next_step = remaining[0]
                print(f"\n[NEXT] 下一步: python scripts/xhs.py analyze-video {args.note_id} --step {next_step}")
                if len(remaining) > 1:
                    print(f"       或执行所有剩余: --step {' '.join(remaining)}")

        # 重新渲染 MD
        try:
            md = xhs_storage.write_markdown(conn, args.note_id)
            print(f"     MD 已更新: {md.name}")
        except Exception:
            pass

        return 0
    finally:
        conn.close()


def cmd_setup_video(args) -> int:
    """交互式配置视频分析。"""
    cfg = load_config()

    # 检查依赖
    deps = deps_status()
    print("=== 视频分析依赖检查 ===")
    for k, v in deps.items():
        status = "OK" if v else "未安装"
        print(f"  {k}: {status}")

    if not deps.get("ffmpeg"):
        print("\nffmpeg 未安装。视频分析需要 ffmpeg。")
        print("安装方式:")
        print("  Windows: winget install Gyan.FFmpeg")
        print("  macOS:   brew install ffmpeg")
        print("  Linux:   sudo apt install ffmpeg / sudo yum install ffmpeg")
        return 1

    # 交互选择摘要模式
    print("\n=== AI 视频摘要模式选择 ===")
    print("1) none   — 不生成 AI 摘要，仅返回转录+OCR 数据（由调用方 AI 分析）")
    print("2) local  — 本地文本摘要（jieba 关键词 + 句子评分，无需额外服务）")
    print("3) ollama — 调用本地 Ollama 模型（需安装 Ollama + 下载模型）")
    print("4) openai — 调用 OpenAI GPT-4o API（需 API Key）")
    print("5) mcp    — MCP 视觉工具（AI Agent 提供，零配置）")

    if args.mode:
        mode = args.mode
    else:
        try:
            choice = input("\n请选择 (1-5) [1]: ").strip() or "1"
            mode_map = {"1": "none", "2": "local", "3": "ollama", "4": "openai", "5": "mcp"}
            mode = mode_map.get(choice, "none")
        except (EOFError, KeyboardInterrupt):
            mode = "none"

    cfg["summary_mode"] = mode
    print(f"\n已选择: {mode}")

    # Whisper 模型大小
    print("\n=== Whisper 模型选择 ===")
    print("  tiny   — 最快，精度低 (~75MB)")
    print("  base   — 平衡（推荐）(~145MB)")
    print("  small  — 较好精度 (~488MB)")
    print("  medium — 高精度 (~1.5GB)")
    print("  large-v3 — 最高精度 (~3GB)")
    if args.whisper_model:
        cfg["whisper_model"] = args.whisper_model
    else:
        try:
            wm = input(f"选择 Whisper 模型 [base]: ").strip() or "base"
            if wm in ("tiny", "base", "small", "medium", "large-v3"):
                cfg["whisper_model"] = wm
        except (EOFError, KeyboardInterrupt):
            pass

    # 关键帧间隔
    if args.frame_interval:
        cfg["frame_interval"] = args.frame_interval
    else:
        try:
            fi = input(f"关键帧间隔秒数 [5]: ").strip()
            if fi:
                cfg["frame_interval"] = int(fi)
        except (EOFError, KeyboardInterrupt, ValueError):
            pass

    # OpenAI 配置
    if mode == "openai":
        print("\n=== OpenAI 配置 ===")
        try:
            key = input("API Key: ").strip()
            if key:
                cfg["openai_api_key"] = key
            model = input(f"模型 [gpt-4o-mini]: ").strip() or "gpt-4o-mini"
            cfg["openai_model"] = model
            base_url = input(f"API Base URL (留空用官方) []: ").strip()
            if base_url:
                cfg["openai_base_url"] = base_url
        except (EOFError, KeyboardInterrupt):
            pass

    # Ollama 配置
    if mode == "ollama":
        print("\n=== Ollama 配置 ===")
        try:
            url = input(f"Ollama URL [{cfg.get('ollama_url', 'http://localhost:11434')}]: ").strip()
            if url:
                cfg["ollama_url"] = url
            model = input(f"模型 [{cfg.get('ollama_model', 'qwen2.5:7b')}]: ").strip()
            if model:
                cfg["ollama_model"] = model
        except (EOFError, KeyboardInterrupt):
            pass

    save_config(cfg)
    print(f"\n[OK] 视频配置已保存到 data/video_config.json")
    print(f"  summary_mode: {cfg['summary_mode']}")
    print(f"  whisper_model: {cfg['whisper_model']}")
    print(f"  frame_interval: {cfg['frame_interval']}s")
    return 0
