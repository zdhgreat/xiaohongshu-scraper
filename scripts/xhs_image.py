"""图片智能分析：OCR 文字提取 + AI 视觉描述 + Mermaid 图表生成。

三层分析能力：
  Layer 1: OCR 文字提取    →  图片中所有可识别的文字
  Layer 2: AI 视觉描述     →  AI "看懂"图片内容（路线、穿搭、步骤...）
  Layer 3: Mermaid 图表    →  自动生成路线图/流程图（嵌入 MD）

依赖（可选，缺失时优雅降级）：
  - rapidocr-onnxruntime  画面文字 OCR（纯 Python，CPU 可跑）
  - jieba                  本地模式文本分析

配置文件：data/image_config.json
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG_PATH = DATA / "image_config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "image_mode": "auto",           # auto / none / local / vision
    "image_vision_backend": "api",  # ollama / api / mcp（仅 vision 模式）
    "image_mermaid": True,          # 是否生成 Mermaid 图表
    # API 后端（OpenAI 兼容）
    "api_base_url": "",
    "api_key": "",
    "api_model": "",
    # Ollama 后端
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen2.5:7b",
}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PROMPT_ANALYSIS = """你是一个小红书图文内容分析助手。请根据以下图片内容，生成详细的中文内容描述和分析。

请注意：
1. 逐张描述图片中的主要内容（如景点、物品、步骤、穿搭等）
2. 如果是教程/攻略类内容，提取关键步骤和要点
3. 如果有文字信息，请准确转录
4. 总结整个图文笔记的核心价值和关键信息
5. 提取可能被搜索到的关键词

笔记标题：{title}
笔记正文：{description}
图片中识别到的文字（OCR）：
{ocr_text}"""

PROMPT_SYNTHESIS = """你是一个内容综合助手。以下是同一篇笔记的多批图片分析结果，请将它们合并为一份完整、连贯的中文内容描述。

要求：
1. 合并重复信息
2. 保持逻辑连贯
3. 保留所有关键细节

各批分析结果：
{partials}"""

PROMPT_MERMAID = """基于以下图片内容分析，判断这篇笔记是否包含路线图、流程图或步骤序列。

如果包含，请生成 Mermaid 语法代码（graph LR 或 flowchart TD），并输出 JSON：
{{"has_diagram": true, "mermaid_code": "graph LR\\n    ...", "diagram_type": "route"}}

diagram_type 取值：route（旅游路线）、steps（教程步骤）、comparison（对比分析）

如果不包含路线/流程/步骤类内容：
{{"has_diagram": false}}

注意：
- 节点名称用中文，连线标签包含交通方式/时间/费用等关键信息
- 使用 graph LR（路线类）或 flowchart TD（步骤类）
- 保持简洁，节点不超过 10 个
- 只输出 JSON，不要输出其他内容

图片内容分析：
{image_summary}"""


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


def has_jieba() -> bool:
    try:
        import jieba  # noqa: F401
        return True
    except ImportError:
        return False


def deps_status() -> dict[str, bool]:
    status = {
        "rapidocr": has_ocr(),
        "jieba": has_jieba(),
    }
    # 检查视觉后端可达性
    cfg = load_config()
    backend = cfg.get("image_vision_backend", "api")
    if backend == "api":
        status["api_key"] = bool(cfg.get("api_key", ""))
    elif backend == "ollama":
        try:
            import requests as _req  # type: ignore
            url = cfg.get("ollama_url", "http://localhost:11434")
            resp = _req.get(f"{url}/api/tags", timeout=3)
            status["ollama"] = resp.status_code == 200
        except Exception:
            status["ollama"] = False
    elif backend == "mcp":
        status["mcp"] = True  # 由 AI Agent 运行时提供
    return status


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
# Layer 1: OCR
# ---------------------------------------------------------------------------

def _do_ocr(image_paths: list[Path]) -> str:
    """对所有图片做 OCR，返回合并的文字。"""
    if not image_paths:
        return ""
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except ImportError:
        _msg("rapidocr 未安装，跳过 OCR")
        return ""
    try:
        ocr = RapidOCR()
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
# Layer 2: Local analysis (image_mode: "local")
# ---------------------------------------------------------------------------

def _analyze_local(ocr_text: str, title: str, desc: str) -> str:
    """基于 OCR 文字的本地分析（jieba 关键词 + 高频词 + 关联分析）。"""
    if not ocr_text:
        return "(图片中未识别到文字)"

    parts = ["【图片文字分析】"]

    # 1. OCR 全文
    parts.append(f"\n图片识别文字 ({len(ocr_text)} 字):\n{ocr_text}")

    # 2. 关键词提取
    combined = f"{title} {desc} {ocr_text}"
    try:
        import jieba.analyse  # type: ignore
        keywords = jieba.analyse.extract_tags(combined, topK=20)
        if keywords:
            parts.append(f"\n关键词: {', '.join(keywords)}")
    except ImportError:
        # 简单降级：中文高频词
        words = re.findall(r'[\u4e00-\u9fff]{2,4}', ocr_text)
        from collections import Counter
        top = [w for w, _ in Counter(words).most_common(15)]
        if top:
            parts.append(f"\n高频词: {', '.join(top)}")

    # 3. 与标题/正文的关联
    title_words = set(re.findall(r'[\u4e00-\u9fff]{2,}', title))
    desc_words = set(re.findall(r'[\u4e00-\u9fff]{2,}', desc))
    ocr_words = set(re.findall(r'[\u4e00-\u9fff]{2,}', ocr_text))
    overlap_title = title_words & ocr_words
    overlap_desc = desc_words & ocr_words
    if overlap_title:
        parts.append(f"\n与标题关联词: {', '.join(overlap_title)}")
    if overlap_desc:
        parts.append(f"\n与正文关联词: {', '.join(overlap_desc)}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Layer 2: Vision analysis (image_mode: "vision")
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Auto-detection: 根据OCR结果自动判断是否需要AI视觉
# ---------------------------------------------------------------------------

# 关键词 → 大概率包含视觉内容，OCR 不够
_VISION_KEYWORDS = [
    "路线", "攻略", "行程", "穿搭", "搭配", "教程", "步骤", "流程",
    "对比", "推荐", "清单", "打卡", "景点", "美食", "探店",
    "旅游", "旅行", "自驾", "高铁", "飞机", "地图",
    "图解", "示意", "手绘", "标注",
]

# 关键词 → 大概率纯文字内容，OCR 足够
_TEXT_KEYWORDS = [
    "文案", "文字", "语录", "摘抄", "金句", "诗词", "名言",
    "价格", "报价", "清单", "参数", "规格",
]


def _should_use_vision(
    ocr_text: str,
    title: str,
    desc: str,
    image_count: int,
    has_ai_backend: bool,
) -> tuple[bool, str]:
    """自动判断是否需要 AI 视觉分析。

    返回 (need_vision, reason):
      need_vision: True 表示需要 AI 视觉
      reason: 判断原因（用于日志）
    """
    if not has_ai_backend:
        return False, "无可用 AI 后端"

    ocr_len = len(ocr_text)
    combined = f"{title} {desc}"

    # ── 规则 1: OCR 文字非常丰富（> 500 字/图），大概率纯文字图，OCR 足够
    if image_count > 0 and ocr_len / image_count > 500:
        return False, f"OCR 文字充足（{ocr_len} 字 / {image_count} 图），无需 AI"

    # ── 规则 2: 标题/描述含视觉类关键词
    vision_hits = [kw for kw in _VISION_KEYWORDS if kw in combined]
    if len(vision_hits) >= 2:
        return True, f"标题含视觉关键词: {', '.join(vision_hits[:3])}"

    # ── 规则 3: OCR 文字很少但有多张图 → 大概率是视觉内容（路线图/穿搭图等）
    if ocr_len < 50 and image_count >= 3:
        return True, f"OCR 文字少（{ocr_len} 字）但图片多（{image_count} 张）"

    # ── 规则 4: 标题/描述含纯文字类关键词
    text_hits = [kw for kw in _TEXT_KEYWORDS if kw in combined]
    if len(text_hits) >= 1 and ocr_len > 100:
        return False, f"标题含文字类关键词且 OCR 充足: {', '.join(text_hits)}"

    # ── 规则 5: OCR 完全空白 → 必须用 AI
    if ocr_len == 0 and image_count > 0:
        return True, "OCR 无文字，需 AI 看图"

    # ── 规则 6: OCR 适度 + 无明显视觉关键词 → 跳过 AI
    if ocr_len > 200 and not vision_hits:
        return False, f"OCR 文字足够（{ocr_len} 字）且无视觉关键词"

    # ── 规则 7: 中间地带 → 有 1 个视觉关键词就调 AI
    if vision_hits:
        return True, f"含视觉关键词: {vision_hits[0]}"

    # ── 默认: 有 OCR 文字就跳过
    if ocr_len > 0:
        return False, f"OCR 有文字（{ocr_len} 字），默认跳过 AI"

    return False, "默认跳过"


def _check_ai_backend_available(cfg: dict[str, Any]) -> bool:
    """检查是否有可用的 AI 视觉后端。"""
    backend = cfg.get("image_vision_backend", "api")
    if backend == "api":
        return bool(cfg.get("api_key", ""))
    elif backend == "ollama":
        try:
            import requests as _req  # type: ignore
            url = cfg.get("ollama_url", "http://localhost:11434")
            resp = _req.get(f"{url}/api/tags", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False
    elif backend == "mcp":
        return True  # MCP 由 Agent 提供，始终可用
    return False


def _vision_api(
    image_paths: list[Path],
    prompt: str,
    cfg: dict[str, Any],
    batch_size: int = 3,
) -> str:
    """OpenAI 兼容 Chat Completions 视觉调用。

    分批发送图片，多批时做综合合并。
    """
    api_key = cfg.get("api_key", "")
    if not api_key:
        raise RuntimeError("未配置 API Key")

    base_url = cfg.get("api_base_url", "") or "https://api.openai.com/v1"
    model = cfg.get("api_model", "") or "gpt-4o-mini"

    def _call_batch(paths: list[Path], batch_prompt: str) -> str:
        content: list[dict] = [{"type": "text", "text": batch_prompt}]
        for p in paths:
            b64 = base64.b64encode(p.read_bytes()).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
        import requests as _req  # type: ignore
        resp = _req.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 2000,
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    return _batch_and_synthesize(image_paths, prompt, cfg, _call_batch, batch_size)


def _vision_ollama(
    image_paths: list[Path],
    prompt: str,
    cfg: dict[str, Any],
    batch_size: int = 5,
) -> str:
    """Ollama 视觉调用。

    分批发送图片，多批时做综合合并。
    """
    url = cfg.get("ollama_url", "http://localhost:11434")
    model = cfg.get("ollama_model", "qwen2.5:7b")

    def _call_batch(paths: list[Path], batch_prompt: str) -> str:
        images_b64: list[str] = []
        for p in paths:
            images_b64.append(base64.b64encode(p.read_bytes()).decode())
        payload: dict[str, Any] = {
            "model": model,
            "prompt": batch_prompt,
            "images": images_b64,
            "stream": False,
        }
        import requests as _req  # type: ignore
        resp = _req.post(f"{url}/api/generate", json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()

    return _batch_and_synthesize(image_paths, prompt, cfg, _call_batch, batch_size)


def _batch_and_synthesize(
    image_paths: list[Path],
    prompt: str,
    cfg: dict[str, Any],
    call_fn,
    batch_size: int,
) -> str:
    """分批调用 + 多批综合。"""
    # 单批直接调用
    if len(image_paths) <= batch_size:
        return call_fn(image_paths, prompt)

    # 多批分片
    partials: list[str] = []
    for i in range(0, len(image_paths), batch_size):
        batch = image_paths[i:i + batch_size]
        batch_label = f"（第 {i // batch_size + 1} 批，图片 {i + 1}-{min(i + batch_size, len(image_paths))}）"
        _msg(f"分析 {batch_label}...")
        try:
            partial = call_fn(batch, prompt)
            partials.append(partial)
        except Exception as e:
            _msg(f"批次分析失败: {e}")

    if not partials:
        raise RuntimeError("所有批次均失败")
    if len(partials) == 1:
        return partials[0]

    # 综合
    _msg("综合多批分析结果...")
    synthesis_prompt = PROMPT_SYNTHESIS.format(
        partials="\n---\n".join(f"批次 {i + 1}:\n{p}" for i, p in enumerate(partials))
    )
    # 综合调用只发文本，不发图片
    backend = cfg.get("image_vision_backend", "api")
    if backend == "ollama":
        return _ollama_text_call(synthesis_prompt, cfg)
    else:
        return _api_text_call(synthesis_prompt, cfg)


def _ollama_text_call(prompt: str, cfg: dict[str, Any]) -> str:
    """Ollama 纯文本调用（不发图片）。"""
    url = cfg.get("ollama_url", "http://localhost:11434")
    model = cfg.get("ollama_model", "qwen2.5:7b")
    import requests as _req  # type: ignore
    resp = _req.post(
        f"{url}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _api_text_call(prompt: str, cfg: dict[str, Any]) -> str:
    """OpenAI 兼容纯文本调用（不发图片）。"""
    api_key = cfg.get("api_key", "")
    base_url = cfg.get("api_base_url", "") or "https://api.openai.com/v1"
    model = cfg.get("api_model", "") or "gpt-4o-mini"
    import requests as _req  # type: ignore
    resp = _req.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2000,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _vision_mcp(
    image_paths: list[Path],
    ocr_text: str,
    title: str,
    desc: str,
    prompt: str,
) -> str:
    """MCP 视觉后端：输出结构化任务清单，由 AI Agent 调用 MCP 工具完成分析。

    Python 不直接调用 MCP Server，而是输出任务描述到 stderr，
    AI Agent（Claude Code / GLM Coding / Cursor 等）解析后使用自己环境中的 MCP 视觉工具。
    """
    lines = [
        "[MCP_VISION_TASK]",
        f"title: {title or '(无标题)'}",
        f"image_count: {len(image_paths)}",
        "images:",
    ]
    for p in image_paths:
        lines.append(f"  - {p}")
    if ocr_text:
        lines.append("ocr_text: |")
        for line in ocr_text.strip().splitlines():
            lines.append(f"  {line}")
    else:
        lines.append("ocr_text: ''")
    lines.append("prompt: |")
    suggested = (
        f"请分析这篇小红书笔记的 {len(image_paths)} 张图片。"
        "逐张描述图片内容（景点、物品、步骤、穿搭等），"
        "如果有文字请准确转录，最后总结核心信息。"
    )
    for line in suggested.splitlines():
        lines.append(f"  {line}")
    lines.append("[/MCP_VISION_TASK]")

    task_text = "\n".join(lines)
    _msg(task_text)
    _msg(f"[MCP_PENDING] 已输出 {len(image_paths)} 张图片的分析任务，请 AI Agent 使用 MCP 视觉工具完成分析")
    return f"[MCP_PENDING] 已输出 {len(image_paths)} 张图片的分析任务，请 AI Agent 使用 MCP 视觉工具完成分析"


def _vision_analyze(
    image_paths: list[Path],
    ocr_text: str,
    title: str,
    desc: str,
    cfg: dict[str, Any],
) -> str:
    """视觉分析主入口：选择后端 → 分批 → 综合。"""
    prompt = PROMPT_ANALYSIS.format(
        title=title or "(无标题)",
        description=desc or "(无正文)",
        ocr_text=ocr_text or "(无 OCR 文字)",
    )
    backend = cfg.get("image_vision_backend", "api")
    if backend == "ollama":
        # 检查 Ollama 视觉支持
        url = cfg.get("ollama_url", "http://localhost:11434")
        model = cfg.get("ollama_model", "qwen2.5:7b")
        if not _ollama_supports_vision(url, model):
            _msg(f"Ollama 模型 {model} 不支持视觉，降级到 local 模式")
            return _analyze_local(ocr_text, title, desc)
        return _vision_ollama(image_paths, prompt, cfg, batch_size=5)
    elif backend == "mcp":
        return _vision_mcp(image_paths, ocr_text, title, desc, prompt)
    else:
        return _vision_api(image_paths, prompt, cfg, batch_size=3)


# ---------------------------------------------------------------------------
# Layer 3: Mermaid generation
# ---------------------------------------------------------------------------

def _generate_mermaid(image_summary: str, cfg: dict[str, Any]) -> str:
    """基于分析结果生成 Mermaid 图表代码。"""
    if not image_summary:
        return ""
    prompt = PROMPT_MERMAID.format(image_summary=image_summary)
    backend = cfg.get("image_vision_backend", "api")

    try:
        if backend == "mcp":
            return ""  # MCP 模式下不生成 Mermaid（由 Agent 后续处理）
        elif backend == "ollama":
            raw = _ollama_text_call(prompt, cfg)
        else:
            raw = _api_text_call(prompt, cfg)
    except Exception as e:
        _msg(f"Mermaid 生成失败: {e}")
        return ""

    # 解析 JSON 响应
    try:
        # 尝试提取 JSON（可能被 markdown 代码块包裹）
        json_match = re.search(r'\{[^{}]*"has_diagram"[^{}]*\}', raw, re.DOTALL)
        if not json_match:
            return ""
        result = json.loads(json_match.group())
        if not result.get("has_diagram"):
            return ""
        code = result.get("mermaid_code", "")
        if _validate_mermaid(code):
            return code
    except (json.JSONDecodeError, KeyError):
        pass

    # JSON 解析失败，尝试直接提取 Mermaid 代码
    mermaid_match = re.search(r'```mermaid\s*\n(.*?)```', raw, re.DOTALL)
    if mermaid_match:
        code = mermaid_match.group(1).strip()
        if _validate_mermaid(code):
            return code

    return ""


def _validate_mermaid(code: str) -> bool:
    """校验 Mermaid 代码基本合法性。"""
    if not code or not code.strip():
        return False
    stripped = code.strip()
    # 必须以合法关键字开头
    if not (stripped.startswith("graph ") or stripped.startswith("flowchart ")
            or stripped.startswith("sequenceDiagram")):
        return False
    # 节点数不超过 15
    nodes = set(re.findall(r'[A-Za-z_]\w*(?=\s*[\[\{(]|--|-->|===|---|\.\.>)', stripped))
    if len(nodes) > 15:
        return False
    # 不含明显非法内容
    if re.search(r'<script|javascript:|onerror|onload|onclick|onmouseover|data:text/html|vbscript:|<iframe|<embed|<object|expression\(|url\(|@import', stripped, re.IGNORECASE):
        return False
    return True


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def analyze_images(
    note_id: str,
    conn,
    cfg: dict[str, Any] | None = None,
    steps: list[str] | None = None,
) -> dict[str, Any]:
    """分析图文笔记的图片内容。

    steps: 控制执行哪些阶段，None 表示全部执行。
           ["ocr"]      → 仅 OCR
           ["vision"]   → 仅 AI 视觉（需先 OCR，用缓存）
           ["mermaid"]  → 仅 Mermaid（需先 vision，用缓存）
           支持任意组合，如 ["ocr", "vision"]
    """
    import xhs_storage
    import xhs_media

    cfg = cfg or load_config()
    all_steps = steps is None

    row = xhs_storage.get_note(conn, note_id)
    title = row["title"] if row else ""
    desc = row["description"] if row else ""

    result: dict[str, Any] = {
        "ocr_text": "",
        "image_summary": "",
        "mermaid": "",
        "image_count": 0,
    }

    # 缓存目录
    cache_dir = _image_cache_dir(note_id, conn)
    ocr_cache = cache_dir / "ocr.json"
    vision_cache = cache_dir / "vision.json"

    # 加载已有缓存
    if ocr_cache.exists():
        try:
            result["ocr_text"] = ocr_cache.read_text(encoding="utf-8")
        except Exception:
            pass
    if vision_cache.exists():
        try:
            d = json.loads(vision_cache.read_text(encoding="utf-8"))
            result["image_summary"] = d.get("summary", "")
            result["mermaid"] = d.get("mermaid", "")
        except Exception:
            pass

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

    # 2. Layer 1: OCR
    if all_steps or "ocr" in steps:
        _msg("OCR 识别中...")
        ocr_text = _do_ocr(image_paths)
        result["ocr_text"] = ocr_text
        # 缓存 OCR 结果
        cache_dir.mkdir(parents=True, exist_ok=True)
        ocr_cache.write_text(ocr_text, encoding="utf-8")
        if ocr_text:
            _msg(f"OCR 完成: {len(ocr_text)} 字（已缓存）")
        else:
            _msg("OCR 未识别到文字")
    else:
        # 使用缓存的 OCR
        ocr_text = result["ocr_text"]
        if ocr_text:
            _msg(f"使用缓存 OCR: {len(ocr_text)} 字")
        else:
            _msg("无 OCR 缓存，需先执行 ocr 步骤")

    # 3. Layer 2: AI 视觉 / 本地分析
    if all_steps or "vision" in steps:
        mode = cfg.get("image_mode", "auto")
        used_vision = False

        if mode == "auto":
            has_ai = _check_ai_backend_available(cfg)
            need_vision, reason = _should_use_vision(
                ocr_text, title, desc, len(image_paths), has_ai
            )
            _msg(f"auto 判断: {'需要 AI 视觉' if need_vision else 'OCR 足够'}（{reason}）")
            if need_vision and has_ai:
                _msg("AI 视觉分析中...")
                try:
                    result["image_summary"] = _vision_analyze(
                        image_paths, ocr_text, title, desc, cfg
                    )
                    used_vision = True
                    _msg("AI 视觉分析完成")
                except Exception as e:
                    _msg(f"AI 视觉分析失败，降级到 local: {e}")
                    result["image_summary"] = _analyze_local(ocr_text, title, desc)
            elif need_vision and not has_ai:
                _msg("需要 AI 视觉但无可用后端，降级到 local")
                result["image_summary"] = _analyze_local(ocr_text, title, desc)
            else:
                _msg("OCR 内容充足，跳过 AI 分析")

        elif mode == "none":
            _msg("image_mode=none，跳过 AI 分析")
        elif mode == "local":
            _msg("本地文本分析中...")
            result["image_summary"] = _analyze_local(ocr_text, title, desc)
            _msg("本地分析完成")
        elif mode == "vision":
            _msg("AI 视觉分析中...")
            try:
                result["image_summary"] = _vision_analyze(
                    image_paths, ocr_text, title, desc, cfg
                )
                used_vision = True
                _msg("AI 视觉分析完成")
            except Exception as e:
                _msg(f"AI 视觉分析失败，降级到 local: {e}")
                result["image_summary"] = _analyze_local(ocr_text, title, desc)

        # 缓存视觉结果
        cache_dir.mkdir(parents=True, exist_ok=True)
        vision_cache.write_text(json.dumps({
            "summary": result["image_summary"],
            "mermaid": "",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        _msg("视觉分析已缓存")

    # 4. Layer 3: Mermaid
    if all_steps or "mermaid" in steps:
        if result["image_summary"]:
            _msg("Mermaid 图表生成中...")
            mermaid = _generate_mermaid(result["image_summary"], cfg)
            if mermaid:
                result["mermaid"] = mermaid
                _msg("Mermaid 图表已生成")
                # 更新缓存
                vision_cache.write_text(json.dumps({
                    "summary": result["image_summary"],
                    "mermaid": mermaid,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                _msg("未检测到路线/流程类内容，不生成图表")
        else:
            _msg("无视觉分析结果，跳过 Mermaid")

    # 清理缓存（仅在完整执行时）
    if all_steps and cache_dir.exists():
        try:
            import shutil
            shutil.rmtree(cache_dir, ignore_errors=True)
        except Exception:
            pass

    return result


def _image_cache_dir(note_id: str, conn) -> Path:
    """获取图片分析缓存目录。"""
    import xhs_storage
    media_dir = xhs_storage._find_media_dir(note_id, conn)
    return media_dir / "_cache"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(text: str) -> None:
    print(f"[IMAGE] {text}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# CLI command handlers (moved from xhs.py)
# ---------------------------------------------------------------------------

def cmd_analyze_images(args) -> int:
    """对已入库的图文笔记做图片智能分析：OCR + AI 视觉描述 + Mermaid 图表。"""
    import xhs_storage

    conn = xhs_storage.connect()
    try:
        row = xhs_storage.get_note(conn, args.note_id)
        if not row:
            print(f"[ERR] 笔记 {args.note_id} 不在 DB，请先抓取入库", file=sys.stderr)
            return 1

        title = row["title"] or "(无标题)"

        # 加载配置（CLI 参数覆盖配置文件）
        cfg = load_config()
        if args.mode:
            cfg["image_mode"] = args.mode
        if args.backend:
            cfg["image_vision_backend"] = args.backend
        if args.no_mermaid:
            cfg["image_mermaid"] = False

        # 检查依赖
        deps = deps_status()
        if not deps.get("rapidocr"):
            print("[WARN] rapidocr 未安装，OCR 不可用。pip install rapidocr-onnxruntime",
                  file=sys.stderr)

        # 视觉后端预检（vision/auto 模式时）
        mode = cfg.get("image_mode", "auto")
        if mode in ("vision", "auto"):
            backend = cfg.get("image_vision_backend", "api")
            if backend == "ollama":
                if not deps.get("ollama"):
                    print(f"[WARN] Ollama 不可达（{cfg.get('ollama_url', '')}）", file=sys.stderr)
                    print("  请确认: 1) Ollama 已安装 (https://ollama.com/download)", file=sys.stderr)
                    print("          2) Ollama 正在运行 (ollama serve)", file=sys.stderr)
                    if mode == "vision":
                        print("  image_mode=vision 且 Ollama 不可用，分析将降级到 local 模式",
                              file=sys.stderr)
            elif backend == "api":
                if not deps.get("api_key"):
                    print("[WARN] 未配置 API Key，AI 视觉不可用", file=sys.stderr)
                    print("  运行 python scripts/xhs.py setup-image 配置 API Key",
                          file=sys.stderr)
            elif backend == "mcp":
                print("[INFO] 使用 MCP 视觉后端（由 AI Agent 提供，零配置）")

        # 解析 --step 参数
        steps = None
        if hasattr(args, "step") and args.step is not None:
            valid_steps = {"ocr", "vision", "mermaid"}
            steps = [s for s in args.step if s in valid_steps]
            if not steps:
                print(f"[WARN] --step 无有效值，执行全部阶段", file=sys.stderr)
                steps = None
            else:
                print(f"[IMAGE] 分段模式: {' → '.join(steps)}", file=sys.stderr)

        # 执行分析
        print(f"[IMAGE] 开始分析《{title}》...", file=sys.stderr)
        result = analyze_images(args.note_id, conn, cfg, steps=steps)

        # 将结果存入 DB
        xhs_storage.update_image_analysis(
            conn,
            args.note_id,
            ocr_text=result.get("ocr_text", ""),
            summary=result.get("image_summary", ""),
            mermaid=result.get("mermaid", ""),
        )

        # 打印更新内容摘要
        is_partial = steps is not None
        if is_partial:
            print(f"\n[OK] 《{title}》分段完成 ({' → '.join(steps)})")
        else:
            print(f"\n[OK] 《{title}》图片分析完成:")
        updates = []
        ocr_text = result.get("ocr_text", "")
        if ocr_text:
            updates.append(f"图片OCR {len(ocr_text)} 字")
            print(f"     OCR 预览: {ocr_text[:100]}{'...' if len(ocr_text) > 100 else ''}")
        image_summary = result.get("image_summary", "")
        if image_summary:
            updates.append(f"AI 描述 {len(image_summary)} 字")
            print(f"     描述预览: {image_summary[:100]}{'...' if len(image_summary) > 100 else ''}")
        mermaid = result.get("mermaid", "")
        if mermaid:
            updates.append("路线图/流程图")
        image_count = result.get("image_count", 0)
        if image_count:
            updates.append(f"共 {image_count} 张图片")
        if not updates:
            print("     (无新内容)")
        else:
            print(f"     更新项: {'、'.join(updates)}")

        # 分段模式提示下一步
        if is_partial:
            all_stages = ["ocr", "vision", "mermaid"]
            done = set(steps)
            remaining = [s for s in all_stages if s not in done]
            if remaining:
                next_step = remaining[0]
                print(f"\n[NEXT] 下一步: python scripts/xhs.py analyze-images {args.note_id} --step {next_step}")
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


def cmd_setup_image(args) -> int:
    """交互式配置图片分析。"""
    cfg = load_config()

    # 检查依赖
    deps = deps_status()
    print("=== 图片分析依赖检查 ===")
    for k, v in deps.items():
        status = "OK" if v else "未安装"
        print(f"  {k}: {status}")

    # 分析模式选择
    print("\n=== 图片分析模式选择 ===")
    print("1) auto   — 自动判断：OCR 足够就跳过 AI，不够才调用（推荐）")
    print("2) none   — 仅 OCR 文字提取，不做 AI 分析（零 AI 依赖）")
    print("3) local  — OCR + jieba 本地文本分析（关键词提取、高频词统计）")
    print("4) vision — OCR + AI 视觉模型看图（每条笔记都调 AI，需 Ollama 或 API）")

    if args.mode:
        mode = args.mode
    else:
        try:
            choice = input("\n请选择 (1-4) [1]: ").strip() or "1"
            mode_map = {"1": "auto", "2": "none", "3": "local", "4": "vision"}
            mode = mode_map.get(choice, "none")
        except (EOFError, KeyboardInterrupt):
            mode = "none"

    cfg["image_mode"] = mode
    print(f"\n已选择: {mode}")

    # 视觉后端选择
    if mode == "vision":
        print("\n=== 视觉后端选择 ===")
        print("1) ollama — 本地 Ollama 视觉模型")
        print("2) api    — 远程 OpenAI 兼容 API（智谱/通义/硅基流动/DeepSeek 等）")
        print("3) mcp    — MCP 视觉工具（AI Agent 提供，零配置）")

        if args.backend:
            backend = args.backend
        else:
            try:
                choice = input("\n请选择 (1-3) [2]: ").strip() or "2"
                backend_map = {"1": "ollama", "2": "api", "3": "mcp"}
                backend = backend_map.get(choice, "api")
            except (EOFError, KeyboardInterrupt):
                backend = "api"

        cfg["image_vision_backend"] = backend
        print(f"\n已选择: {backend}")

        # API 配置
        if backend == "api":
            print("\n=== API 配置（OpenAI 兼容格式）===")
            try:
                base_url = input(f"API Base URL (如 https://open.bigmodel.cn/api/paas/v4) []: ").strip()
                if base_url:
                    cfg["api_base_url"] = base_url
                key = input("API Key: ").strip()
                if key:
                    cfg["api_key"] = key
                model = input(f"模型 (如 glm-4v-plus, gpt-4o-mini) []: ").strip()
                if model:
                    cfg["api_model"] = model
            except (EOFError, KeyboardInterrupt):
                pass

        # Ollama 配置
        if backend == "ollama":
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

    # Mermaid 开关
    print("\n=== Mermaid 图表 ===")
    print("自动检测路线图/流程图并生成 Mermaid 代码（嵌入 Markdown）")
    if args.no_mermaid:
        cfg["image_mermaid"] = False
    else:
        try:
            mermaid_choice = input("开启 Mermaid 图表？(y/n) [y]: ").strip().lower()
            cfg["image_mermaid"] = mermaid_choice != "n"
        except (EOFError, KeyboardInterrupt):
            cfg["image_mermaid"] = True

    save_config(cfg)
    print(f"\n[OK] 图片分析配置已保存到 data/image_config.json")
    print(f"  image_mode: {cfg['image_mode']}")
    print(f"  image_vision_backend: {cfg.get('image_vision_backend', 'api')}")
    print(f"  image_mermaid: {cfg['image_mermaid']}")
    return 0
