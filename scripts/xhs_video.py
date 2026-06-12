"""视频内容智能分析：语音转文字 + 关键帧 OCR + LLM 转录纠错。

流程：extract（音频+关键帧）→ transcribe（Whisper）→ OCR（关键帧文字）→ LLM 纠错

依赖（可选，缺失时优雅降级）：
  - ffmpeg          音频提取 / 关键帧抽帧
  - faster-whisper   语音转文字（本地，支持中文）
  - rapidocr-onnxruntime  画面文字 OCR（纯 Python，CPU 可跑）

LLM 纠错（用 OCR 画面文字纠正 Whisper 转录中的同音字和英文错误）：
  - auto   — 自动检测可用的 LLM 后端（openai > ollama > none）
  - openai — OpenAI 兼容 API（需 API Key）
  - ollama — 本地 Ollama 模型
  - none   — 不纠错，返回原始转录

配置文件：data/video_config.json
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG_PATH = DATA / "video_config.json"
MEDIA_DIR = DATA / "media"

from xhs_config import Heartbeat

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "correct_mode": "auto",       # auto / openai / ollama / none
    "whisper_model": "base",      # tiny(base快5倍) / base / small / medium / large-v3
    "whisper_beam_size": 3,       # 降低 beam_size 加速（默认5→3，精度略降）
    "max_transcribe_seconds": 300, # 最多转录前 N 秒音频（0=不限制）
    "frame_interval": 5,          # 关键帧间隔（秒）
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    "openai_base_url": "",        # 留空用官方，可填兼容端点
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
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, encoding="utf-8", errors="replace", timeout=5)
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
    }
    return status




# ----------------------------------------------------------------
# Audio extraction
# ----------------------------------------------------------------

def extract_audio(
    video_path: Path,
    output_path: Path | None = None,
    max_seconds: float = 0,
) -> Path:
    """用 ffmpeg 从视频中提取音频（16kHz mono WAV，whisper 最优格式）。

    max_seconds: > 0 时只提取前 N 秒音频，避免长视频生成巨大 WAV。
    """
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg 未安装。安装: brew install ffmpeg / sudo apt install ffmpeg / winget install Gyan.FFmpeg")
    if output_path is None:
        output_path = video_path.with_suffix(".wav")
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
    ]
    if max_seconds > 0:
        cmd += ["-t", str(max_seconds)]
        _msg(f"截取前 {max_seconds:.0f}s 音频")
    cmd += [
        "-vn",                   # 不要视频流
        "-acodec", "pcm_s16le",  # 16-bit PCM
        "-ar", "16000",          # 16kHz 采样率
        "-ac", "1",              # 单声道
        str(output_path),
    ]
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 音频提取失败: {r.stderr[:200]}")
    return output_path


# ----------------------------------------------------------------
# Speech-to-text (faster-whisper)
# ----------------------------------------------------------------

# 模型缓存：避免每次调用都重新加载（140MB-3GB）
_whisper_cache: tuple[str, Any] | None = None
_ocr_engine: Any = None


def _get_ocr():
    """获取或创建 RapidOCR 实例（带缓存）。"""
    global _ocr_engine
    if _ocr_engine is not None:
        return _ocr_engine
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        _ocr_engine = RapidOCR()
        return _ocr_engine
    except ImportError:
        return None


def _get_whisper_model(model_size: str = "base"):
    """获取或加载 Whisper 模型（带缓存，第一次加载约需 5–15 秒）。"""
    global _whisper_cache
    if _whisper_cache is not None and _whisper_cache[0] == model_size:
        return _whisper_cache[1]
    from faster_whisper import WhisperModel  # type: ignore
    _msg(f"加载 Whisper 模型 ({model_size})... 第一次较慢，缓存后复用")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    _whisper_cache = (model_size, model)
    return model


def transcribe(audio_path: Path, model_size: str = "tiny", beam_size: int = 3) -> dict[str, Any]:
    """转录音频，返回 {text: str, segments: [{start, end, text}], duration: float}。"""
    model = _get_whisper_model(model_size)

    _msg(f"转录中（模型 {model_size}，beam {beam_size}）...")
    segments_iter, info = model.transcribe(
        str(audio_path),
        language="zh",        # 中文优先，自动检测也可
        beam_size=beam_size,
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
# Keyframe extraction (scene-change detection)
# ----------------------------------------------------------------

def _parse_showinfo_timestamps(stderr_text: str) -> dict[int, float]:
    """从 ffmpeg showinfo 滤镜的 stderr 输出中解析帧时间戳。

    返回 {帧序号(1-based): 秒数}。
    showinfo 输出格式: [Parsed_showinfo_1 @ ...] n:   0 pts:12345 pts_time:12.345 ...
    """
    timestamps: dict[int, float] = {}
    for line in stderr_text.splitlines():
        if "pts_time:" in line:
            m_n = re.search(r'n:\s*(\d+)', line)
            m_t = re.search(r'pts_time:([\d.]+)', line)
            if m_n and m_t:
                idx = int(m_n.group(1)) + 1  # showinfo 的 n 从 0 开始，帧文件名从 1 开始
                timestamps[idx] = float(m_t.group(1))
    return timestamps


def _format_time(seconds: float) -> str:
    """秒数格式化为 MM:SS 或 HH:MM:SS。"""
    total = int(seconds)
    if total >= 3600:
        h, remainder = divmod(total, 3600)
        m, s = divmod(remainder, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def extract_keyframes(
    video_path: Path,
    output_dir: Path,
    scene_threshold: float = 0.3,
    fallback_interval: int = 5,
) -> tuple[list[Path], dict[int, float]]:
    """场景变化检测提取关键帧，返回 (帧路径列表, {帧序号: 秒数})。

    使用 ffmpeg select 滤镜检测场景变化（scene > threshold）。
    如果场景变化太少（< 3 帧），回退到定间隔抽帧。
    """
    if not has_ffmpeg():
        return [], {}

    # 清理旧帧
    if output_dir.exists():
        for f in output_dir.glob("frame_*.jpg"):
            f.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)

    pattern = str(output_dir / "frame_%04d.jpg")

    # 1. 场景变化检测
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vf", f"select=gt(scene\\,{scene_threshold}),showinfo",
        "-vsync", "vfr",
        "-q:v", "2",
        pattern,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    except subprocess.TimeoutExpired as proc:
        proc.kill()
        r = None

    frames = sorted(output_dir.glob("frame_*.jpg")) if output_dir.exists() else []
    timestamps = _parse_showinfo_timestamps(r.stderr) if r else {}

    # 2. 场景变化太少 → 回退到定间隔抽帧
    if len(frames) < 3:
        _msg(f"场景变化检测仅 {len(frames)} 帧，回退到每 {fallback_interval}s 抽帧")
        for f in frames:
            f.unlink()
        cmd_fallback = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", f"fps=1/{fallback_interval}",
            "-q:v", "2",
            pattern,
        ]
        try:
            subprocess.run(cmd_fallback, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        except subprocess.TimeoutExpired as proc:
            proc.kill()

        frames = sorted(output_dir.glob("frame_*.jpg"))
        # 定间隔模式：帧序号 × 间隔 = 时间
        timestamps = {}
        for i, _ in enumerate(frames, 1):
            timestamps[i] = (i - 1) * fallback_interval

    _msg(f"提取 {len(frames)} 个关键帧（含时间戳）")
    return frames, timestamps


# ----------------------------------------------------------------
# OCR
# ----------------------------------------------------------------

def _ocr_single(fp: Path, time_label: str = "") -> dict[str, str] | None:
    """对单张图片做 OCR。"""
    ocr = _get_ocr()
    if ocr is None:
        return None
    try:
        img_result, _ = ocr(str(fp))
        texts = []
        if img_result:
            for item in img_result:
                if len(item) >= 2 and item[1].strip():
                    texts.append(item[1].strip())
        if texts:
            return {"frame": fp.name, "time": time_label, "text": " ".join(texts)}
    except Exception:
        pass
    return None


def ocr_frames(frame_paths: list[Path], timestamps: dict[int, float] | None = None) -> list[dict[str, str]]:
    """对帧图片做 OCR，返回 [{"frame": "frame_0001.jpg", "time": "00:05", "text": "..."}]。复用 OCR 实例。"""
    ocr = _get_ocr()
    if ocr is None:
        _msg("rapidocr 未安装，跳过 OCR")
        return []

    timestamps = timestamps or {}
    results: list[dict[str, str]] = []
    for i, fp in enumerate(frame_paths, 1):
        sec = timestamps.get(i)
        time_label = _format_time(sec) if sec is not None else ""
        r = _ocr_single(fp, time_label)
        if r:
            results.append(r)

    _msg(f"OCR 完成: {len(results)}/{len(frame_paths)} 帧有文字")
    return results


# ----------------------------------------------------------------
# LLM Correction — 纠正 Whisper 转录错误
# ----------------------------------------------------------------


def _correct_via_openai(prompt: str, cfg: dict, fallback: str) -> str:
    """通过 OpenAI API 纠正转录错误。"""
    api_key = cfg.get("openai_api_key", "")
    if not api_key:
        return fallback
    model = cfg.get("openai_model", "gpt-4o-mini")
    base_url = cfg.get("openai_base_url", "") or "https://api.openai.com/v1"
    try:
        import requests as _req  # type: ignore
        resp = _req.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 2000},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip() or fallback
    except Exception as e:
        _msg(f"纠错 OpenAI 调用失败: {e}")
        return fallback


def _correct_via_ollama(prompt: str, cfg: dict, fallback: str) -> str:
    """通过 Ollama 纠正转录错误。"""
    url = cfg.get("ollama_url", "http://localhost:11434")
    model = cfg.get("ollama_model", "qwen2.5:7b")
    try:
        import requests as _req  # type: ignore
        resp = _req.post(
            f"{url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip() or fallback
    except Exception as e:
        _msg(f"纠错 Ollama 调用失败: {e}")
        return fallback


def _correct_transcript(transcript: str, ocr_results: list[dict], cfg: dict) -> str:
    """用 OCR 画面文字做参照，调用 LLM 纠正 Whisper 转录中的同音字和英文错误。"""
    ocr_text = " ".join(r["text"] for r in ocr_results)
    if not transcript or not ocr_text:
        return transcript

    # 从 OCR 提取关键术语（英文单词、技术名词）
    ocr_terms = set(re.findall(r'[A-Za-z][A-Za-z0-9+._#/-]{2,}', ocr_text))

    prompt = (
        "你是中文语音转录纠错助手。Whisper 语音识别的原始转录存在同音字和英文识别错误。\n"
        "以下是从视频画面 OCR 识别出的文字，这些是准确的参照。\n\n"
        "请修正转录中的错误：\n"
        "1. 英文单词识别错误（如 Cloud→Claude, Festor→Faster）\n"
        "2. 中文同音字错误（如 确→缺德, 康车性→Ctrl+C）\n"
        "3. 用 OCR 中的准确术语替换转录中的错误版本\n\n"
        f"【OCR 画面文字（参照）】\n{ocr_text[:2000]}\n\n"
        f"【OCR 中的关键术语】\n{', '.join(sorted(ocr_terms))}\n\n"
        f"【原始转录】\n{transcript}\n\n"
        "请输出修正后的完整转录（仅输出文本，不要解释）："
    )

    mode = cfg.get("correct_mode", "auto")
    if mode == "auto":
        # 自动检测可用的 LLM 后端
        if cfg.get("openai_api_key"):
            mode = "openai"
        elif _ollama_available(cfg):
            mode = "ollama"
        else:
            mode = "none"

    if mode == "openai":
        return _correct_via_openai(prompt, cfg, transcript)
    elif mode == "ollama":
        return _correct_via_ollama(prompt, cfg, transcript)
    else:
        return transcript  # 无 LLM 可用则跳过


def _ollama_available(cfg: dict) -> bool:
    """检测 Ollama 是否可用。"""
    url = cfg.get("ollama_url", "http://localhost:11434")
    try:
        import requests as _req
        resp = _req.get(f"{url}/api/tags", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


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
        "audio_duration": 0.0,
        "keyframes": 0,
        "corrected": False,
    }

    # 加载已有缓存结果
    audio_cache = cache_dir / "audio.wav"
    transcript_cache = cache_dir / "transcript.json"
    ocr_cache = cache_dir / "ocr.json"
    ts_cache = cache_dir / "frame_timestamps.json"

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
        max_sec = cfg.get("max_transcribe_seconds", 300)
        if has_ffmpeg():
            try:
                _msg("提取音频...")
                extract_audio(video_path, output_path=audio_cache,
                              max_seconds=max_sec if max_sec > 0 else 0)
                _msg(f"音频已缓存: {audio_cache}")
            except Exception as e:
                _msg(f"音频提取失败: {e}")
            try:
                frame_dir = cache_dir / "frames"
                frame_paths, frame_timestamps = extract_keyframes(
                    video_path, frame_dir,
                    fallback_interval=cfg.get("frame_interval", 5),
                )
                result["keyframes"] = len(frame_paths)
                # 缓存时间戳
                if frame_timestamps:
                    ts_cache.write_text(
                        json.dumps({str(k): v for k, v in frame_timestamps.items()}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                _msg(f"关键帧已缓存: {len(frame_paths)} 帧")
            except Exception as e:
                _msg(f"关键帧提取失败: {e}")
        else:
            _msg("跳过提取（缺少 ffmpeg）")

    # 2. 转录（transcribe 步骤）
    if all_steps or "transcribe" in steps:
        if audio_cache.exists() and has_whisper():
            try:
                whisper_result = transcribe(
                    audio_cache,
                    model_size=cfg.get("whisper_model", "base"),
                    beam_size=cfg.get("whisper_beam_size", 3),
                )
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
        # 加载时间戳缓存
        frame_timestamps: dict[int, float] = {}
        if ts_cache.exists():
            try:
                frame_timestamps = {int(k): v for k, v in json.loads(ts_cache.read_text(encoding="utf-8")).items()}
            except Exception:
                pass
        if not frame_paths:
            # 尝试提取关键帧
            if has_ffmpeg():
                try:
                    frame_paths, frame_timestamps = extract_keyframes(
                        video_path, frame_dir,
                        fallback_interval=cfg.get("frame_interval", 5),
                    )
                    if frame_timestamps:
                        ts_cache.write_text(
                            json.dumps({str(k): v for k, v in frame_timestamps.items()}, ensure_ascii=False),
                            encoding="utf-8",
                        )
                except Exception:
                    pass
        if frame_paths and has_ocr():
            try:
                result["ocr_results"] = ocr_frames(frame_paths, frame_timestamps)
                ocr_cache.write_text(json.dumps(result["ocr_results"], ensure_ascii=False, indent=2), encoding="utf-8")
                _msg(f"OCR 已缓存: {len(result['ocr_results'])} 帧")
            except Exception as e:
                _msg(f"OCR 失败: {e}")

    # 3.5 转录纠错（需要 transcript + ocr + LLM 后端）
    if result["transcript"] and result["ocr_results"]:
        corrected_cache = cache_dir / "transcript_corrected.json"
        if corrected_cache.exists():
            try:
                result["transcript"] = json.loads(
                    corrected_cache.read_text(encoding="utf-8")
                ).get("text", result["transcript"])
                result["corrected"] = True
                _msg("已加载纠错缓存")
            except Exception:
                pass
        else:
            original = result["transcript"]
            corrected = _correct_transcript(original, result["ocr_results"], cfg)
            if corrected != original:
                corrected_cache.write_text(
                    json.dumps(
                        {"text": corrected, "original": original},
                        ensure_ascii=False, indent=2,
                    ),
                    encoding="utf-8",
                )
                _msg(f"转录纠错完成: {len(original)} → {len(corrected)} 字")
                result["transcript"] = corrected
                result["corrected"] = True

    # 清理缓存目录（保留关键帧图片）
    if all_steps and cache_dir.exists():
        for f in [audio_cache, transcript_cache, ocr_cache, ts_cache, cache_dir / "transcript_corrected.json"]:
            if f and f.exists():
                try:
                    f.unlink()
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

    # 心跳：防止长耗时任务被 Kimi 2.6 等平台静默 kill
    hb = Heartbeat()
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
        if hasattr(args, "correct_mode") and args.correct_mode:
            cfg["correct_mode"] = args.correct_mode
        if args.whisper_model:
            cfg["whisper_model"] = args.whisper_model
        if args.frame_interval:
            cfg["frame_interval"] = args.frame_interval
        if hasattr(args, "max_duration") and args.max_duration is not None:
            cfg["max_transcribe_seconds"] = args.max_duration

        # 解析 --step 参数
        steps = None
        if hasattr(args, "step") and args.step is not None:
            valid_steps = {"extract", "transcribe", "ocr"}
            steps = [s for s in args.step if s in valid_steps]
            if not steps:
                print(f"[WARN] --step 无有效值，执行全部阶段", file=sys.stderr)
                steps = None
            else:
                print(f"[VIDEO] 分段模式: {' → '.join(steps)}", file=sys.stderr)
                if "transcribe" in steps and "extract" not in steps:
                    print("[VIDEO] 提示: transcribe 需要 extract 先完成（或音频已缓存）", file=sys.stderr)

        # 执行分析
        print(f"[VIDEO] 开始分析《{title}》...", file=sys.stderr)
        result = analyze_video(video_local, cfg, steps=steps)

        # 将结果存入 DB
        ocr_text = json.dumps(result.get("ocr_results", []), ensure_ascii=False)
        xhs_storage.update_video_analysis(
            conn,
            args.note_id,
            transcript=result.get("transcript", ""),
            ocr_text=ocr_text,
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
            updates.append(f"转录 {len(transcript)} 字")
            if result.get("corrected"):
                updates.append("已纠错")
            print(f"     转录预览: {transcript[:100]}{'...' if len(transcript) > 100 else ''}")
        ocr_results = result.get("ocr_results", [])
        if ocr_results:
            updates.append(f"OCR {len(ocr_results)} 帧")
        if not updates:
            print("     (无新内容)")
        else:
            print(f"     更新项: {'、'.join(updates)}")

        # 分段模式提示下一步
        if is_partial:
            all_stages = ["extract", "transcribe", "ocr"]
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
            print(f"     MD 已更新: {md.parent.name}/{md.name}")
        except Exception:
            pass

        return 0
    finally:
        conn.close()
        hb.stop()


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

    # LLM 纠错模式
    print("\n=== LLM 转录纠错配置 ===")
    print("视频转录会用 Whisper 进行语音转文字，然后 LLM 根据 OCR 画面文字纠正同音字和英文错误。")
    print("1) auto   — 自动检测可用的 LLM 后端（推荐）")
    print("2) openai — OpenAI 兼容 API（需 API Key）")
    print("3) ollama — 本地 Ollama 模型")
    print("4) none   — 不纠错，返回原始转录")

    if hasattr(args, "correct_mode") and args.correct_mode:
        mode = args.correct_mode
    else:
        try:
            choice = input("\n请选择 (1-4) [1]: ").strip() or "1"
            mode_map = {"1": "auto", "2": "openai", "3": "ollama", "4": "none"}
            mode = mode_map.get(choice, "auto")
        except (EOFError, KeyboardInterrupt):
            mode = "auto"

    cfg["correct_mode"] = mode
    print(f"\n已选择: {mode}")

    # OpenAI 配置
    if mode in ("auto", "openai"):
        print("\n=== OpenAI 兼容 API 配置 ===")
        try:
            key = input(f"API Key [{cfg.get('openai_api_key', '')}]: ").strip()
            if key:
                cfg["openai_api_key"] = key
            model = input(f"模型 [{cfg.get('openai_model', 'gpt-4o-mini')}]: ").strip()
            if model:
                cfg["openai_model"] = model
            base_url = input(f"API Base URL (留空用官方) []: ").strip()
            if base_url:
                cfg["openai_base_url"] = base_url
        except (EOFError, KeyboardInterrupt):
            pass

    # Ollama 配置
    if mode in ("auto", "ollama"):
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

    save_config(cfg)
    print(f"\n[OK] 视频配置已保存到 data/video_config.json")
    print(f"  correct_mode: {cfg['correct_mode']}")
    print(f"  whisper_model: {cfg['whisper_model']}")
    print(f"  frame_interval: {cfg['frame_interval']}s")
    return 0
