"""WSL 下从 Windows Chromium 系浏览器（Edge / Chrome）自动提取小红书 cookie。

为什么需要这个模块：
- rookiepy 在 Python 3.13+ 暂无 wheel
- WSL 下 rookiepy 也访问不到 Windows 浏览器的 DPAPI 加密 cookie
- 让用户手动复制粘贴 cookie 不够"Skill 化"

实现路径：
1. 检测 WSL（/proc/version 含 microsoft）
2. 通过 cmd.exe 拿 Windows 用户名 → 定位 Edge/Chrome 用户目录
3. 读 Local State（未锁定）→ 提取 encrypted_key（DPAPI 加密）
4. 调 PowerShell + .NET ProtectedData 用当前用户身份 DPAPI 解密 → 32 字节 AES key
5. 等待 Cookies SQLite 文件可读（Edge 持锁，需要用户关闭 Edge）
6. 复制 Cookies DB 到 /tmp，SQLite 读 xiaohongshu 相关 cookie
7. AES-GCM 解密每个 cookie 值（v10 / v11 格式）
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class WslLoginError(RuntimeError):
    pass


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


def is_wsl() -> bool:
    try:
        with open("/proc/version", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _get_windows_user() -> str:
    """通过 cmd.exe 拿 Windows %USERNAME%。比 /mnt/c/Users 扫描更准。"""
    try:
        out = subprocess.run(
            ["/mnt/c/Windows/System32/cmd.exe", "/c", "echo %USERNAME%"],
            cwd="/mnt/c/Users",
            capture_output=True, text=True, timeout=10,
        )
        name = out.stdout.strip().splitlines()[-1].strip()
        if name and name != "%USERNAME%":
            return name
    except Exception:
        pass
    # fallback：取 /mnt/c/Users 下最近活跃的目录
    users_root = Path("/mnt/c/Users")
    candidates = [p for p in users_root.iterdir()
                  if p.is_dir() and p.name not in ("Default", "Public", "All Users", "Default User")]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0].name
    raise WslLoginError("无法定位 Windows 用户目录")


def _browser_paths(browser: str, win_user: str) -> tuple[Path, Path]:
    """返回 (cookies_db_path, local_state_path)。"""
    base = Path(f"/mnt/c/Users/{win_user}/AppData/Local")
    if browser == "edge":
        prof = base / "Microsoft" / "Edge" / "User Data"
    elif browser == "chrome":
        prof = base / "Google" / "Chrome" / "User Data"
    else:
        raise WslLoginError(f"unsupported browser: {browser}")
    return prof / "Default" / "Network" / "Cookies", prof / "Local State"


def _read_aes_key(local_state_path: Path) -> bytes:
    if not local_state_path.exists():
        raise WslLoginError(f"Local State 不存在：{local_state_path}")
    data = json.loads(local_state_path.read_text(encoding="utf-8"))
    enc_key_b64 = data.get("os_crypt", {}).get("encrypted_key")
    if not enc_key_b64:
        raise WslLoginError("Local State 未找到 os_crypt.encrypted_key")
    blob = base64.b64decode(enc_key_b64)
    if not blob.startswith(b"DPAPI"):
        raise WslLoginError("encrypted_key 缺少 DPAPI 前缀，格式不识别")
    enc = blob[5:]  # 去掉 'DPAPI' 前缀
    # 写到 Windows %TEMP% 给 PowerShell 读
    win_temp = _windows_temp_dir()
    enc_path = win_temp / "xhs_enc_key.bin"
    enc_path.write_bytes(enc)
    dec_path = win_temp / "xhs_aes_key.bin"

    ps_script = (
        "Add-Type -AssemblyName System.Security; "
        f"$enc = [System.IO.File]::ReadAllBytes('{_to_win_path(enc_path)}'); "
        "$dec = [System.Security.Cryptography.ProtectedData]::Unprotect("
        "$enc, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser); "
        f"[System.IO.File]::WriteAllBytes('{_to_win_path(dec_path)}', $dec); "
        "Write-Output $dec.Length"
    )
    res = subprocess.run(
        ["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0 or "32" not in res.stdout:
        raise WslLoginError(f"DPAPI 解密失败：{res.stdout.strip()} | {res.stderr.strip()}")

    aes_key = dec_path.read_bytes()
    # 立刻清理临时文件（含密钥）
    try:
        enc_path.unlink(missing_ok=True)
        dec_path.unlink(missing_ok=True)
    except OSError:
        pass
    if len(aes_key) != 32:
        raise WslLoginError(f"AES key 长度异常：{len(aes_key)}")
    return aes_key


def _windows_temp_dir() -> Path:
    """返回 WSL 视角的 Windows %TEMP% 路径。"""
    win_user = _get_windows_user()
    p = Path(f"/mnt/c/Users/{win_user}/AppData/Local/Temp")
    if not p.exists():
        raise WslLoginError(f"Windows Temp 目录不存在：{p}")
    return p


def _to_win_path(wsl_path: Path) -> str:
    """/mnt/c/foo → C:\\foo"""
    s = str(wsl_path)
    if s.startswith("/mnt/"):
        drive = s[5:6].upper()
        rest = s[6:].replace("/", "\\")
        return f"{drive}:{rest}"
    raise WslLoginError(f"not a /mnt path: {wsl_path}")


def _close_browser_processes(browser: str) -> int:
    """通过 PowerShell 关闭浏览器进程，返回被关的进程数。"""
    proc_name = "msedge" if browser == "edge" else "chrome"
    res = subprocess.run(
        ["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
         "-NoProfile", "-Command",
         f"$p = Get-Process -Name '{proc_name}' -ErrorAction SilentlyContinue; "
         f"if ($p) {{ $p | Stop-Process -Force; Write-Output $p.Count }} else {{ Write-Output 0 }}"],
        capture_output=True, text=True, timeout=15,
    )
    try:
        # 取最后一行非空输出
        lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]
        return int(lines[-1]) if lines else 0
    except (ValueError, IndexError):
        return 0


def _copy_locked_db(
    src: Path, dst: Path,
    browser: str = "edge",
    auto_close: bool = True,
    wait_timeout_s: int = 60,
) -> None:
    """复制 cookies SQLite。被锁时：
       auto_close=True → 主动关闭浏览器后立即复制
       auto_close=False → 提示用户手动关闭，轮询等待
    """
    # 先尝试直接复制
    try:
        shutil.copy(src, dst)
        return
    except (PermissionError, OSError):
        pass

    if auto_close:
        print(
            f"\n[LOGIN-WSL] Cookies 文件被 {browser.title()} 锁定。\n"
            f"[LOGIN-WSL] 自动关闭 {browser.title()} 以读取 cookie（你的标签页保存在浏览历史中可恢复）...",
            file=sys.stderr,
        )
        n = _close_browser_processes(browser)
        print(f"[LOGIN-WSL] 已关闭 {n} 个 {browser} 进程", file=sys.stderr)
        # 等浏览器释放文件句柄
        for _ in range(20):
            time.sleep(0.5)
            try:
                shutil.copy(src, dst)
                print(f"[LOGIN-WSL] Cookies 复制成功；你现在可以重新打开 {browser.title()}", file=sys.stderr)
                return
            except (PermissionError, OSError):
                continue
        raise WslLoginError(f"已关闭 {browser} 但仍无法复制（可能有残留进程，请手动关闭后重试）")

    # auto_close=False：提示用户手动关闭，轮询
    print(
        f"\n[LOGIN-WSL] Cookies 文件被浏览器锁定（{src.name}）。\n"
        f"            请手动关闭 {browser.title()} 所有窗口，脚本会自动检测并继续。\n"
        f"            （等待中，每 2s 重试一次，超时 {wait_timeout_s}s）",
        file=sys.stderr,
    )
    deadline = time.time() + wait_timeout_s
    last_err = ""
    while time.time() < deadline:
        try:
            shutil.copy(src, dst)
            return
        except (PermissionError, OSError) as e:
            last_err = str(e)
            time.sleep(2)
    raise WslLoginError(f"等待 {wait_timeout_s}s 仍无法复制 cookies：{last_err}")


def _decrypt_cookie_value(encrypted: bytes, aes_key: bytes) -> str:
    """解密 Chromium cookie 值。
    - v10/v11: AES-GCM with profile AES key（DPAPI 派生）— 我们能解
    - v20:     App-Bound Encryption（Chrome 130+/Edge 130+）— 用户态无法解
    """
    if not encrypted:
        return ""
    prefix = encrypted[:3]
    if prefix == b"v20":
        raise WslLoginError("v20")  # 调用方据此判断走 CDP/Playwright 兜底
    if prefix in (b"v10", b"v11"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = encrypted[3:15]
        ct_and_tag = encrypted[15:]
        try:
            plain = AESGCM(aes_key).decrypt(nonce, ct_and_tag, None)
        except Exception as e:
            raise WslLoginError(f"AES-GCM 解密失败：{e}")
        if len(plain) > 32 and not _looks_like_text(plain[:32]):
            plain = plain[32:]
        return plain.decode("utf-8", errors="replace")
    raise WslLoginError(f"未知 cookie 加密格式 prefix={prefix!r}")


def _looks_like_text(b: bytes) -> bool:
    try:
        b.decode("ascii")
        return True
    except UnicodeDecodeError:
        return False


def _extract_xhs_cookies(db_path: Path, aes_key: bytes) -> tuple[dict[str, str], dict[str, dict]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT name, encrypted_value, host_key, path, is_secure, is_httponly, samesite, expires_utc "
            "FROM cookies "
            "WHERE host_key LIKE '%xiaohongshu.com%' OR host_key LIKE '%xhscdn.com%'"
        )
        cookies: dict[str, str] = {}
        cookie_meta: dict[str, dict] = {}
        v20_count = 0
        skipped_other = 0
        for name, enc, host_key, path, is_secure, is_httponly, samesite, expires_utc in cur.fetchall():
            try:
                cookies[name] = _decrypt_cookie_value(bytes(enc), aes_key)
                cookie_meta[name] = _build_sqlite_cookie_meta(
                    host_key, path, is_secure, is_httponly, samesite,
                    expires_utc / 1e6 - 11644473600 if expires_utc else 0)
            except WslLoginError as e:
                if str(e) == "v20":
                    v20_count += 1
                else:
                    print(f"[LOGIN-WSL] 跳过 {name}: {e}", file=sys.stderr)
                    skipped_other += 1
        if v20_count and not cookies:
            raise WslLoginError(
                f"全部 {v20_count} 个 cookie 都是 v20（App-Bound Encryption）格式，"
                "用户态无法解密。这是 Edge/Chrome 130+ 的安全策略。\n"
                "  解决方案：\n"
                "    1) 推荐 → python3 scripts/xhs.py login --prefer qr   "
                "（用 Playwright 启 Chromium 扫码登录，绕开 v20）\n"
                "    2) 备选 → python3 scripts/xhs.py login --prefer manual  "
                "（从 DevTools Network 面板复制 Cookie 请求头粘贴）\n"
                "    3) 备选 → 装 Firefox 登录小红书，再 `--prefer wsl-firefox`（待实现）"
            )
        return cookies, cookie_meta
    finally:
        conn.close()


def acquire_from_wsl_browser(browser: str = "edge", auto_close: bool = True) -> tuple[dict[str, str], dict[str, dict]]:
    """主入口。browser ∈ {edge, chrome}。auto_close=True 自动关浏览器拿锁。返回 (cookies, cookie_meta)。"""
    if not is_wsl():
        raise WslLoginError("not running on WSL")

    win_user = _get_windows_user()
    print(f"[LOGIN-WSL] Windows 用户：{win_user}，浏览器：{browser}", file=sys.stderr)

    db_src, local_state = _browser_paths(browser, win_user)
    if not local_state.exists():
        raise WslLoginError(f"未找到 {browser} 用户目录：{local_state.parent}")

    print(f"[LOGIN-WSL] DPAPI 解密 AES key...", file=sys.stderr)
    aes_key = _read_aes_key(local_state)
    print(f"[LOGIN-WSL] AES key ({len(aes_key)} bytes) OK", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="xhs_wsl_") as tmp:
        db_dst = Path(tmp) / "Cookies"
        _copy_locked_db(db_src, db_dst, browser=browser, auto_close=auto_close)
        cookies, cookie_meta = _extract_xhs_cookies(db_dst, aes_key)

    if not cookies:
        raise WslLoginError(f"{browser} 中没有 xiaohongshu.com 的 cookie，请先在 {browser} 登录小红书")
    print(f"[LOGIN-WSL] 提取到 {len(cookies)} 个 cookie：{', '.join(list(cookies.keys())[:8])}{'...' if len(cookies)>8 else ''}", file=sys.stderr)

    required = {"a1", "web_session"}
    missing = required - cookies.keys()
    if missing:
        raise WslLoginError(f"提取成功但缺关键字段：{missing}（可能是登录态过期）")

    return cookies, cookie_meta


# ---------------------------------------------------------------------------
# CDP 路线：通过 Edge headless 自启 + Chrome DevTools Protocol 拉 cookie
# 适用：Edge 130+ 的 v20 App-Bound Encryption（DPAPI 路线失效时）
# 工作原理：spawn 一个 Edge 进程（用同一个 user data dir，含登录态），
#          它在自己进程内能解密 v20 cookies；我们通过 CDP 端口访问它，拿明文 cookies。
# ---------------------------------------------------------------------------

_EDGE_PATHS = [
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
]
_CHROME_PATHS = [
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]


def _find_browser_exe(browser: str) -> Path:
    paths = _EDGE_PATHS if browser == "edge" else _CHROME_PATHS
    for p in paths:
        if Path(p).exists():
            return Path(p)
    raise WslLoginError(f"未找到 {browser} 可执行文件")


def _stop_browser(browser: str) -> None:
    proc = "msedge" if browser == "edge" else "chrome"
    subprocess.run(
        ["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
         "-NoProfile", "-Command",
         f"Get-Process -Name '{proc}' -EA SilentlyContinue | Stop-Process -Force"],
        capture_output=True, timeout=15,
    )


def acquire_from_wsl_browser_cdp(
    browser: str = "edge",
    port: int = 9222,
    nav_timeout_s: int = 30,
) -> tuple[dict[str, str], dict[str, dict]]:
    """用 CDP 让浏览器自己解密 v20 cookies。需要：
    - 浏览器已安装且 user data dir 里有 xhs 登录态
    - playwright 已装（仅用 Playwright 的 connect_over_cdp，不启 Chromium）
    返回 (cookies, cookie_meta)。
    """
    if not is_wsl():
        raise WslLoginError("not running on WSL")
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as e:
        raise WslLoginError("缺少 playwright。pip install playwright") from e

    win_user = _get_windows_user()
    _, local_state = _browser_paths(browser, win_user)
    if not local_state.exists():
        raise WslLoginError(f"未找到 {browser} user data dir")
    profile_win = _to_win_path(local_state.parent)  # User Data
    exe = _find_browser_exe(browser)

    print(f"[LOGIN-CDP] 关闭现有 {browser} 进程...", file=sys.stderr)
    _stop_browser(browser)
    time.sleep(2)

    print(f"[LOGIN-CDP] 启动 {browser} headless + CDP 端口 {port}...", file=sys.stderr)
    log_file = Path("/tmp") / f"xhs_{browser}_cdp.log"
    proc = None
    with open(log_file, "wb") as log_fh:
        proc = subprocess.Popen(
            [str(exe),
             "--headless=new", "--disable-gpu",
             f"--remote-debugging-port={port}",
             f"--user-data-dir={profile_win}",
             "--no-first-run", "--no-default-browser-check",
             "about:blank"],
            stdout=log_fh, stderr=subprocess.STDOUT,
        )
        try:
            # 等 CDP 端口就绪
            import urllib.request, urllib.error
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=2).read()
                    break
                except urllib.error.URLError:
                    time.sleep(0.5)
            else:
                raise WslLoginError(f"CDP 端口 {port} 未就绪")

            with sync_playwright() as pw:
                cdp = pw.chromium.connect_over_cdp(f"http://localhost:{port}")
                ctx = cdp.contexts[0]
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    page.goto("https://www.xiaohongshu.com/explore",
                              wait_until="domcontentloaded", timeout=nav_timeout_s * 1000)
                except Exception as e:
                    print(f"[LOGIN-CDP] navigate 警告：{e}", file=sys.stderr)
                cks = ctx.cookies(["https://www.xiaohongshu.com", "https://edith.xiaohongshu.com"])
                cookies: dict[str, str] = {}
                cookie_meta: dict[str, dict] = {}
                for c in cks:
                    if "xiaohongshu" not in c.get("domain", ""):
                        continue
                    name = c["name"]
                    cookies[name] = c["value"]
                    meta = {k: c[k] for k in ("domain", "path", "secure", "httpOnly", "sameSite", "expires")
                            if k in c and c[k] is not None}
                    if meta:
                        cookie_meta[name] = meta
                cdp.close()
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            _stop_browser(browser)

    if not cookies:
        raise WslLoginError(f"{browser} 中没有 xiaohongshu cookie")
    required = {"a1", "web_session"}
    missing = required - cookies.keys()
    if missing:
        raise WslLoginError(
            f"{browser} 中缺关键 cookie {missing}（说明 {browser} 内没有真正登录小红书）。"
            f"请先在普通 {browser} 里打开 xhs.com 完成登录（扫码 + 手机 App 确认），再重试此命令。"
        )
    print(f"[LOGIN-CDP] 提取到 {len(cookies)} 个 cookies：{', '.join(list(cookies.keys())[:8])}",
          file=sys.stderr)
    return cookies, cookie_meta


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--browser", default="edge", choices=["edge", "chrome"])
    p.add_argument("--mode", default="dpapi", choices=["dpapi", "cdp"])
    p.add_argument("--no-close-browser", action="store_true")
    args = p.parse_args()
    if args.mode == "cdp":
        ck = acquire_from_wsl_browser_cdp(args.browser)
    else:
        ck = acquire_from_wsl_browser(args.browser, auto_close=not args.no_close_browser)
    print(json.dumps(ck, indent=2, ensure_ascii=False))
