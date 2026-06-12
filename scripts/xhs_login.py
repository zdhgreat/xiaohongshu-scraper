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
# Cookie 元数据辅助
# ---------------------------------------------------------------------------

_RICH_COOKIE_KEYS = ("domain", "path", "secure", "httpOnly", "sameSite", "expires")


def _build_cookie_result(
    raw_cookies: list[dict],
    domain_filter: str = "xiaohongshu",
) -> tuple[dict[str, str], dict[str, dict]]:
    """从 Playwright/rookiepy cookie 列表构建 (cookies, cookie_meta)。

    cookies: name → value（扁平字典，向后兼容）
    cookie_meta: name → {domain, path, secure, ...}（富元数据）
    """
    cookies: dict[str, str] = {}
    cookie_meta: dict[str, dict] = {}
    for c in raw_cookies:
        if domain_filter and domain_filter not in c.get("domain", ""):
            continue
        name = c["name"]
        cookies[name] = c["value"]
        meta = {k: c[k] for k in _RICH_COOKIE_KEYS if k in c and c[k] is not None}
        if meta:
            cookie_meta[name] = meta
    return cookies, cookie_meta


def _build_sqlite_cookie_meta(
    host: str, path: str, secure: bool | int,
    httponly: bool | int, samesite: int | str | None,
    expiry: int | float | None,
) -> dict:
    """从 SQLite 行字段构建 cookie 元数据字典。"""
    meta: dict = {"domain": host, "path": path}
    if int(secure):
        meta["secure"] = True
    if int(httponly):
        meta["httpOnly"] = True
    if samesite is not None:
        if isinstance(samesite, int):
            meta["sameSite"] = {0: "None", 1: "Lax", 2: "Strict"}.get(samesite, "Lax")
        elif samesite:
            meta["sameSite"] = str(samesite)
    if expiry and float(expiry) > 0:
        meta["expires"] = float(expiry)
    return meta


# ---------------------------------------------------------------------------
# WSL 档（动态导入，避免非 WSL 环境强制依赖 cryptography）
# ---------------------------------------------------------------------------

def acquire_via_wsl_browser_cdp(browser: str = "edge") -> tuple[dict[str, str], dict[str, dict]]:
    _ensure_scripts_path()
    try:
        import xhs_login_wsl  # type: ignore
    except ImportError as e:
        raise LoginError(f"无法加载 WSL 模块：{e}")
    try:
        return xhs_login_wsl.acquire_from_wsl_browser_cdp(browser)
    except xhs_login_wsl.WslLoginError as e:
        raise LoginError(f"WSL {browser} CDP 登录失败：{e}")


def acquire_via_wsl_browser(browser: str = "edge") -> tuple[dict[str, str], dict[str, dict]]:
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


def persist_cookies(cookies: dict[str, str], cookie_meta: dict[str, dict] | None = None) -> Path:
    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"_version": 2, "cookies": cookies, "cookie_meta": cookie_meta or {}}
    COOKIES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _restrict_file(COOKIES_PATH)
    return COOKIES_PATH


def load_cookies() -> tuple[dict[str, str], dict[str, dict]] | None:
    if not COOKIES_PATH.exists():
        return None
    try:
        raw = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("_version") == 2:
            return raw.get("cookies", {}), raw.get("cookie_meta", {})
        # v1: flat dict
        return raw, {}
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
    # 注意：不返回假 user_id，调用方应使用在线验证获取真实 user_id
    return {"via": "weak-check"}


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
        "user-agent": fingerprint.user_agent if fingerprint else xhs_config.USER_AGENT,
        **sign_headers,
    }

    try:
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=15)
    except Exception as e:
        print(f"[VALIDATE] 网络异常：{e}", file=sys.stderr)
        return None, None, cookies  # 网络问题，返回 None 表示未知状态

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

def acquire_via_rookiepy() -> tuple[dict[str, str], dict[str, dict]]:
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
        cookies, cookie_meta = _build_cookie_result(raw, domain_filter="xiaohongshu")
        if REQUIRED_KEYS.issubset(cookies.keys()):
            print(f"[LOGIN] rookiepy {name}: 提取 {len(cookies)} 个 cookie", file=sys.stderr)
            return cookies, cookie_meta
    raise LoginError("rookiepy 未在任何浏览器找到完整的小红书 cookie（需要先在浏览器登录过）")


# ---------------------------------------------------------------------------
# 档位 2: Playwright QR
# ---------------------------------------------------------------------------

def acquire_via_playwright_qr(headless: bool = False, timeout_s: int = 240,
                               profile_hint: str = "", channel: str = "") -> tuple[dict[str, str], dict[str, dict]]:
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
    browser_label = channel.title() if channel else "Chromium"
    print(f"[LOGIN] 启动 {browser_label}，请扫码并在手机 App 上点确认登录...", file=sys.stderr)
    with sync_playwright() as pw:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile),
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if channel:
            launch_kwargs["channel"] = channel
        browser = pw.chromium.launch_persistent_context(**launch_kwargs)
        try:
            page = browser.new_page()
            page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded")
            start = time.time()
            cookies: dict[str, str] = {}
            cookie_meta: dict[str, dict] = {}
            last_check = 0.0
            while time.time() - start < timeout_s:
                raw_ck = browser.cookies()
                ck, cm = _build_cookie_result(raw_ck, domain_filter="")
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
                            cookie_meta = cm
                            break
                        # 否则继续等待用户在手机 App 上点"确认"
                time.sleep(2)
        finally:
            browser.close()
        if not cookies:
            raise LoginError(f"{timeout_s}s 内未完成登录确认（扫码后请在手机 App 上点'确认登录'）")
        print(f"[LOGIN] QR 登录确认成功，{len(cookies)} 个 cookie", file=sys.stderr)
        return cookies, cookie_meta


# ---------------------------------------------------------------------------
# 档位 2.5: Profile Session 恢复（headless，无需人工）
# ---------------------------------------------------------------------------

def acquire_via_profile_restore(alias: str, timeout_s: int = 30) -> tuple[dict[str, str], dict[str, dict]]:
    """利用 Playwright persistent profile 的 session 恢复能力获取 cookie。

    与 acquire_via_playwright_qr 的区别：
    - 完全 headless，不需要用户扫码
    - 超时更短（30s vs 240s）
    - 期望 profile 里已有有效 session

    前提：该 alias 已经通过 QR 登录过一次，建立了 pw_profile_<alias>。
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as e:
        raise LoginError("缺少 playwright。pip install playwright && playwright install chromium") from e

    from xhs_config import KEEPALIVE_PROFILE_TIMEOUT_S, KEEPALIVE_LOGIN_WAIT_S

    if not alias:
        raise LoginError("profile_restore 需要指定 alias")

    profile = DATA_DIR / f"pw_profile_{alias}"
    if not profile.exists() or not any(profile.iterdir()):
        raise LoginError(f"profile {profile.name} 不存在，需先运行 `login --name {alias}` 建立 profile")

    effective_timeout = timeout_s or KEEPALIVE_PROFILE_TIMEOUT_S
    wait_s = KEEPALIVE_LOGIN_WAIT_S

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = browser.new_page()
            page.goto("https://www.xiaohongshu.com/explore",
                      wait_until="domcontentloaded", timeout=15000)
            # 等待浏览器恢复 session
            page.wait_for_timeout(wait_s * 1000)

            # 检查 cookie 是否齐全
            raw_ck = browser.cookies()
            ck, cm = _build_cookie_result(raw_ck, domain_filter="")
            if not REQUIRED_KEYS.issubset(ck.keys()):
                raise LoginError("profile 中 session 已过期（cookie 不完整）")

            # 在线验证：非 guest 才是真登录
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

            if not is_logged_in:
                raise LoginError("profile 中 session 已过期（服务端验证为 guest）")

        finally:
            browser.close()

    print(f"[LOGIN] Profile 恢复成功（{alias}），{len(ck)} 个 cookie", file=sys.stderr)
    return ck, cm


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

def acquire_via_native_browser(browser: str = "edge") -> tuple[dict[str, str], dict[str, dict]]:
    """跨平台：用 Playwright 读取用户真实浏览器配置中的 cookie。

    支持 Windows / macOS / Linux 的 Edge 和 Chrome。
    WSL 不适用（由 wsl-* tier 处理）。
    返回 (cookies, cookie_meta)。
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
_NATIVE_TIERS = {"native-edge", "native-chrome", "native-firefox", "native-brave"}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def acquire_cookies(prefer: str = "auto", headless_qr: bool = False, profile_hint: str = "") -> tuple[dict[str, str], dict[str, dict]]:
    """多档 fallback。prefer ∈ {auto, rookie, edge, chrome, firefox, brave, native, wsl-edge, wsl-chrome, qr, manual}。

    auto：根据平台自动选择最优链（非 WSL 环境自动跳过 WSL tier）。
    profile_hint: 多账号时传入别名，QR 登录用独立 profile 避免冲突。
    返回 (cookies, cookie_meta)。
    """
    # 向后兼容旧名称
    compat = {"win-edge": "native-edge", "win-chrome": "native-chrome"}
    prefer = compat.get(prefer, prefer)

    _native_chain = ["native-edge", "native-chrome", "native-firefox", "native-brave"]
    _native_fb = ["rookie", "qr", "manual"]
    _wsl_chain = ["wsl-edge-cdp", "wsl-edge", "wsl-chrome-cdp", "wsl-chrome", "qr", "manual"]

    chain = {
        "auto":            ["rookie"] + _native_chain + _wsl_chain,
        "rookie":          ["rookie"] + _native_chain + _wsl_chain,
        "native":          _native_chain + _native_fb,
        "edge":            ["native-edge"] + _native_fb,
        "chrome":          ["native-chrome"] + _native_fb,
        "firefox":         ["native-firefox"] + _native_fb,
        "brave":           ["native-brave"] + _native_fb,
        "native-edge":     _native_chain + _native_fb,
        "native-chrome":   ["native-chrome"] + ["native-edge", "native-firefox", "native-brave"] + _native_fb,
        "native-firefox":  ["native-firefox"] + ["native-edge", "native-chrome", "native-brave"] + _native_fb,
        "native-brave":    ["native-brave"] + ["native-edge", "native-chrome", "native-firefox"] + _native_fb,
        "wsl-edge":        ["wsl-edge", "wsl-edge-cdp", "wsl-chrome-cdp", "wsl-chrome", "qr", "manual"],
        "wsl-edge-cdp":    ["wsl-edge-cdp", "wsl-edge", "wsl-chrome-cdp", "wsl-chrome", "qr", "manual"],
        "wsl-chrome":      ["wsl-chrome", "wsl-chrome-cdp", "wsl-edge-cdp", "wsl-edge", "qr", "manual"],
        "wsl-chrome-cdp":  ["wsl-chrome-cdp", "wsl-chrome", "wsl-edge-cdp", "wsl-edge", "qr", "manual"],
        "qr":              ["qr", "manual"],
        "manual":          ["manual"],
    }.get(prefer, ["rookie"] + _native_chain + _wsl_chain)

    # 非 WSL 环境自动跳过 WSL tier（避免无意义的错误输出）
    _plat = _current_platform()
    if _plat != "wsl":
        chain = [t for t in chain if t not in _WSL_TIERS]
    # WSL 环境跳过 native browser tier（WSL 无本地 Edge/Chrome）
    if _plat == "wsl":
        chain = [t for t in chain if t not in _NATIVE_TIERS]

    last_err: Exception | None = None
    for tier in chain:
        try:
            if tier in ("native-edge", "edge"):
                cookies, cookie_meta = acquire_via_native_browser("edge")
            elif tier in ("native-chrome", "chrome"):
                cookies, cookie_meta = acquire_via_native_browser("chrome")
            elif tier in ("native-firefox", "firefox"):
                cookies, cookie_meta = acquire_via_native_browser("firefox")
            elif tier in ("native-brave", "brave"):
                cookies, cookie_meta = acquire_via_native_browser("brave")
            elif tier == "rookie":
                cookies, cookie_meta = acquire_via_rookiepy()
            elif tier == "wsl-edge":
                cookies, cookie_meta = acquire_via_wsl_browser("edge")
            elif tier == "wsl-edge-cdp":
                cookies, cookie_meta = acquire_via_wsl_browser_cdp("edge")
            elif tier == "wsl-chrome":
                cookies, cookie_meta = acquire_via_wsl_browser("chrome")
            elif tier == "wsl-chrome-cdp":
                cookies, cookie_meta = acquire_via_wsl_browser_cdp("chrome")
            elif tier == "qr":
                cookies, cookie_meta = acquire_via_playwright_qr(headless=headless_qr, profile_hint=profile_hint)
            else:
                cookies = acquire_via_manual()
                cookie_meta = {}
        except LoginError as e:
            print(f"[LOGIN] tier={tier} 失败：{e}", file=sys.stderr)
            last_err = e
            continue
        return cookies, cookie_meta

    raise LoginError(f"所有登录档位均失败：{last_err}")


if __name__ == "__main__":
    ck, cm = acquire_cookies()
    persist_cookies(ck, cm)
    print(f"saved {len(ck)} cookies to {COOKIES_PATH}")
