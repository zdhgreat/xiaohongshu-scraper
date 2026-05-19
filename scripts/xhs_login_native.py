"""跨平台浏览器 cookie 提取：使用 Playwright 读取用户真实 Edge/Chrome 配置。

工作原理：
  1. 定位浏览器用户配置目录（按平台不同）
  2. 检测浏览器是否在运行 → 暂时关闭（配置文件被锁）
  3. 用 Playwright channel="msedge"/"chrome" 加载用户配置
  4. 通过 ctx.cookies() 读取明文 cookie（Playwright 内部处理 v10/v20 解密）
  5. 提取完毕后重新打开浏览器

支持平台：Windows / macOS / Linux（不含 WSL）
支持浏览器：Edge / Chrome

WSL 不适用：WSL 下 Playwright 是 Linux 二进制，无法用 channel 调用 Windows 的 Edge/Chrome。
WSL 场景由 xhs_login_wsl.py 的 CDP 路径处理。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REQUIRED_KEYS = {"web_session", "a1"}

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
}


def _profile_dir(browser: str) -> str | None:
    """返回当前平台下浏览器 User Data 目录路径，不存在返回 None。"""
    key = {"darwin": "darwin"}.get(sys.platform, "linux" if sys.platform != "win32" else "win32")
    path = _PROFILE_MAP.get(browser, {}).get(key)
    return path if path and os.path.isdir(path) else None


# ── 浏览器进程管理（按平台） ──────────────────────────────────────

def _process_name(browser: str) -> str:
    """返回用于查找/关闭的进程名。"""
    if sys.platform == "win32":
        return "msedge.exe" if browser == "edge" else "chrome.exe"
    if sys.platform == "darwin":
        return "Microsoft Edge" if browser == "edge" else "Google Chrome"
    # Linux
    return "msedge" if browser == "edge" else "google-chrome"


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
            exe = "msedge.exe" if browser == "edge" else "chrome.exe"
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            subprocess.Popen(["cmd", "/c", "start", "", exe],
                             creationflags=flags)
        elif sys.platform == "darwin":
            app = "Microsoft Edge" if browser == "edge" else "Google Chrome"
            subprocess.Popen(["open", "-a", app],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            cmd = "microsoft-edge" if browser == "edge" else "google-chrome"
            subprocess.Popen([cmd],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception:
        pass


# ── 在线验证 ──────────────────────────────────────────────────────

def check_logged_in(page) -> bool:
    """通过页内 fetch 调 /api/sns/web/v2/user/me 检查是否已登录。

    可在 extract_cookies 返回后调用，确认 cookie 未过期。
    注意：该接口可能需要签名头（x-s, x-t），页内直接 fetch 可能失败，
    因此失败仅返回 False，不抛异常。
    """
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

def extract_cookies(browser: str = "edge") -> dict[str, str]:
    """从浏览器用户配置中提取小红书 cookie。

    使用 Playwright channel 模式加载用户真实浏览器配置，
    Playwright 内部处理 v10/v20 cookie 解密。
    浏览器运行时会暂时关闭并在提取后重新打开。

    Raises:
        FileNotFoundError: 浏览器配置目录不存在
        ValueError: cookie 不完整（用户未在小红书登录）
        ImportError: Playwright 未安装
        OSError: Playwright 操作失败
    """
    if browser not in ("edge", "chrome"):
        raise ValueError(f"不支持的浏览器: {browser}，可选 edge / chrome")

    profile = _profile_dir(browser)
    if not profile:
        raise FileNotFoundError(
            f"未找到 {browser} 用户配置目录"
            f"（{sys.platform}）。请确认 {browser} 已安装。"
        )

    # 检测并关闭运行中的浏览器（配置文件被锁）
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
            channel = "msedge" if browser == "edge" else "chrome"
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
                pass  # 页面加载失败不影响 cookie 读取
            time.sleep(2)

            # 读取 cookie — 不过滤 URL，手动筛选 xiaohongshu 域名
            raw_cookies = ctx.cookies()
            cookies = {
                c["name"]: c["value"]
                for c in raw_cookies
                if "xiaohongshu.com" in c.get("domain", "")
            }

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
            return cookies

    except (ValueError, ImportError):
        raise
    except Exception as e:
        raise OSError(f"Playwright cookie 提取失败: {e}")
    finally:
        if was_running:
            print(f"[NATIVE] 重新打开 {browser.title()}...", file=sys.stderr)
            _reopen_browser(browser)
