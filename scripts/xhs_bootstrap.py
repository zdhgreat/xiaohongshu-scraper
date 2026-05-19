"""Auto-bootstrap: 首次运行自动安装所有依赖，让爬虫开箱即用。

由 xhs.py 在 import 其他模块之前调用，确保：
  1. Python 依赖（pip install -r requirements.txt）
  2. Node.js（签名引擎 PyExecJS 需要）
  3. crypto-js（npm install，签名算法需要）
  4. Playwright + Chromium（QR 登录 / 浏览器接管 / PlaywrightSigner）

幂等：已安装的组件会跳过，首次运行约 2-5 分钟（取决于网络）。
"""

from __future__ import annotations

import importlib
import os
import platform as _platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
ASSETS = ROOT / "assets"
DATA = ROOT / "data"

# 标记文件：已完成的安装步骤会跳过
_PIP_MARKER = DATA / ".pip_ok"
_PW_MARKER = DATA / ".pw_ok"


def _msg(text: str) -> None:
    print(f"[SETUP] {text}", file=sys.stderr, flush=True)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=600, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 1, "", f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, "", "timeout")


def _in_venv() -> bool:
    return (hasattr(sys, "real_prefix")
            or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix))


# ----------------------------------------------------------------
# 1. Python packages
# ----------------------------------------------------------------

def _pip_marker_valid() -> bool:
    if not _PIP_MARKER.exists():
        return False
    if not REQUIREMENTS.exists():
        return True
    try:
        return _PIP_MARKER.stat().st_mtime > REQUIREMENTS.stat().st_mtime
    except OSError:
        return False


def _install_pip() -> None:
    if not REQUIREMENTS.exists():
        return
    _msg("安装 Python 依赖（首次运行，约 30-60 秒）...")

    cmd_base = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    strategies: list[list[str]] = [cmd_base]
    if not _in_venv():
        strategies.append(cmd_base + ["--user"])
        strategies.append(cmd_base + ["--break-system-packages"])

    for s in strategies:
        r = _run(s)
        if r.returncode == 0:
            DATA.mkdir(exist_ok=True)
            _PIP_MARKER.touch()
            _msg("Python 依赖安装完成")
            importlib.invalidate_caches()
            return
        # PEP 668 externally-managed → 尝试下一种策略
        if "externally-managed-environment" not in (r.stderr or ""):
            # 非 PEP 668 错误，也试下一种
            continue

    _msg("pip install 失败。请手动运行: " + " ".join(strategies[0]))
    # 不写入 marker — 下次运行会重试


# ----------------------------------------------------------------
# 2. Node.js
# ----------------------------------------------------------------

def _find_node() -> str | None:
    """查找 node 可执行文件，必要时刷新 PATH。"""
    n = shutil.which("node")
    if n:
        return n
    # nodejs（某些 Linux 发行版把二进制叫 nodejs）
    n = shutil.which("nodejs")
    if n:
        return n

    # 新安装的 Node 可能还没进入当前进程 PATH，检查常见路径
    candidates: list[Path] = []
    if sys.platform == "win32":
        for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_key, "")
            if base:
                candidates.append(Path(base) / "nodejs" / "node.exe")
        localapp = os.environ.get("LOCALAPPDATA", "")
        if localapp:
            candidates.append(Path(localapp) / "Programs" / "nodejs" / "node.exe")
    else:
        candidates = [
            Path("/usr/local/bin/node"),
            Path("/opt/homebrew/bin/node"),
            Path("/usr/bin/node"),
            Path("/snap/bin/node"),
        ]
    for p in candidates:
        if p.is_file():
            os.environ["PATH"] = str(p.parent) + os.pathsep + os.environ.get("PATH", "")
            return str(p)
    return None


def _find_npm() -> str | None:
    n = shutil.which("npm")
    if n:
        return n
    node = _find_node()
    if node:
        npm_name = "npm.cmd" if sys.platform == "win32" else "npm"
        npm_path = Path(node).parent / npm_name
        if npm_path.is_file():
            return str(npm_path)
    return None


def _refresh_windows_path() -> None:
    """从 Windows 注册表刷新 PATH（刚装完 Node 后当前进程看不到）。"""
    if sys.platform != "win32":
        return
    try:
        import winreg  # noqa
        parts: list[str] = []
        for hive, key_path in [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment"),
        ]:
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    parts.append(winreg.QueryValueEx(key, "Path")[0])
            except OSError:
                pass
        if parts:
            os.environ["PATH"] = ";".join(parts) + ";" + os.environ.get("PATH", "")
    except Exception:
        pass


def _install_node() -> None:
    system = _platform.system()
    _msg(f"安装 Node.js（{system}）...")

    ok = False
    if system == "Windows":
        r = _run(["winget", "install", "OpenJS.NodeJS",
                  "--accept-source-agreements", "--accept-package-agreements"])
        if r.returncode == 0:
            _refresh_windows_path()
            ok = True
        else:
            # winget 可能不可用，试 choco
            r2 = _run(["choco", "install", "nodejs", "-y"])
            if r2.returncode == 0:
                _refresh_windows_path()
                ok = True

    elif system == "Darwin":
        r = _run(["brew", "install", "node"])
        if r.returncode == 0:
            ok = True

    else:  # Linux / WSL
        for cmd in [
            ["sudo", "apt-get", "install", "-y", "nodejs"],
            ["sudo", "yum", "install", "-y", "nodejs"],
            ["sudo", "dnf", "install", "-y", "nodejs"],
            ["sudo", "snap", "install", "node", "--classic"],
        ]:
            r = _run(cmd)
            if r.returncode == 0:
                ok = True
                break

    if ok:
        if _find_node():
            _msg("Node.js 安装完成")
        else:
            _msg("Node.js 已安装但当前终端未识别，请重启终端后重试")
    else:
        _msg("自动安装 Node.js 失败，请手动安装: https://nodejs.org")
        _msg("  Windows: winget install OpenJS.NodeJS")
        _msg("  macOS:   brew install node")
        _msg("  Linux:   sudo apt install nodejs")


# ----------------------------------------------------------------
# 3. npm deps (crypto-js)
# ----------------------------------------------------------------

def _has_crypto_js() -> bool:
    return (ASSETS / "node_modules" / "crypto-js").is_dir()


def _install_npm() -> None:
    npm = _find_npm()
    if not npm:
        _msg("npm 未找到，跳过 crypto-js 安装")
        return
    _msg("安装 crypto-js (npm)...")
    r = _run([npm, "install"], cwd=str(ASSETS))
    if r.returncode == 0 and _has_crypto_js():
        _msg("crypto-js 安装完成")
    else:
        _msg(f"npm install 失败: {(r.stderr or '')[:200]}")


# ----------------------------------------------------------------
# 4. Playwright browser
# ----------------------------------------------------------------

def _install_playwright() -> None:
    if _PW_MARKER.exists():
        return
    try:
        import playwright  # type: ignore  # noqa: F401
    except ImportError:
        return  # playwright 未装（pip install 可能跳过了），不阻塞

    _msg("安装 Playwright Chromium 浏览器（约 100MB，首次运行）...")
    r = _run([sys.executable, "-m", "playwright", "install", "chromium"])
    if r.returncode == 0:
        _msg("Playwright Chromium 安装完成")
        DATA.mkdir(exist_ok=True)
        _PW_MARKER.touch()
        # Linux / WSL 安装系统依赖（需要 sudo，失败不阻塞）
        if _platform.system() == "Linux":
            r2 = _run(["sudo", sys.executable, "-m", "playwright",
                       "install-deps", "chromium"])
            if r2.returncode != 0:
                _msg("Playwright 系统依赖安装失败，可手动运行: "
                     "sudo playwright install-deps chromium")
    else:
        _msg(f"Playwright 安装失败（QR 登录不可用，其他功能正常）: "
             f"{(r.stderr or '')[:200]}")


# ----------------------------------------------------------------
# Main entry
# ----------------------------------------------------------------

def ensure_ready(force: bool = False) -> None:
    """检查并安装所有依赖。由 xhs.py 在启动时调用。

    force=True 时忽略标记文件，重新检查（用于 setup 子命令）。
    """
    if force:
        _PIP_MARKER.unlink(missing_ok=True)
        _PW_MARKER.unlink(missing_ok=True)

    try:
        # 1. Python packages
        if not _pip_marker_valid():
            _install_pip()

        # 2. Node.js
        if not _find_node():
            _install_node()

        # 3. crypto-js
        if _find_node() and not _has_crypto_js():
            _install_npm()

        # 4. Playwright browser
        _install_playwright()

        # 5. ffmpeg check (video analysis dependency)
        _check_ffmpeg()

        # 6. 安装后验证 — 检查关键包是否可导入
        _verify_optional_packages()
    except Exception as e:
        _msg(f"自动配置异常（不影响已有功能）: {e}")


def _check_ffmpeg() -> None:
    """检查 ffmpeg 是否可用，不可用时给出安装提示（不自动安装）。"""
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    _msg("ffmpeg 未安装 → 视频分析（语音转文字/关键帧提取）不可用")
    _msg("  安装方式: Windows: winget install Gyan.FFmpeg | macOS: brew install ffmpeg | Linux: sudo apt install ffmpeg")
    _msg("  提示: 如果不需要视频分析，可以忽略此提示")


# 可选包验证列表: (import_name, display_name, feature)
_OPTIONAL_PACKAGES = [
    ("curl_cffi", "curl_cffi", "反风控 Chrome TLS 模拟"),
    ("execjs", "PyExecJS", "签名引擎"),
    ("cryptography", "cryptography", "WSL cookie 解密"),
    ("jieba", "jieba", "图片/视频 本地文本分析"),
    ("rapidocr_onnxruntime", "rapidocr-onnxruntime", "图片 OCR 文字识别 + 视频帧 OCR"),
    ("faster_whisper", "faster-whisper", "视频语音转文字"),
    ("snownlp", "snownlp", "评论情感分析"),
    ("playwright", "playwright", "QR 登录 / 浏览器接管"),
]


def _verify_optional_packages() -> None:
    """安装后验证：检查关键包是否可导入，报告缺失。"""
    missing = []
    for mod_name, display_name, feature in _OPTIONAL_PACKAGES:
        try:
            __import__(mod_name)
        except ImportError:
            missing.append((display_name, feature))
    if missing:
        _msg(f"有 {len(missing)} 个可选包未就绪（不影响基础功能，按需安装）:")
        for name, feature in missing:
            _msg(f"  {name:30s} → {feature}")


# ---------------------------------------------------------------------------
# CLI command handlers (moved from xhs.py)
# ---------------------------------------------------------------------------

def cmd_setup(args) -> int:
    """手动触发环境安装（正常使用时会自动触发，此命令用于排查问题）。"""
    ensure_ready(force=True)

    # ── 核心依赖验证 ──
    print("=== 核心依赖 ===", file=sys.stderr)
    ok = True
    core_deps = [
        ("curl_cffi", "curl_cffi", "反风控 Chrome TLS 模拟"),
        ("execjs", "PyExecJS", "签名引擎"),
        ("cryptography", "cryptography", "WSL cookie 解密"),
    ]
    for mod_name, display_name, feature in core_deps:
        try:
            __import__(mod_name)
            print(f"  OK  {display_name:30s} {feature}", file=sys.stderr)
        except ImportError:
            print(f"  MISS {display_name:30s} {feature}", file=sys.stderr)
            ok = False

    # Node.js + crypto-js
    if _find_node():
        print(f"  OK  {'Node.js':30s} 签名引擎需要", file=sys.stderr)
    else:
        print(f"  MISS {'Node.js':30s} 签名引擎需要", file=sys.stderr)
        ok = False
    if _has_crypto_js():
        print(f"  OK  {'crypto-js':30s} 签名算法核心", file=sys.stderr)
    else:
        print(f"  MISS {'crypto-js':30s} 签名算法核心", file=sys.stderr)
        ok = False

    # Playwright
    try:
        import playwright  # noqa: F401
        print(f"  OK  {'playwright':30s} QR 登录 / 浏览器接管", file=sys.stderr)
    except ImportError:
        print(f"  MISS {'playwright':30s} QR 登录 / 浏览器接管（可选）", file=sys.stderr)

    # ── 图片/视频分析依赖验证 ──
    print("\n=== 图片 & 视频分析（可选） ===", file=sys.stderr)
    analysis_deps = [
        ("jieba", "jieba", "本地文本分析（关键词提取）"),
        ("rapidocr_onnxruntime", "rapidocr-onnxruntime", "图片/视频 OCR 文字识别"),
        ("faster_whisper", "faster-whisper", "视频语音转文字"),
        ("snownlp", "snownlp", "评论情感分析"),
    ]
    for mod_name, display_name, feature in analysis_deps:
        try:
            __import__(mod_name)
            print(f"  OK  {display_name:30s} {feature}", file=sys.stderr)
        except ImportError:
            print(f"  MISS {display_name:30s} {feature}", file=sys.stderr)

    # ffmpeg
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            print(f"  OK  {'ffmpeg':30s} 视频音频处理", file=sys.stderr)
        else:
            raise FileNotFoundError
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print(f"  MISS {'ffmpeg':30s} 视频分析需要（需手动安装）", file=sys.stderr)

    # ── 配置引导提示 ──
    print("\n=== 下一步 ===", file=sys.stderr)
    print("  配置图片/视频分析: python scripts/xhs.py setup-wizard", file=sys.stderr)
    print("  仅配置图片分析:   python scripts/xhs.py setup-image", file=sys.stderr)
    print("  仅配置视频分析:   python scripts/xhs.py setup-video", file=sys.stderr)
    return 0 if ok else 1


def _prompt_ollama_config(cfg: dict, prefix: str = "ollama_") -> None:
    """交互输入 Ollama 配置，实时验证连接和模型。"""
    url_key = f"{prefix}url"
    model_key = f"{prefix}model"
    try:
        url = input(f"  Ollama URL [{cfg.get(url_key, 'http://localhost:11434')}]: ").strip()
        if url:
            cfg[url_key] = url
        url = cfg.get(url_key, "http://localhost:11434")

        # ── 验证 1: Ollama 是否在运行 ──
        print("  正在检查 Ollama 连接...", end=" ", flush=True)
        try:
            import requests as _req
            resp = _req.get(f"{url}/api/tags", timeout=5)
            if resp.status_code != 200:
                print("失败")
                print("  [错误] Ollama 未响应。请确认 Ollama 已安装并正在运行：")
                print("    安装: https://ollama.com/download")
                print("    启动: ollama serve")
                return
            print("OK")
        except Exception as e:
            print(f"失败 ({type(e).__name__})")
            print("  [错误] 无法连接 Ollama。请确认：")
            print("    1. 已安装 Ollama: https://ollama.com/download")
            print("    2. Ollama 正在运行: ollama serve")
            print(f"    3. URL 正确: {url}")
            return

        # ── 列出已安装的模型 ──
        try:
            models = resp.json().get("models", [])
            if models:
                names = [m["name"] for m in models]
                vision_keywords = ("llava", "qwen2-vl", "minicpm-v", "bakllava", "llama3.2-vision")
                print(f"  已安装模型 ({len(names)}):")
                for n in names:
                    is_vision = any(kw in n.lower() for kw in vision_keywords)
                    tag = " [视觉]" if is_vision else ""
                    print(f"    - {n}{tag}")
            else:
                print("  [警告] Ollama 中无已安装模型")
        except Exception:
            pass

        print("  常用视觉模型: qwen2-vl:7b, llava:13b, minicpm-v:8b")
        model = input(f"  模型 [{cfg.get(model_key, 'qwen2-vl:7b')}]: ").strip()
        if model:
            cfg[model_key] = model
        model = cfg.get(model_key, "qwen2-vl:7b")

        # ── 验证 2: 模型是否已安装 ──
        print(f"  正在检查模型 {model}...", end=" ", flush=True)
        installed_names = [m["name"] for m in resp.json().get("models", [])]
        model_installed = any(model in n or n.startswith(model.split(":")[0]) for n in installed_names)
        if not model_installed:
            print("未安装")
            print(f"  [提示] 模型 {model} 未安装。需要先拉取：")
            print(f"    ollama pull {model}")
            try:
                pull = input(f"  是否现在拉取？(y/n) [y]: ").strip().lower()
                if pull != "n":
                    print(f"  正在拉取 {model}（首次下载需要几分钟）...")
                    subprocess.run(["ollama", "pull", model], timeout=600)
                    print("  拉取完成")
            except (EOFError, KeyboardInterrupt, Exception):
                pass
        else:
            print("已安装")

        # ── 验证 3: 模型是否支持视觉 ──
        print(f"  正在检查视觉能力...", end=" ", flush=True)
        try:
            import xhs_video
            if xhs_video._ollama_supports_vision(url, model):
                print("OK（支持视觉）")
            else:
                print("不支持")
                print(f"  [警告] 模型 {model} 可能不支持视觉输入。")
                print("  建议换用视觉模型: qwen2-vl:7b, llava:13b, minicpm-v:8b")
        except Exception:
            print("无法检测")

    except (EOFError, KeyboardInterrupt):
        pass


def _prompt_api_config(cfg: dict, prefix: str = "api_", default_url: str = "") -> None:
    """交互输入 API 配置（支持多种服务商），实时验证连接。"""
    if prefix == "api_":
        url_key = "api_base_url"
        key_key = "api_key"
        model_key = "api_model"
    elif prefix == "openai_":
        url_key = "openai_base_url"
        key_key = "openai_api_key"
        model_key = "openai_model"
    else:
        url_key = f"{prefix}base_url"
        key_key = f"{prefix}key"
        model_key = f"{prefix}model"

    print("  常用 API 服务商：")
    print("    智谱 GLM-4V:   https://open.bigmodel.cn/api/paas/v4")
    print("    通义 Qwen-VL:  https://dashscope.aliyuncs.com/compatible-mode/v1")
    print("    硅基流动:       https://api.siliconflow.cn/v1")
    print("    DeepSeek:       https://api.deepseek.com/v1")
    print("    OpenAI:         https://api.openai.com/v1")
    try:
        base_url = input(f"  API Base URL [{cfg.get(url_key, default_url)}]: ").strip()
        if base_url:
            cfg[url_key] = base_url
        key = input("  API Key: ").strip()
        if key:
            cfg[key_key] = key
        model = input(f"  模型 [{cfg.get(model_key, '')}]: ").strip()
        if model:
            cfg[model_key] = model

        # ── 验证 API 连通性 ──
        api_key = cfg.get(key_key, "")
        api_url = cfg.get(url_key, "") or default_url
        if api_key and api_url:
            print("  正在验证 API 连接...", end=" ", flush=True)
            try:
                import requests as _req
                resp = _req.get(
                    f"{api_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    model_list = data.get("data", [])
                    print(f"OK（可用模型 {len(model_list)} 个）")
                elif resp.status_code == 401:
                    print("失败")
                    print("  [错误] API Key 无效，请检查")
                else:
                    print(f"响应 {resp.status_code}")
                    print(f"  [提示] API 返回非预期状态码，可能配置有误")
            except Exception as e:
                print(f"失败 ({type(e).__name__})")
                print(f"  [提示] 无法连接 {api_url}，请检查 URL 和网络")
        elif not api_key:
            print("  [警告] 未填写 API Key，分析时将无法调用 API")
    except (EOFError, KeyboardInterrupt):
        pass


def _configure_image(ai_choice: str) -> None:
    """图片分析子向导。"""
    import xhs_image

    cfg = xhs_image.load_config()

    # 依赖检查
    deps = xhs_image.deps_status()
    dep_status = "  ".join(f"{'OK' if v else '未安装'}" for k, v in deps.items())
    print(f"依赖状态: {dep_status}")
    if not deps.get("rapidocr"):
        print("  [提示] rapidocr 未安装 → OCR 不可用")
        print("  安装: pip install rapidocr-onnxruntime")

    if ai_choice == "A":
        cfg["image_mode"] = "auto"
        print("\n已选择: auto 模式（自动判断，优先 OCR，必要时才调 AI）")
        print("  [提示] 当前无 AI 后端，auto 模式下只做 OCR")
    elif ai_choice == "B":
        cfg["image_mode"] = "auto"
        cfg["image_vision_backend"] = "ollama"
        print("\n已选择: auto 模式 + Ollama 视觉后端")
        print("  → OCR 足够的笔记自动跳过 AI，需要时才调用 Ollama 看图")
        _prompt_ollama_config(cfg, prefix="ollama_")
    elif ai_choice == "C":
        cfg["image_mode"] = "auto"
        cfg["image_vision_backend"] = "api"
        print("\n已选择: auto 模式 + API 视觉后端")
        print("  → OCR 足够的笔记自动跳过 AI，需要时才调用 API 看图")
        _prompt_api_config(cfg, prefix="api_")
    elif ai_choice == "D":
        cfg["image_mode"] = "auto"
        cfg["image_vision_backend"] = "mcp"
        print("\n已选择: auto 模式 + MCP 视觉后端")
        print("  → OCR 足够的笔记自动跳过 AI，需要时输出 MCP 任务清单")
        print("  → AI Agent 使用当前环境的 MCP 视觉工具完成分析，零配置")

    # Mermaid 开关
    if ai_choice in ("B", "C"):
        try:
            mermaid = input("\n开启 Mermaid 路线图/流程图？(y/n) [y]: ").strip().lower()
            cfg["image_mermaid"] = mermaid != "n"
        except (EOFError, KeyboardInterrupt):
            cfg["image_mermaid"] = True
    else:
        cfg["image_mermaid"] = False

    xhs_image.save_config(cfg)
    print(f"[OK] 图片分析配置已保存 → data/image_config.json")


def _configure_video(ai_choice: str) -> None:
    """视频分析子向导。"""
    import xhs_video

    cfg = xhs_video.load_config()

    # 依赖检查
    deps = xhs_video.deps_status()
    dep_status = "  ".join(f"{'OK' if v else '未安装'}" for k, v in deps.items())
    print(f"依赖状态: {dep_status}")
    if not deps.get("ffmpeg"):
        print("  [提示] ffmpeg 未安装 → 视频分析不可用")
        print("  Windows: winget install Gyan.FFmpeg")
        print("  macOS:   brew install ffmpeg")
        print("  Linux:   sudo apt install ffmpeg")

    # Whisper 模型
    print("\nWhisper 语音转文字模型：")
    print("  base（推荐） / small / medium / large-v3")
    try:
        wm = input("选择 [base]: ").strip() or "base"
        if wm in ("tiny", "base", "small", "medium", "large-v3"):
            cfg["whisper_model"] = wm
    except (EOFError, KeyboardInterrupt):
        pass

    if ai_choice == "A":
        cfg["summary_mode"] = "none"
        print("\n已选择: 仅转录模式（不调用 AI）")
    elif ai_choice == "B":
        cfg["summary_mode"] = "ollama"
        print("\n已选择: Ollama 摘要模式")
        _prompt_ollama_config(cfg, prefix="ollama_")
    elif ai_choice == "C":
        cfg["summary_mode"] = "openai"
        print("\n已选择: API 摘要模式")
        _prompt_api_config(cfg, prefix="openai_", default_url="https://api.openai.com/v1")
    elif ai_choice == "D":
        cfg["summary_mode"] = "mcp"
        print("\n已选择: MCP 视觉工具模式")
        print("  → AI Agent 使用当前环境的 MCP 视觉工具完成视频分析，零配置")

    xhs_video.save_config(cfg)
    print(f"[OK] 视频分析配置已保存 → data/video_config.json")


def cmd_setup_wizard(args) -> int:
    """统一引导向导：帮助用户选择并配置图片分析和/或视频分析。"""
    print("=" * 60)
    print("  小红书爬虫 — 智能分析配置向导")
    print("=" * 60)
    print()
    print("本向导将帮你配置图片和视频的智能分析功能。")
    print()

    # ── 1. 总览 ──
    print("┌─────────────────────────────────────────────────────┐")
    print("│  可用的分析能力：                                     │")
    print("│                                                       │")
    print("│  图片分析：                                          │")
    print("│    Layer 1: OCR 文字提取（图片中的文字）             │")
    print("│    Layer 2: AI 视觉描述（看懂路线图/穿搭/步骤）       │")
    print("│    Layer 3: Mermaid 路线图/流程图                    │")
    print("│                                                       │")
    print("│  视频分析：                                          │")
    print("│    Layer 1: 语音转文字（faster-whisper）             │")
    print("│    Layer 2: 关键帧 OCR（画面文字）                   │")
    print("│    Layer 3: AI 摘要（none/local/ollama/openai）     │")
    print("└─────────────────────────────────────────────────────┘")
    print()

    # ── 2. 选择要配置的功能 ──
    print("请选择要配置的功能：")
    print("  1) 仅图片分析")
    print("  2) 仅视频分析")
    print("  3) 图片 + 视频分析（推荐）")
    print("  4) 跳过（保持当前配置）")
    try:
        choice = input("\n请选择 (1-4) [3]: ").strip() or "3"
    except (EOFError, KeyboardInterrupt):
        print("\n[OK] 已跳过配置")
        return 0

    do_image = choice in ("1", "3")
    do_video = choice in ("2", "3")

    if not do_image and not do_video:
        print("[OK] 已跳过配置")
        return 0

    # ── 3. 共享 AI 后端选择 ──
    print()
    print("=" * 60)
    print("  AI 后端选择（图片和视频共用）")
    print("=" * 60)
    print()
    print("  图片分析和视频分析都需要 AI 后端来理解内容。")
    print("  你可以选择：")
    print()
    print("  A) 不用 AI（仅 OCR/文字提取，零成本）")
    print("     → 图片只做文字识别，视频只做语音转文字")
    print()
    print("  B) 本地 AI 模型（Ollama，免费，需安装）")
    print("     → 需要安装 Ollama + 下载视觉模型（2-8GB）")
    print("     → 优点：免费、隐私、离线可用")
    print("     → 推荐模型：qwen2-vl:7b（中文视觉强）")
    print()
    print("  C) 云端 API（智谱/通义/硅基流动/DeepSeek/OpenAI）")
    print("     → 最灵活，效果最好，按量付费")
    print("     → 部分服务商有免费额度（智谱、硅基流动）")
    print()
    print("  D) MCP 视觉工具（AI Agent 提供，零配置）")
    print("     → 适配 Claude Code / GLM Coding / Cursor / Windsurf 等")
    print("     → 使用当前环境中的 MCP 视觉 Server（Gemini/OpenAI/HuggingFace 等）")
    print()
    try:
        ai_choice = input("请选择 AI 后端 (A/B/C/D) [A]: ").strip().upper() or "A"
    except (EOFError, KeyboardInterrupt):
        ai_choice = "A"

    # ── 4. 根据选择配置各模块 ──
    if do_image:
        print()
        print("=" * 60)
        print("  图片分析配置")
        print("=" * 60)
        _configure_image(ai_choice)

    if do_video:
        print()
        print("=" * 60)
        print("  视频分析配置")
        print("=" * 60)
        _configure_video(ai_choice)

    # ── 5. 最终确认 ──
    print()
    print("=" * 60)
    print("  配置完成！")
    print("=" * 60)
    print()
    print("常用命令：")
    print("  python scripts/xhs.py analyze-images <note_id>  # 分析图片")
    print("  python scripts/xhs.py analyze-video <note_id>   # 分析视频")
    print("  python scripts/xhs.py crawl-search '关键词' --download --analyze  # 批量")
    print()
    print("单独调整配置：")
    print("  python scripts/xhs.py setup-image   # 图片分析配置")
    print("  python scripts/xhs.py setup-video   # 视频分析配置")
    return 0
