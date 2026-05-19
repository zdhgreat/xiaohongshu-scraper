"""三档登录 fallback：rookiepy → Playwright QR → 手动粘贴。

Cookie 落盘到 data/cookies.json，权限 0600。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

from xhs_config import (
    COOKIES_PATH, REQUIRED_COOKIE_KEYS as REQUIRED_KEYS,
    restrict_file as _restrict_file, base_headers,
    ROOT, DATA_DIR,
)

# 保留模块级引用供外部使用（如 xhs.py 中的 xhs_login.COOKIES_PATH）
# COOKIES_PATH 和 REQUIRED_KEYS 现在从 xhs_config 导入


class LoginError(RuntimeError):
    pass


def _ensure_scripts_path() -> None:
    """幂等地将 scripts/ 加入 sys.path，避免重复插入。"""
    _dir = str(Path(__file__).resolve().parent)
    if _dir not in sys.path:
        sys.path.insert(0, _dir)


# ---------------------------------------------------------------------------
# WSL 档（动态导入，避免非 WSL 环境强制依赖 cryptography）
# ---------------------------------------------------------------------------

def acquire_via_wsl_browser_cdp(browser: str = "edge") -> dict[str, str]:
    _ensure_scripts_path()
    try:
        import xhs_login_wsl  # type: ignore
    except ImportError as e:
        raise LoginError(f"无法加载 WSL 模块：{e}")
    try:
        return xhs_login_wsl.acquire_from_wsl_browser_cdp(browser)
    except xhs_login_wsl.WslLoginError as e:
        raise LoginError(f"WSL {browser} CDP 登录失败：{e}")


def acquire_via_wsl_browser(browser: str = "edge") -> dict[str, str]:
    _ensure_scripts_path()
    try:
        import xhs_login_wsl  # type: ignore
    except ImportError as e:
        raise LoginError(f"无法加载 WSL 模块：{e}")
    try:
        return xhs_login_wsl.acquire_from_wsl_browser(browser)
    except xhs_login_wsl.WslLoginError as e:
        raise LoginError(f"WSL {browser} 登录失败：{e}")


def cookies_to_str(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def cookies_from_str(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def persist_cookies(cookies: dict[str, str]) -> Path:
    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    _restrict_file(COOKIES_PATH)
    return COOKIES_PATH


def load_cookies() -> dict[str, str] | None:
    if not COOKIES_PATH.exists():
        return None
    try:
        return json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def validate_cookies(cookies: dict[str, str]) -> dict[str, Any] | None:
    """调一个轻量接口验证 cookie 是否仍有效。返回用户信息 dict 或 None。

    这里用 /api/sns/web/v2/user/me。注意此接口在某些时段需要签名，
    所以失败也不一定代表 cookie 失效。MVP 期暂用 a1+web_session 存在性作为弱校验。
    """
    if not REQUIRED_KEYS.issubset(cookies.keys()):
        return None
    # 弱校验：键齐全就当有效，留给上层 Fetcher 在实际调用时判定真正过期
    return {"user_id": cookies.get("unread", ""), "via": "weak-check"}


def validate_cookies_online(cookies: dict[str, str], fingerprint=None) -> tuple[bool, dict[str, Any] | None, dict[str, str]]:
    """在线验证 cookie 是否有效。构造签名请求调 /api/sns/web/v2/user/me。

    fingerprint: 可选 FingerprintProfile，用于匹配账号的 UA。
    返回 (valid, user_info, updated_cookies)。
    """
    if not REQUIRED_KEYS.issubset(cookies.keys()):
        return False, None, cookies

    try:
        _ensure_scripts_path()
        import xhs_sign
        signer = xhs_sign.make_signer("embed-js")
    except Exception:
        # 签名不可用，降级为弱检查
        info = validate_cookies(cookies)
        return info is not None, info, cookies

    api = "/api/sns/web/v2/user/me"
    a1 = cookies.get("a1", "")
    try:
        sign_headers = signer.sign(api, "", a1, "GET")
    except Exception:
        info = validate_cookies(cookies)
        return info is not None, info, cookies

    url = "https://edith.xiaohongshu.com" + api
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://www.xiaohongshu.com",
        "referer": "https://www.xiaohongshu.com/",
        "user-agent": fingerprint.user_agent if fingerprint else (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        ),
        **sign_headers,
    }

    try:
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=15)
    except Exception as e:
        print(f"[VALIDATE] 网络异常：{e}", file=sys.stderr)
        return True, None, cookies  # 网络问题不判死

    # 同步 Set-Cookie
    updated = dict(cookies)
    if hasattr(resp, "cookies"):
        for k, v in resp.cookies.items():
            if v:
                updated[k] = v

    if resp.status_code != 200:
        return False, None, updated

    try:
        payload = resp.json()
    except ValueError:
        return False, None, updated

    if payload.get("code") == -100:
        return False, None, updated

    user_info = (payload.get("data") or {})
    return True, user_info, updated


# ---------------------------------------------------------------------------
# 档位 1: rookiepy
# ---------------------------------------------------------------------------

def acquire_via_rookiepy() -> dict[str, str]:
    try:
        import rookiepy  # type: ignore
    except ImportError as e:
        raise LoginError("缺少 rookiepy。pip install rookiepy") from e

    domains = ["xiaohongshu.com", ".xiaohongshu.com"]
    browsers = [
        ("edge", getattr(rookiepy, "edge", None)),
        ("chrome", getattr(rookiepy, "chrome", None)),
        ("firefox", getattr(rookiepy, "firefox", None)),
        ("brave", getattr(rookiepy, "brave", None)),
    ]
    for name, fn in browsers:
        if fn is None:
            continue
        try:
            raw = fn(domains)
        except Exception as e:
            print(f"[LOGIN] rookiepy {name}: {e}", file=sys.stderr)
            continue
        if not raw:
            continue
        cookies = {c["name"]: c["value"] for c in raw}
        if REQUIRED_KEYS.issubset(cookies.keys()):
            print(f"[LOGIN] rookiepy {name}: 提取 {len(cookies)} 个 cookie", file=sys.stderr)
            return cookies
    raise LoginError("rookiepy 未在任何浏览器找到完整的小红书 cookie（需要先在浏览器登录过）")


# ---------------------------------------------------------------------------
# 档位 2: Playwright QR
# ---------------------------------------------------------------------------

def acquire_via_playwright_qr(headless: bool = False, timeout_s: int = 240, profile_hint: str = "") -> dict[str, str]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as e:
        raise LoginError("缺少 playwright。pip install playwright && playwright install chromium") from e

    # 多账号时每个号用独立 profile，避免第二次扫码时已被第一个号登录
    if profile_hint:
        profile = DATA_DIR / f"pw_profile_{profile_hint}"
    else:
        profile = DATA_DIR / "pw_profile"
    profile.mkdir(parents=True, exist_ok=True)
    print("[LOGIN] 启动 Chromium，请扫码并在手机 App 上点确认登录...", file=sys.stderr)
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = browser.new_page()
            page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded")
            start = time.time()
            cookies: dict[str, str] = {}
            last_check = 0.0
            while time.time() - start < timeout_s:
                ck = {c["name"]: c["value"] for c in browser.cookies()}
                if REQUIRED_KEYS.issubset(ck.keys()):
                    # 调 user/me 校验是真登录还是 guest
                    now = time.time()
                    if now - last_check >= 3:
                        last_check = now
                        try:
                            is_logged_in = page.evaluate(
                                """async () => {
                                    const r = await fetch('/api/sns/web/v2/user/me', {credentials:'include'});
                                    const j = await r.json();
                                    return j && j.data && j.data.guest === false;
                                }"""
                            )
                        except Exception:
                            is_logged_in = False
                        if is_logged_in:
                            cookies = ck
                            break
                        # 否则继续等待用户在手机 App 上点"确认"
                time.sleep(2)
        finally:
            browser.close()
        if not cookies:
            raise LoginError(f"{timeout_s}s 内未完成登录确认（扫码后请在手机 App 上点'确认登录'）")
        print(f"[LOGIN] QR 登录确认成功，{len(cookies)} 个 cookie", file=sys.stderr)
        return cookies


# ---------------------------------------------------------------------------
# 档位 3: 手动粘贴
# ---------------------------------------------------------------------------

def acquire_via_manual() -> dict[str, str]:
    if not sys.stdin.isatty():
        raise LoginError("非交互环境无法使用手动粘贴登录。请使用 --prefer rookie 或 --prefer qr")
    print(
        "请在浏览器登录 xiaohongshu.com，打开 DevTools → Application → Cookies，\n"
        "复制整段 cookie 字符串（k1=v1; k2=v2; ...），粘贴后回车结束（双回车确认）：",
        file=sys.stderr,
    )
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    raw = " ".join(lines).strip()
    if not raw:
        raise LoginError("没有粘贴任何内容")
    cookies = cookies_from_str(raw)
    missing = REQUIRED_KEYS - cookies.keys()
    if missing:
        raise LoginError(f"粘贴的 cookie 缺少必需字段：{missing}")
    return cookies


# ---------------------------------------------------------------------------
# 档位 1.5: 跨平台原生浏览器 cookie 提取（无需 rookiepy）
# ---------------------------------------------------------------------------

def acquire_via_win_native(browser: str = "edge") -> dict[str, str]:
    """跨平台：用 Playwright 读取用户真实浏览器配置中的 cookie。

    支持 Windows / macOS / Linux 的 Edge 和 Chrome。
    WSL 不适用（由 wsl-* tier 处理）。
    已合并原 xhs_login_win_native.py（已删除）。
    """
    try:
        import xhs_login_native  # type: ignore
    except ImportError as e:
        raise LoginError(f"无法加载 cookie 提取模块：{e}")
    try:
        return xhs_login_native.extract_cookies(browser)
    except (FileNotFoundError, ValueError, OSError) as e:
        raise LoginError(f"{browser} cookie 提取失败：{e}")


# ---------------------------------------------------------------------------
# 平台检测
# ---------------------------------------------------------------------------

def _is_wsl() -> bool:
    """检测是否运行在 WSL 环境中。"""
    try:
        with open("/proc/version", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except (OSError, FileNotFoundError):
        return False


def _current_platform() -> str:
    """返回当前平台标识：'wsl' / 'windows' / 'macos' / 'linux'。"""
    if _is_wsl():
        return "wsl"
    import platform
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    return "linux"


# 平台特定 tier 名称集合
_WSL_TIERS = {"wsl-edge", "wsl-edge-cdp", "wsl-chrome", "wsl-chrome-cdp"}
_WIN_TIERS = {"win-edge", "win-chrome"}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def acquire_cookies(prefer: str = "auto", headless_qr: bool = False, profile_hint: str = "") -> dict[str, str]:
    """多档 fallback。prefer ∈ {auto, rookie, wsl-edge, wsl-chrome, qr, manual}。

    auto：根据平台自动选择最优链（非 WSL 环境自动跳过 WSL tier）。
    profile_hint: 多账号时传入别名，QR 登录用独立 profile 避免冲突。
    """
    chain = {
        "auto":          ["win-edge", "win-chrome", "rookie", "wsl-edge-cdp", "wsl-edge", "wsl-chrome-cdp", "wsl-chrome", "qr", "manual"],
        "rookie":        ["rookie", "win-edge", "win-chrome", "wsl-edge-cdp", "wsl-edge", "wsl-chrome-cdp", "wsl-chrome", "qr", "manual"],
        "win-edge":      ["win-edge", "win-chrome", "qr", "manual"],
        "win-chrome":    ["win-chrome", "win-edge", "qr", "manual"],
        "wsl-edge":      ["wsl-edge", "wsl-edge-cdp", "wsl-chrome-cdp", "wsl-chrome", "qr", "manual"],
        "wsl-edge-cdp":  ["wsl-edge-cdp", "wsl-edge", "wsl-chrome-cdp", "wsl-chrome", "qr", "manual"],
        "wsl-chrome":    ["wsl-chrome", "wsl-chrome-cdp", "wsl-edge-cdp", "wsl-edge", "qr", "manual"],
        "wsl-chrome-cdp":["wsl-chrome-cdp", "wsl-chrome", "wsl-edge-cdp", "wsl-edge", "qr", "manual"],
        "qr":            ["qr", "manual"],
        "manual":        ["manual"],
    }.get(prefer, ["win-edge", "win-chrome", "rookie", "wsl-edge-cdp", "wsl-edge", "wsl-chrome-cdp", "wsl-chrome", "qr", "manual"])

    # 非 WSL 环境自动跳过 WSL tier（避免无意义的错误输出）
    platform = _current_platform()
    if platform != "wsl":
        chain = [t for t in chain if t not in _WSL_TIERS]
    # WSL 环境跳过 native browser tier（WSL 无本地 Edge/Chrome）
    if platform == "wsl":
        chain = [t for t in chain if t not in _WIN_TIERS]

    last_err: Exception | None = None
    for tier in chain:
        try:
            if tier == "win-edge":
                cookies = acquire_via_win_native("edge")
            elif tier == "win-chrome":
                cookies = acquire_via_win_native("chrome")
            elif tier == "rookie":
                cookies = acquire_via_rookiepy()
            elif tier == "wsl-edge":
                cookies = acquire_via_wsl_browser("edge")
            elif tier == "wsl-edge-cdp":
                cookies = acquire_via_wsl_browser_cdp("edge")
            elif tier == "wsl-chrome":
                cookies = acquire_via_wsl_browser("chrome")
            elif tier == "wsl-chrome-cdp":
                cookies = acquire_via_wsl_browser_cdp("chrome")
            elif tier == "qr":
                cookies = acquire_via_playwright_qr(headless=headless_qr, profile_hint=profile_hint)
            else:
                cookies = acquire_via_manual()
        except LoginError as e:
            print(f"[LOGIN] tier={tier} 失败：{e}", file=sys.stderr)
            last_err = e
            continue
        return cookies

    raise LoginError(f"所有登录档位均失败：{last_err}")


if __name__ == "__main__":
    ck = acquire_cookies()
    persist_cookies(ck)
    print(f"saved {len(ck)} cookies to {COOKIES_PATH}")
