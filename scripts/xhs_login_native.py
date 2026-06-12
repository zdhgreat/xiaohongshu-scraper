"""跨平台浏览器 cookie 提取。

工作原理（Chromium 系）：
  1. 定位浏览器用户配置目录（按平台不同）
  2. 检测浏览器是否在运行 → 暂时关闭（配置文件被锁）
  3. 用 Playwright channel 模式加载用户配置
  4. 通过 ctx.cookies() 读取明文 cookie（Playwright 内部处理解密）
  5. 提取完毕后重新打开浏览器

工作原理（Firefox）：
  1. 解析 profiles.ini 找到默认 profile 目录
  2. 复制 cookies.sqlite 到临时目录（避免锁冲突）
  3. 直接 sqlite3 查询（Firefox cookie 明文存储，无需解密）
  4. 无需关闭/重启浏览器

支持平台：Windows / macOS / Linux（不含 WSL）
支持浏览器：Edge / Chrome / Firefox / Brave
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REQUIRED_KEYS = {"web_session", "a1"}


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

# ── 浏览器用户配置目录（按平台） ──────────────────────────────────

_PROFILE_MAP: dict[str, dict[str, str]] = {
    "edge": {
        "win32":  os.path.join(os.environ.get("LOCALAPPDATA", ""),
                               "Microsoft", "Edge", "User Data"),
        "darwin": str(Path.home() / "Library" / "Application Support" / "Microsoft Edge"),
        "linux":  str(Path.home() / ".config" / "microsoft-edge"),
    },
    "chrome": {
        "win32":  os.path.join(os.environ.get("LOCALAPPDATA", ""),
                               "Google", "Chrome", "User Data"),
        "darwin": str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome"),
        "linux":  str(Path.home() / ".config" / "google-chrome"),
    },
    "firefox": {
        # Firefox 的父目录（含 profiles.ini），不是 profile 目录本身
        "win32":  os.path.join(os.environ.get("APPDATA", ""),
                               "Mozilla", "Firefox"),
        "darwin": str(Path.home() / "Library" / "Application Support" / "Firefox"),
        "linux":  str(Path.home() / ".mozilla" / "firefox"),
    },
    "brave": {
        "win32":  os.path.join(os.environ.get("LOCALAPPDATA", ""),
                               "BraveSoftware", "Brave-Browser", "User Data"),
        "darwin": str(Path.home() / "Library" / "Application Support"
                      / "BraveSoftware" / "Brave-Browser"),
        "linux":  str(Path.home() / ".config" / "BraveSoftware" / "Brave-Browser"),
    },
}

# Chromium 系浏览器的 Playwright channel 映射
_PLAYWRIGHT_CHANNEL: dict[str, str] = {
    "edge": "msedge",
    "chrome": "chrome",
    "brave": "brave",
}


def _profile_dir(browser: str) -> str | None:
    """返回当前平台下浏览器配置目录路径，不存在返回 None。

    对 Firefox，返回包含 profiles.ini 的父目录。
    """
    key = {"darwin": "darwin"}.get(sys.platform, "linux" if sys.platform != "win32" else "win32")
    path = _PROFILE_MAP.get(browser, {}).get(key)
    return path if path and os.path.isdir(path) else None


# ── Firefox profile 解析 ─────────────────────────────────────────

def _firefox_default_profile() -> str | None:
    """解析 profiles.ini 找到 Firefox 默认 profile 目录。"""
    parent = _profile_dir("firefox")
    if not parent:
        return None
    ini_path = os.path.join(parent, "profiles.ini")
    if not os.path.isfile(ini_path):
        return None
    sections: list[dict[str, str]] = []
    current: dict[str, str] = {}
    try:
        with open(ini_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    if current:
                        sections.append(current)
                    current = {}
                elif "=" in line:
                    k, v = line.split("=", 1)
                    current[k.strip().lower()] = v.strip()
        if current:
            sections.append(current)
    except Exception:
        return None
    # 策略 0: [Install*] section 的 Default 值（Firefox 真正使用的默认 profile）
    for i, line in enumerate(open(ini_path, encoding="utf-8")):
        pass  # 重新读取以保留 section 名
    with open(ini_path, encoding="utf-8") as f:
        lines = f.readlines()
    in_install = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[Install"):
            in_install = True
            continue
        elif stripped.startswith("["):
            in_install = False
        if in_install and stripped.lower().startswith("default="):
            val = stripped.split("=", 1)[1].strip()
            p = os.path.join(parent, val)
            if os.path.isdir(p):
                return p
    # 策略 1: Profile section 中 Default=1
    for sec in sections:
        if sec.get("default") == "1" and "path" in sec:
            p = os.path.join(parent, sec["path"])
            if os.path.isdir(p):
                return p
    # 策略 2: .default-release
    for sec in sections:
        path = sec.get("path", "")
        if ".default-release" in path:
            p = os.path.join(parent, path)
            if os.path.isdir(p):
                return p
    # 策略 3: 任意有 path 的 section
    for sec in sections:
        if "path" in sec:
            p = os.path.join(parent, sec["path"])
            if os.path.isdir(p):
                return p
    return None


# ── Firefox cookie 提取（直接读 sqlite） ──────────────────────────

def _extract_firefox_cookies() -> tuple[dict[str, str], dict[str, dict]]:
    """从 Firefox 的 cookies.sqlite 提取小红书 cookie。

    Firefox cookie 明文存储，无需解密，无需关闭浏览器。
    返回 (cookies, cookie_meta)。
    """
    profile = _firefox_default_profile()
    if not profile:
        raise FileNotFoundError(
            f"未找到 Firefox 默认配置文件（{sys.platform}）。"
            "请确认 Firefox 已安装并运行过。"
        )
    db_src = os.path.join(profile, "cookies.sqlite")
    if not os.path.isfile(db_src):
        raise FileNotFoundError(f"Firefox cookies.sqlite 不存在：{db_src}")

    # 复制到临时目录避免锁冲突
    with tempfile.TemporaryDirectory(prefix="xhs_ff_") as tmp:
        db_dst = os.path.join(tmp, "cookies.sqlite")
        shutil.copy2(db_src, db_dst)

        conn = sqlite3.connect(db_dst)
        try:
            now = int(time.time())
            cur = conn.execute(
                "SELECT name, value, host, path, isSecure, isHttpOnly, sameSite, expiry "
                "FROM moz_cookies "
                "WHERE (host LIKE '%xiaohongshu.com%' OR host LIKE '%xhscdn.com%') "
                "AND (expiry = 0 OR expiry > ?)",
                (now,),
            )
            cookies: dict[str, str] = {}
            cookie_meta: dict[str, dict] = {}
            for name, value, host, path, isSecure, isHttpOnly, sameSite, expiry in cur.fetchall():
                cookies[name] = value
                cookie_meta[name] = _build_sqlite_cookie_meta(
                    host, path, isSecure, isHttpOnly, sameSite, expiry)
        finally:
            conn.close()

    if not REQUIRED_KEYS.issubset(cookies.keys()):
        missing = REQUIRED_KEYS - cookies.keys()
        raise ValueError(
            f"Firefox 中小红书 cookie 不完整，缺少: {missing}。"
            f"请先在 Firefox 中打开 xiaohongshu.com 并登录。"
        )

    print(f"[NATIVE] 从 Firefox 提取到 {len(cookies)} 个 cookie",
          file=sys.stderr)
    return cookies, cookie_meta


# ── 浏览器进程管理（按平台） ──────────────────────────────────────

def _process_name(browser: str) -> str:
    """返回用于查找/关闭的进程名。"""
    if sys.platform == "win32":
        return {
            "edge": "msedge.exe", "chrome": "chrome.exe",
            "firefox": "firefox.exe", "brave": "brave.exe",
        }[browser]
    if sys.platform == "darwin":
        return {
            "edge": "Microsoft Edge", "chrome": "Google Chrome",
            "firefox": "firefox", "brave": "Brave Browser",
        }[browser]
    # Linux
    return {
        "edge": "msedge", "chrome": "google-chrome",
        "firefox": "firefox", "brave": "brave-browser",
    }[browser]


def _is_browser_running(browser: str) -> bool:
    proc = _process_name(browser)
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {proc}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return proc.lower() in r.stdout.lower()
        else:
            r = subprocess.run(
                ["pgrep", "-x", proc],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0
    except Exception:
        return False


def _close_browser(browser: str) -> None:
    proc = _process_name(browser)
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/IM", proc, "/F"],
                           capture_output=True, timeout=15)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e", f'quit app "{proc}"'],
                           capture_output=True, timeout=10)
        else:
            subprocess.run(["pkill", "-x", proc],
                           capture_output=True, timeout=10)
    except Exception:
        pass
    time.sleep(2)


def _reopen_browser(browser: str) -> None:
    try:
        if sys.platform == "win32":
            exe = _process_name(browser)
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            subprocess.Popen(["cmd", "/c", "start", "", exe],
                             creationflags=flags)
        elif sys.platform == "darwin":
            app = _process_name(browser)
            subprocess.Popen(["open", "-a", app],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            cmd = _process_name(browser)
            subprocess.Popen([cmd],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception:
        pass


# ── 在线验证 ──────────────────────────────────────────────────────

def check_logged_in(page) -> bool:
    """通过页内 fetch 调 /api/sns/web/v2/user/me 检查是否已登录。"""
    try:
        return page.evaluate(
            """async () => {
                const r = await fetch('/api/sns/web/v2/user/me', {credentials:'include'});
                const j = await r.json();
                return j && j.data && j.data.guest === false;
            }"""
        )
    except Exception:
        return False


# ── 主入口 ────────────────────────────────────────────────────────

_SUPPORTED_BROWSERS = ("edge", "chrome", "firefox", "brave")


def extract_cookies(browser: str = "edge") -> tuple[dict[str, str], dict[str, dict]]:
    """从浏览器提取小红书 cookie。

    Chromium 系（Edge/Chrome/Brave）：Playwright channel 模式加载真实 profile。
    Firefox：直接读 cookies.sqlite（明文，无需 Playwright）。
    返回 (cookies, cookie_meta)。
    """
    if browser not in _SUPPORTED_BROWSERS:
        raise ValueError(f"不支持的浏览器: {browser}，可选 {' / '.join(_SUPPORTED_BROWSERS)}")

    # Firefox：直接 sqlite3 读取，无需 Playwright
    if browser == "firefox":
        return _extract_firefox_cookies()

    # Chromium 系：Playwright channel 模式
    profile = _profile_dir(browser)
    if not profile:
        raise FileNotFoundError(
            f"未找到 {browser.title()} 用户配置目录"
            f"（{sys.platform}）。请确认 {browser.title()} 已安装。"
        )

    was_running = _is_browser_running(browser)
    if was_running:
        print(f"[NATIVE] {browser.title()} 正在运行，暂时关闭以读取 cookie...",
              file=sys.stderr)
        _close_browser(browser)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        if was_running:
            _reopen_browser(browser)
        raise ImportError(
            "需要 Playwright: pip install playwright && playwright install chromium"
        ) from e

    try:
        with sync_playwright() as pw:
            channel = _PLAYWRIGHT_CHANNEL[browser]
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=profile,
                channel=channel,
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.new_page()
            try:
                page.goto("https://www.xiaohongshu.com/",
                          wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(2)

            raw_cookies = ctx.cookies()
            cookies: dict[str, str] = {}
            cookie_meta: dict[str, dict] = {}
            for c in raw_cookies:
                if "xiaohongshu.com" not in c.get("domain", ""):
                    continue
                name = c["name"]
                cookies[name] = c["value"]
                meta = {k: c[k] for k in ("domain", "path", "secure", "httpOnly", "sameSite", "expires")
                        if k in c and c[k] is not None}
                if meta:
                    cookie_meta[name] = meta

            if not REQUIRED_KEYS.issubset(cookies.keys()):
                missing = REQUIRED_KEYS - cookies.keys()
                ctx.close()
                raise ValueError(
                    f"{browser.title()} 中小红书 cookie 不完整，缺少: {missing}。"
                    f"请先在 {browser.title()} 中打开 xiaohongshu.com 并登录。"
                )

            ctx.close()
            print(f"[NATIVE] 从 {browser.title()} 提取到 {len(cookies)} 个 cookie",
                  file=sys.stderr)
            return cookies, cookie_meta

    except (ValueError, ImportError):
        raise
    except Exception as e:
        raise OSError(f"Playwright cookie 提取失败: {e}")
    finally:
        if was_running:
            print(f"[NATIVE] 重新打开 {browser.title()}...", file=sys.stderr)
            _reopen_browser(browser)
