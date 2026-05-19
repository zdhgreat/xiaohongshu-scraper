"""Fetcher：HTTP 层 + 节流 + Cookie 管理 + 风控处理 + 浏览器接管。

从 xhs.py 拆分出来的核心抓取层。

Fetcher 负责：
- 组装签名头 + cookie；可选 curl_cffi Chrome TLS 模拟
- 响应码处理（460 降速 / 461 切浏览器接管 / -100 重新登录 / 429 退避）
- speed-mode 节流：normal / slow / paranoid （burst + rest 模型）
- session warmup：开抓前调一次 homefeed 模仿真实导航
- 周期 cookie refresh：每 N 次抓取调一次 user/me 让 websectiga/sec_poison_id 更新
- xsec_token 透传：search/user_posted 拿到的 token 入库，note detail 自动带上
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import xhs_accounts
import xhs_config
import xhs_log
import xhs_proxy
import xhs_sign

# 优先 curl_cffi 模拟 Chrome 的 TLS/JA3/JA4 指纹；不可用则降级到 requests
try:
    from curl_cffi.requests import Session as _HttpSession  # type: ignore
    _CURL_CFFI = True
except ImportError:
    import requests as _requests  # type: ignore
    _HttpSession = _requests.Session  # type: ignore
    _CURL_CFFI = False


class FatalRiskError(RuntimeError):
    pass


class Fetcher:
    def __init__(
        self,
        signer: xhs_sign.SignerBase,
        speed: xhs_config.SpeedProfile,
        account_mgr: xhs_accounts.AccountManager,
        proxy_pool: xhs_proxy.ProxyPool | None = None,
        force_account: str | None = None,
        sign_mode_label: str = "auto",
        speed_mode_label: str = "normal",
    ) -> None:
        self.signer = signer
        self.speed = speed
        self.account_mgr = account_mgr
        self.proxy_pool = proxy_pool
        self.force_account = force_account
        self._sign_mode_label = sign_mode_label
        self._speed_mode_label = speed_mode_label

        # 当前活跃账号 + cookies
        self.account = account_mgr.get(force_account)
        self.cookies = self.account.cookies

        # 当前代理
        self.current_proxy: xhs_proxy.Proxy | None = None
        if proxy_pool and proxy_pool.is_active():
            self.current_proxy = proxy_pool.next_available()

        # HTTP session
        if _CURL_CFFI:
            self.session = _HttpSession(impersonate=xhs_config.IMPERSONATE_PROFILE)
        else:
            self.session = _HttpSession()
        self._apply_proxy()
        self.session.headers.update(xhs_config.base_headers())
        self.session.cookies.update(self.cookies)
        self._apply_fingerprint()

        self.request_count = 0
        self.burst_remaining = random.randint(*speed.burst_size)
        self.consecutive_460 = 0
        self._total_460_retries = 0  # 全局 460 重试计数（防止无限循环）
        self._ip_timestamps: dict[str, deque] = {}
        self._ip_rate_limit = xhs_config.IP_RATE_LIMIT
        self._ip_rate_window = xhs_config.IP_RATE_WINDOW
        self.browser_takeover: PlaywrightTakeover | None = None
        self._warmed = False
        self._relogin_count = 0
        self._engine_label = "curl_cffi/" + xhs_config.IMPERSONATE_PROFILE if _CURL_CFFI else "requests/native"
        proxy_info = f"proxy={self.current_proxy.label}" if self.current_proxy else "直连"
        print(f"[FETCH] HTTP engine: {self._engine_label} | account={self.account.alias} | {proxy_info}",
              file=sys.stderr)

    def _apply_proxy(self) -> None:
        if self.current_proxy:
            self.session.proxies = {"http": self.current_proxy.url, "https": self.current_proxy.url}
        else:
            self.session.proxies = {}

    def _apply_fingerprint(self) -> None:
        """应用当前账号的独立设备指纹到 session。"""
        fp = getattr(self.account, 'fingerprint', None)
        if fp is None:
            return
        self.session.headers.update({
            "user-agent": fp.user_agent,
            "sec-ch-ua": fp.sec_ch_ua,
            "accept-language": fp.accept_language,
        })
        if _CURL_CFFI and hasattr(self.session, 'impersonate'):
            self.session.impersonate = fp.impersonate
        self._engine_label = f"curl_cffi/{fp.impersonate}" if _CURL_CFFI else "requests/native"

    def _rotate_account(self, reason: str) -> bool:
        """换一个可用账号；失败返回 False（让上层 raise）。"""
        try:
            new_acc = self.account_mgr.next_available()
        except xhs_accounts.AccountError as e:
            print(f"[FETCH] 账号轮换失败：{e}", file=sys.stderr)
            return False
        if new_acc.alias == self.account.alias:
            return False  # 只有一个账号，没法轮
        print(f"[FETCH] 切换账号 {self.account.alias} → {new_acc.alias}（{reason}）", file=sys.stderr)
        self.account_mgr.save_state()
        self.account = new_acc
        self.cookies = new_acc.cookies
        self.session.cookies.clear()
        self.session.cookies.update(self.cookies)
        self._apply_fingerprint()
        return True

    def _rotate_proxy(self, reason: str) -> bool:
        if not self.proxy_pool or not self.proxy_pool.is_active():
            return False
        old = self.current_proxy.label if self.current_proxy else "直连"
        new_p = self.proxy_pool.next_available()
        if new_p is None:
            print(f"[FETCH] 代理池全部冷却中（{reason}），临时直连", file=sys.stderr)
            self.current_proxy = None
        else:
            print(f"[FETCH] 切换代理 {old} → {new_p.label}（{reason}）", file=sys.stderr)
            self.current_proxy = new_p
        self._apply_proxy()
        return True

    # -----------------------------------------------------------------
    # Warmup：模仿真人 — 打开首页 → 等几秒 → 才开始抓
    # -----------------------------------------------------------------
    def warmup(self) -> None:
        if self._warmed:
            return
        try:
            print("[FETCH] warmup: POST /api/sns/web/v1/homefeed (推荐流) ...", file=sys.stderr)
            self._call_raw("POST", "/api/sns/web/v1/homefeed", None, {
                "cursor_score": "", "num": 18, "refresh_type": 1, "note_index": 0,
                "unread_begin_note_id": "", "unread_end_note_id": "", "unread_note_count": 0,
                "category": "homefeed_recommend", "search_key": "",
            }, count=False)
            time.sleep(random.uniform(3, 8))
            self._warmed = True
            print("[FETCH] warmup OK（模拟首页停留）", file=sys.stderr)
        except Exception as e:
            print(f"[FETCH] warmup 失败（继续抓取，但风控风险升高）：{e}", file=sys.stderr)
            self._warmed = True

    # -----------------------------------------------------------------
    # 自适应降速：首次 460 自动降档
    # -----------------------------------------------------------------
    def _downshift_speed(self) -> bool:
        """降速到更慢的 profile。返回 True 表示已降速。"""
        target_name = xhs_config.SPEED_DOWNSHIFT.get(self._speed_mode_label, "paranoid")
        if target_name == self._speed_mode_label:
            return False
        old_label = self._speed_mode_label
        self.speed = xhs_config.SPEED_PROFILES[target_name]
        self._speed_mode_label = target_name
        self.burst_remaining = random.randint(*self.speed.burst_size)
        print(f"[FETCH] 自适应降速：{old_label} → {target_name}（检测到 460 风控）", file=sys.stderr)
        return True

    # -----------------------------------------------------------------
    # IP 级滑动窗口限速
    # -----------------------------------------------------------------
    def _check_per_ip_rate(self) -> None:
        """同一 IP 在时间窗口内请求数超限则等待。"""
        ip_label = self.current_proxy.url if self.current_proxy else "direct"
        now = time.time()
        if ip_label not in self._ip_timestamps:
            self._ip_timestamps[ip_label] = deque()
        window = self._ip_timestamps[ip_label]
        while window and window[0] < now - self._ip_rate_window:
            window.popleft()
        if len(window) >= self._ip_rate_limit:
            wait = (window[0] + self._ip_rate_window) - now + 0.5
            if wait > 0:
                print(f"[FETCH] IP 限速 {self._ip_rate_limit}次/{self._ip_rate_window}s，等待 {wait:.1f}s",
                      file=sys.stderr)
                time.sleep(wait)
        window.append(now)

    # -----------------------------------------------------------------
    # Smart pacing：burst + rest 模型
    # -----------------------------------------------------------------
    def _throttle(self) -> None:
        self._check_per_ip_rate()
        if self.burst_remaining <= 0:
            rest = random.uniform(*self.speed.rest_gap)
            print(f"[FETCH] burst done, rest {rest:.1f}s（模仿浏览/思考）", file=sys.stderr)
            time.sleep(rest)
            self.burst_remaining = random.randint(*self.speed.burst_size)
        else:
            time.sleep(random.uniform(*self.speed.burst_gap))
        self.burst_remaining -= 1

        if self.request_count and self.request_count % self.speed.long_rest_every == 0:
            rest = random.uniform(*self.speed.long_rest)
            print(f"[FETCH] 每 {self.speed.long_rest_every} 条强制休息 {rest/60:.1f}min", file=sys.stderr)
            time.sleep(rest)

    # -----------------------------------------------------------------
    # 周期性 cookie 刷新：让 websectiga / sec_poison_id 自然更新
    # -----------------------------------------------------------------
    def _maybe_refresh_cookies(self) -> None:
        if self.request_count and self.request_count % xhs_config.COOKIE_REFRESH_EVERY == 0:
            print(f"[FETCH] 已抓 {self.request_count} 次，刷一次 cookie...", file=sys.stderr)
            try:
                self._call_raw("GET", "/api/sns/web/v2/user/me", None, None, count=False)
            except Exception as e:
                print(f"[FETCH] cookie 刷新失败：{e}", file=sys.stderr)

    # -----------------------------------------------------------------
    # 对外接口
    # -----------------------------------------------------------------
    def get(self, api: str, params: dict[str, Any] | None = None) -> dict:
        return self._call("GET", api, params=params, data=None)

    def post(self, api: str, data: dict[str, Any] | None = None) -> dict:
        return self._call("POST", api, params=None, data=data or {})

    def _call(
        self, method: str, api: str, params: dict | None, data: dict | None
    ) -> dict:
        if self.request_count >= xhs_config.DAILY_HARD_CAP:
            raise FatalRiskError(f"达到单账号日抓硬上限 {xhs_config.DAILY_HARD_CAP}，请明日再战")
        self.warmup()
        self._throttle()
        result = self._call_raw(method, api, params, data, count=True)
        self._maybe_refresh_cookies()
        return result

    def _call_raw(
        self, method: str, api: str, params: dict | None, data: dict | None, count: bool,
        _retry_depth: int = 0,
    ) -> dict:
        if method == "GET":
            full_api = api + ("?" + urlencode(params) if params else "")
            body = ""
            url = xhs_config.BASE + full_api
        else:
            full_api = api
            body = data or {}
            url = xhs_config.BASE + api

        a1 = self.cookies.get("a1", "")
        sign_headers = self.signer.sign(full_api, body, a1, method)
        headers = {**self.session.headers, **sign_headers}

        t0 = time.time()
        proxy_label = self.current_proxy.label if self.current_proxy else None
        try:
            if method == "GET":
                resp = self.session.get(url, headers=headers, timeout=20)
            else:
                resp = self.session.post(
                    url,
                    headers=headers,
                    data=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
                    timeout=20,
                )
        except Exception as e:
            duration = int((time.time() - t0) * 1000)
            print(f"[FETCH] 网络异常：{e}", file=sys.stderr)
            xhs_log.log_request(api, method, 0, None, str(e)[:80], duration,
                                 sign_mode=self._sign_mode_label,
                                 speed_mode=self._speed_mode_label,
                                 account=self.account.alias, proxy=proxy_label)
            # 网络异常可能是代理问题
            if self.current_proxy:
                self.current_proxy.mark_failure()
                self._rotate_proxy("网络异常")
            time.sleep(10)
            raise

        duration = int((time.time() - t0) * 1000)
        if count:
            self.request_count += 1
            self.account.mark_used()

        # 把响应里的 Set-Cookie 同步回 session
        if hasattr(resp, "cookies"):
            try:
                for k, v in resp.cookies.items():
                    if v and self.cookies.get(k) != v:
                        self.cookies[k] = v
                        self.session.cookies.set(k, v)
            except Exception:
                pass

        # 先解析业务 code 用于日志（不影响 _handle 的处理）
        biz_code: int | None = None
        biz_msg = ""
        if resp.status_code == 200:
            try:
                _p = resp.json()
                biz_code = _p.get("code")
                biz_msg = _p.get("msg", "")
            except Exception:
                pass

        xhs_log.log_request(api, method, resp.status_code, biz_code, biz_msg, duration,
                             sign_mode=self._sign_mode_label,
                             speed_mode=self._speed_mode_label,
                             account=self.account.alias, proxy=proxy_label)

        if self.current_proxy and 200 <= resp.status_code < 400:
            self.current_proxy.mark_success()

        return self._handle(resp, method, api, params, data, _retry_depth=_retry_depth, count=count)

    def _handle(
        self,
        resp,
        method: str,
        api: str,
        params: dict | None,
        data: dict | None,
        _retry_depth: int = 0,
        count: bool = True,
    ) -> dict:
        status = resp.status_code
        if status == 200:
            try:
                payload = resp.json()
            except ValueError:
                print(f"[FETCH] 非 JSON 响应：{resp.text[:200]}", file=sys.stderr)
                raise FatalRiskError("响应非 JSON，可能 IP 被封")
            if payload.get("success") is False:
                code = payload.get("code")
                msg = payload.get("msg", "")
                if code == -100:
                    if self._relogin_count >= 1:
                        raise FatalRiskError("cookie 重登后仍失效，请手动登录")
                    self._relogin_count += 1
                    print(f"[FETCH] cookie 失效，尝试重登...", file=sys.stderr)
                    import xhs_login
                    # 多账号时优先 QR 扫码（profile_hint 隔离），避免 rookiepy 取到其他账号的 cookie
                    new_cookies = xhs_login.acquire_cookies(prefer="auto", profile_hint=self.account.alias)
                    self.cookies = new_cookies
                    self.account.cookies = new_cookies
                    self.account.save_cookies()
                    self.session.cookies.clear()
                    self.session.cookies.update(new_cookies)
                    return self._call_raw(method, api, params, data, count=count, _retry_depth=_retry_depth)
                print(f"[FETCH] success=False code={code} msg={msg}", file=sys.stderr)
            self.consecutive_460 = 0
            return payload

        if status == 460:
            self.consecutive_460 += 1
            self._total_460_retries += 1
            print(f"[FETCH] 460 风控 ×{self.consecutive_460}（累计重试 {self._total_460_retries}）", file=sys.stderr)
            if self._total_460_retries > 20:
                raise FatalRiskError("460 风控累计重试超过 20 次，建议换号或等待 24h")
            # 自适应降速：首次 460 立即降档
            if self.consecutive_460 == 1:
                self._downshift_speed()
            if self.consecutive_460 >= 3:
                # 先尝试代理轮换
                if self.proxy_pool and self.proxy_pool.is_active():
                    self.current_proxy and self.current_proxy.mark_failure()
                    self._rotate_proxy("连续 460")
                # 然后尝试账号冷却 + 轮换
                self.account.mark_460(cooldown_min=30)
                self.account_mgr.save_state()
                if self._rotate_account("连续 460"):
                    self.consecutive_460 = 0
                    return self._call(method, api, params, data)
                # 单账号：提示用户等待而非静默浏览器接管
                raise FatalRiskError(
                    f"单账号 {self.account.alias} 触发连续 460，已冷却 30 分钟。"
                    "请等待冷却结束后重试，或添加更多账号（login --name <alias>）"
                )
            time.sleep(random.uniform(60, 120))
            return self._call(method, api, params, data)

        if status == 461:
            print("[FETCH] 461 验证码", file=sys.stderr)
            self.account.mark_461(cooldown_min=120)
            self.account_mgr.save_state()
            # 先尝试切账号
            if self._rotate_account("461 验证码"):
                return self._call(method, api, params, data)
            # 否则切浏览器接管
            return self._browser_fetch(method, api, params, data)

        if status == 429:
            if _retry_depth >= 5:
                raise FatalRiskError("429 频率超限，连续重试 5 次仍被限流")
            print(f"[FETCH] 429 频率超限，退避（重试 {_retry_depth + 1}/5）", file=sys.stderr)
            time.sleep(random.uniform(60, 180))
            return self._call_raw(method, api, params, data, count=True,
                                  _retry_depth=_retry_depth + 1)

        if status == 403:
            if _retry_depth >= 3:
                raise FatalRiskError("403 IP/UA 异常，连续重试 3 次仍被拒绝")
            print(f"[FETCH] 403 IP/UA 异常（重试 {_retry_depth + 1}/3）", file=sys.stderr)
            time.sleep(300)
            return self._call_raw(method, api, params, data, count=True,
                                  _retry_depth=_retry_depth + 1)

        raise FatalRiskError(f"未知状态码 {status}: {resp.text[:200]}")

    def _browser_fetch(
        self, method: str, api: str, params: dict | None, data: dict | None
    ) -> dict:
        if self.browser_takeover is None:
            self.browser_takeover = PlaywrightTakeover(self.cookies, headless=False)
        try:
            payload = self.browser_takeover.fetch(method, api, params, data)
            self.consecutive_460 = 0
            return payload
        except Exception as e:
            print(f"[FETCH] 浏览器接管也失败：{e}", file=sys.stderr)
            raise FatalRiskError(
                "风控强度超出处理能力，建议换号或等待 24h"
            ) from e

    def close(self) -> None:
        if self.browser_takeover:
            self.browser_takeover.close()
        if hasattr(self.signer, "close"):
            try:
                self.signer.close()  # type: ignore
            except Exception:
                pass
        # 关闭 HTTP session（释放连接池和文件描述符）
        try:
            self.session.close()
        except Exception:
            pass
        # 落账号运行状态
        try:
            self.account_mgr.save_state()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Playwright takeover
# ---------------------------------------------------------------------------

class PlaywrightTakeover:
    _JS_GET = (
        "async ([path]) => {"
        "const r = await fetch(path, {credentials:'include', headers:{'Content-Type':'application/json'}});"
        "const t = await r.text();"
        "try { return {__ok:true, body: JSON.parse(t)}; }"
        "catch(e) { return {__ok:false, status: r.status, html: t.slice(0, 400)}; }"
        "}"
    )
    _JS_POST = (
        "async ([path, body]) => {"
        "const r = await fetch(path, {method:'POST', credentials:'include', "
        "headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});"
        "const t = await r.text();"
        "try { return {__ok:true, body: JSON.parse(t)}; }"
        "catch(e) { return {__ok:false, status: r.status, html: t.slice(0, 400)}; }"
        "}"
    )

    def __init__(self, cookies: dict[str, str], headless: bool = False) -> None:
        from playwright.sync_api import sync_playwright  # type: ignore
        self._pw = sync_playwright().start()
        # 使用独立 profile 避免与 PlaywrightSigner 冲突
        profile = xhs_config.DATA_DIR / "pw_profile_takeover"
        profile.mkdir(parents=True, exist_ok=True)
        self.ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.ctx.new_page()
        self.page.goto(xhs_config.WEB_BASE + "/", wait_until="domcontentloaded")
        try:
            self.page.wait_for_function(
                "typeof window._webmsxyw === 'function'", timeout=15000
            )
        except Exception:
            pass

    def fetch(
        self, method: str, api: str, params: dict | None, data: dict | None
    ) -> dict:
        # 浏览器内 fetch，由浏览器自己签名；JSON 解析失败时返回 HTML 头部供诊断
        if method == "GET":
            full = api + ("?" + urlencode(params) if params else "")
            ret = self.page.evaluate(self._JS_GET, [full])
        else:
            full = api
            ret = self.page.evaluate(self._JS_POST, [api, data or {}])
        if ret.get("__ok"):
            return ret["body"]
        # 非 JSON 响应（如验证页 HTML）：把当前页跳到 xhs，让用户手动过验证
        snippet = ret.get("html", "")[:200]
        print(f"[BROWSER] 浏览器接管收到非 JSON 响应（status={ret.get('status')}）: {snippet}", file=sys.stderr)
        print("[BROWSER] 请在弹出的 Chromium 窗口里手动完成滑块/验证码，60s 内完成", file=sys.stderr)
        try:
            self.page.goto(xhs_config.WEB_BASE + "/explore", wait_until="domcontentloaded", timeout=30000)
            self.page.wait_for_timeout(60000)  # 给用户 60s 过验证
            # 验证完再试一次（使用正确的 JS 模板）
            if method == "GET":
                ret = self.page.evaluate(self._JS_GET, [full])
            else:
                ret = self.page.evaluate(self._JS_POST, [api, data or {}])
            if ret.get("__ok"):
                return ret["body"]
        except Exception as e:
            print(f"[BROWSER] 重试失败：{e}", file=sys.stderr)
        raise RuntimeError(f"browser fetch returned non-JSON after retry: {snippet}")

    def close(self) -> None:
        try:
            self.ctx.close()
            self._pw.stop()
        except Exception:
            pass
