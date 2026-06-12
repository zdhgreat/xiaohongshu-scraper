"""Fetcher：HTTP 层 + 节流 + Cookie 管理 + 风控处理 + 浏览器接管。

从 xhs.py 拆分出来的核心抓取层。

Fetcher 负责：
- 组装签名头 + cookie；可选 curl_cffi Chrome TLS 模拟
- 响应码处理（460 / 461 切浏览器接管 / -100 重新登录 / 429 退避）
- speed-mode 节流：paranoid（burst + rest 模型，串行运行）
- session warmup：开抓前调一次 homefeed 模仿真实导航
- 周期 cookie refresh：每 N 次抓取调一次 user/me 让 websectiga/sec_poison_id 更新
- xsec_token 透传：search/user_posted 拿到的 token 入库，note detail 自动带上
"""

from __future__ import annotations

import json
import random
import subprocess
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
from xhs_sign import extract_b1_standalone

# 优先 curl_cffi 模拟 Chrome 的 TLS/JA3/JA4 指纹；不可用则降级到 requests
try:
    from curl_cffi.requests import Session as _HttpSession  # type: ignore
    _CURL_CFFI = True
except ImportError:
    import requests as _requests  # type: ignore
    _HttpSession = _requests.Session  # type: ignore
    _CURL_CFFI = False
    print("[WARNING] ══════════════════════════════════════════════════════", file=sys.stderr)
    print("[WARNING] curl_cffi 未安装！TLS 指纹为 Python 默认值，极易被风控检测", file=sys.stderr)
    print("[WARNING] API 搜索将强制切换到 DOM 浏览器模式以保证安全", file=sys.stderr)
    print("[WARNING] 安装方法: pip install curl_cffi>=0.5.10", file=sys.stderr)
    print("[WARNING] ══════════════════════════════════════════════════════", file=sys.stderr)


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
        speed_mode_label: str = "paranoid",
        autonomous: bool = False,
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
        self.cookie_meta: dict[str, dict] = getattr(self.account, 'cookie_meta', {})

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
        # 如果账号有绑定代理，优先使用
        if self.account.proxy_url and proxy_pool:
            bound = proxy_pool.get_bound(self.account.proxy_url)
            if bound:
                self.current_proxy = bound
                self._apply_proxy()
        self.session.headers.update(xhs_config.base_headers())
        self.session.cookies.update(self.cookies)
        self._engine_label = "curl_cffi/" + xhs_config.IMPERSONATE_PROFILE if _CURL_CFFI else "requests/native"
        # 应用指纹（会覆盖 session headers 和 _engine_label 为账号专属值）
        self._apply_fingerprint()

        # 从账号状态恢复今日已抓数量，确保跨 Fetcher 实例的日抓上限有效
        self.request_count = self.account.daily_count if self.account else 0
        self._dom_search_count = self.account.dom_search_count if self.account else 0
        self._search_api_calls: list[float] = []  # 搜索 API 调用时间戳（滚动窗口，实例级）
        self.burst_remaining = random.randint(*speed.burst_size)
        self.consecutive_460 = 0
        self._total_460_retries = 0  # 全局 460 重试计数（防止无限循环）
        self._last_460_time = 0.0  # 上次 460 时间，用于时间衰减
        self._ip_timestamps: dict[str, deque] = {}
        self._ip_rate_limit = xhs_config.IP_RATE_LIMIT
        self._ip_rate_window = xhs_config.IP_RATE_WINDOW
        self._last_geo_check: float = 0.0
        self.browser_takeover: PlaywrightTakeover | None = None
        self.dom_search: PlaywrightDomSearch | None = None
        self._warmed = False
        self._relogin_count = 0
        self.autonomous = autonomous
        proxy_info = f"proxy={self.current_proxy.label}" if self.current_proxy else "直连"
        print(f"[FETCH] HTTP engine: {self._engine_label} | account={self.account.alias} | {proxy_info}",
              file=sys.stderr)
        if not _CURL_CFFI:
            print("[FETCH] ⚠️ curl_cffi 不可用：搜索走 DOM 安全，但笔记详情/用户/评论等 API 调用暴露 TLS 指纹",
                  file=sys.stderr)
        # Layer 3: 会话稳定
        self._session_start: float = time.time()
        self._session_active_duration: float = self._human_delay(1200, 4800)  # 20-80min
        self._auxiliary_counter: int = 0
        # Layer 4: 账号稳定
        self._account_active_start: float = time.time()
        self._account_max_active: float = self._human_delay(1800, 5400)  # 30-90min
        self._window_request_count: int = 0  # 当前轮换窗口内的请求数
        self._cookie_integrity_warned: bool = False  # 是否已警告 cookie 不完整

    def _apply_proxy(self) -> None:
        if self.current_proxy:
            self.session.proxies = {"http": self.current_proxy.url, "https": self.current_proxy.url}
        else:
            self.session.proxies = {}

    def _apply_fingerprint(self) -> None:
        """应用当前账号的独立设备指纹到 session。

        确保同一请求中 sec-ch-ua（HTTP 头声明版本）与 impersonate
        （TLS ClientHello 模拟版本）始终匹配，避免版本矛盾被检测。
        """
        fp = getattr(self.account, 'fingerprint', None)
        if fp is None:
            # 无指纹时，确保 impersonate 与 base_headers 版本一致
            if _CURL_CFFI and hasattr(self.session, 'impersonate'):
            # 根据 UA 推断 platform，保持与 user-agent 一致
                cur_ua = self.session.headers.get("user-agent", "")
                if "Chrome/136" in cur_ua:
                    self.session.impersonate = "chrome136"
                elif "Chrome/133" in cur_ua:
                    self.session.impersonate = "chrome133a"
            return
        # 根据 UA 推断 platform，保持与 user-agent 一致
        ua = fp.user_agent
        if "Macintosh" in ua:
            platform = '"macOS"'
        elif "Linux" in ua and "Android" not in ua:
            platform = '"Linux"'
        else:
            platform = '"Windows"'
        self.session.headers.update({
            "user-agent": ua,
            "sec-ch-ua": fp.sec_ch_ua,
            "sec-ch-ua-platform": platform,
            "accept-language": fp.accept_language,
        })
        if _CURL_CFFI and hasattr(self.session, 'impersonate'):
            self.session.impersonate = fp.impersonate
        self._engine_label = f"curl_cffi/{fp.impersonate}" if _CURL_CFFI else "requests/native"

    def _current_platform(self) -> str:
        """从当前账号指纹提取平台名称（用于签名载荷 x2 字段）。"""
        fp = getattr(self.account, 'fingerprint', None)
        if fp is None:
            return "Windows"
        ua = fp.user_agent
        if "Macintosh" in ua:
            return "macOS"
        return "Windows"

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
        self.cookie_meta = getattr(new_acc, 'cookie_meta', {})
        self.session.cookies.clear()
        self.session.cookies.update(self.cookies)
        self._apply_fingerprint()
        # 如果新账号有绑定代理，切换到对应代理
        if new_acc.proxy_url and self.proxy_pool:
            bound = self.proxy_pool.get_bound(new_acc.proxy_url)
            if bound:
                self.current_proxy = bound
                self._apply_proxy()
                print(f"[FETCH] 切换到绑定代理 {bound.label} (account={new_acc.alias})",
                      file=sys.stderr)
        # 重置所有账号相关状态
        self._warmed = False
        self.consecutive_460 = 0
        self._total_460_retries = 0  # 新账号应该重新计数
        self._relogin_count = 0
        self.burst_remaining = random.randint(*self.speed.burst_size)
        self._session_start = time.time()
        self._session_active_duration = self._human_delay(1200, 4800)
        self._account_max_active = self._human_delay(1800, 5400)
        self._account_active_start = time.time()
        self._window_request_count = 0
        # 刷新浏览器实例（新号的 cookie 不同）
        if self.browser_takeover:
            self.browser_takeover.close()
            self.browser_takeover = None
        if self.dom_search:
            self.dom_search.close()
            self.dom_search = None
        self._rotate_proxy(reason)
        return True

    def _rotate_proxy(self, reason: str) -> bool:
        """切换到下一个可用代理；无代理池或全部冷却时等待最早可用的代理。"""
        if not self.proxy_pool or not self.proxy_pool.is_active():
            return False
        old = self.current_proxy.label if self.current_proxy else "直连"
        new_p = self.proxy_pool.next_available()
        if new_p is None:
            # 所有代理冷却中：等待最早可用的代理，而非裸连
            wait_min = self.proxy_pool.earliest_recovery()
            if wait_min and wait_min > 0:
                wait = min(wait_min, 300)  # 最多等 5 分钟
                print(f"[FETCH] 代理池全部冷却中（{reason}），等待 {wait:.0f}s 后重试",
                      file=sys.stderr)
                time.sleep(wait)
                new_p = self.proxy_pool.next_available()
            if new_p is None:
                print(f"[FETCH] 代理池等待后仍无可用代理（{reason}），暂停本次请求",
                      file=sys.stderr)
                return False
        print(f"[FETCH] 切换代理 {old} → {new_p.label}（{reason}）", file=sys.stderr)
        self.current_proxy = new_p
        self._apply_proxy()
        self._check_proxy_geo()
        return True

    def _check_proxy_geo(self) -> None:
        """检查代理 IP 归属地与当前指纹 region 是否矛盾（每小时最多一次）。"""
        if not self.current_proxy:
            return
        now = time.time()
        if now - self._last_geo_check < 3600:
            return
        self._last_geo_check = now
        fp = getattr(self.account, 'fingerprint', None)
        expected_region = fp.region if fp else "CN"
        try:
            import requests as _req
            resp = _req.get(
                "http://ip-api.com/json/?fields=countryCode",
                proxies={"http": self.current_proxy.url, "https": self.current_proxy.url},
                timeout=10,
            )
            if resp.status_code == 200:
                country = resp.json().get("countryCode", "")
                if country and country != expected_region:
                    print(f"[GEO-WARN] 代理 IP 在 {country}，但指纹区域是 {expected_region}。"
                          f"可能触发风控检测。", file=sys.stderr)
        except Exception:
            pass  # 网络错误不阻塞

    # -----------------------------------------------------------------
    # Warmup：模仿真人 — 打开首页 → 等几秒 → 才开始抓
    # -----------------------------------------------------------------
    def warmup(self) -> None:
        if self._warmed:
            return
        try:
            # 随机选 warmup 行为（模拟不同入口场景，含消费类动作）
            warmup_actions = [
                # 打开首页推荐流（发现类）
                lambda: self._call_raw("POST", "/api/sns/web/v1/homefeed", None, {
                    "cursor_score": "", "num": 18, "refresh_type": 1, "note_index": 0,
                    "unread_begin_note_id": "", "unread_end_note_id": "", "unread_note_count": 0,
                    "category": "homefeed_recommend", "search_key": "",
                }, count=False),
                # 打开分类推荐（模拟切换 tab）
                lambda: self._call_raw("POST", "/api/sns/web/v1/homefeed", None, {
                    "cursor_score": "", "num": 18, "refresh_type": 1, "note_index": 0,
                    "category": random.choice([
                        "homefeed.food_v3", "homefeed.travel_v3",
                        "homefeed.fashion_v3", "homefeed.beauty_v3",
                    ]),
                }, count=False),
                # 查看通知
                lambda: self._call_raw("GET", "/api/sns/web/v1/you/notifications", None, None, count=False),
                # 查看个人信息（模拟点击头像）
                lambda: self._call_raw("GET", "/api/sns/web/v2/user/me", None, None, count=False),
            ]
            action = random.choice(warmup_actions)
            print("[FETCH] warmup ...", file=sys.stderr)
            action()
            time.sleep(random.uniform(15, 120))  # 模拟首页停留 15s-2min
            self._warmed = True
            print("[FETCH] warmup OK（模拟首页停留）", file=sys.stderr)
        except Exception as e:
            print(f"[FETCH] warmup 失败（继续抓取，但风控风险升高）：{e}", file=sys.stderr)
            self._warmed = True

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
    def _quiet_multiplier(self) -> float:
        """静默时段梯度过渡：23-1 点 3x 降速，1-5 点完全停止，5-6 点 3x 降速。"""
        import datetime
        hour = datetime.datetime.now().hour
        if xhs_config.QUIET_HOURS_START <= hour < xhs_config.QUIET_HOURS_END:
            return xhs_config.QUIET_HOURS_MULTIPLIER  # inf = 完全停止
        # 降速过渡时段（23-1 点和 5-6 点）
        if hour >= 23 or hour < 1:
            return 3.0  # 入睡前降速
        if 5 <= hour < 6:
            return 3.0  # 唤醒前降速
        return 1.0

    @staticmethod
    def _human_delay(lo: float, hi: float) -> float:
        """生成 [lo, hi] 范围内的类人延迟：对数正态分布，长尾偏斜。

        真人的浏览间隔不是均匀分布，而是偶尔出现较长的停顿（被其他事打断）。
        lognormvariate(mu, sigma) 的均值为 exp(mu + sigma²/2)，
        这里用目标区间的中点作为均值，sigma=0.4 产生适度长尾。
        """
        import math
        mid = (lo + hi) / 2
        mu = math.log(mid) if mid > 0 else 0
        sigma = 0.4
        val = random.lognormvariate(mu, sigma)
        return max(lo, min(val, hi * 2.5))  # 允许长尾但不超过 hi×2.5

    def _throttle(self) -> None:
        qm = self._quiet_multiplier()
        if qm == float("inf"):
            # 静默时段：等到静默结束再继续
            import datetime
            now = datetime.datetime.now()
            end_hour = xhs_config.QUIET_HOURS_END
            if now.hour < end_hour:
                wait = (end_hour - now.hour) * 3600 - now.minute * 60 - now.second
                print(f"[FETCH] 静默时段，等待 {wait/60:.0f} 分钟后继续", file=sys.stderr)
                time.sleep(wait)
            return

        self._check_per_ip_rate()
        if self.burst_remaining <= 0:
            # rest 期间做 auxiliary（模拟"浏览其他内容"）
            self._maybe_auxiliary_request()
            rest = self._human_delay(*self.speed.rest_gap) * qm
            print(f"[FETCH] burst done, rest {rest:.1f}s（模仿浏览/思考）", file=sys.stderr)
            time.sleep(rest)
            self.burst_remaining = random.randint(*self.speed.burst_size)
        else:
            gap = self._human_delay(*self.speed.burst_gap) * qm
            time.sleep(gap)
        self.burst_remaining -= 1

        if self.request_count and self.request_count % self.speed.long_rest_every == 0:
            self._maybe_refresh_cookies()
            rest = self._human_delay(*self.speed.long_rest) * qm
            print(f"[FETCH] 每 {self.speed.long_rest_every} 条强制休息 {rest/60:.1f}min", file=sys.stderr)
            time.sleep(rest)

    # -----------------------------------------------------------------
    # 周期性 cookie 刷新：让 websectiga / sec_poison_id 自然更新
    # -----------------------------------------------------------------
    def _maybe_refresh_cookies(self) -> None:
        if not hasattr(self, '_next_cookie_refresh_at'):
            self._next_cookie_refresh_at = self.request_count + random.randint(15, 25)
        if self.request_count and self.request_count >= self._next_cookie_refresh_at:
            self._next_cookie_refresh_at = self.request_count + random.randint(15, 25)
            print(f"[FETCH] 已抓 {self.request_count} 次，刷一次 cookie...", file=sys.stderr)
            try:
                self._call_raw("GET", "/api/sns/web/v2/user/me", None, None, count=False)
            except Exception as e:
                print(f"[FETCH] cookie 刷新失败：{e}", file=sys.stderr)

    # -----------------------------------------------------------------
    # 预测式 cookie 预检 + 统一重登录
    # -----------------------------------------------------------------
    def _predictive_cookie_check(self) -> bool:
        """每 N 次请求主动验证 cookie 有效性。返回 True=继续, False=需处理。"""
        interval = xhs_config.RELOGIN_PREDICTIVE_INTERVAL
        if self.request_count % interval != 0 or self.request_count == 0:
            return True
        print(f"[FETCH] 预测式检查：验证 {self.account.alias} cookie ...", file=sys.stderr)
        try:
            import xhs_login
            valid, user_info, updated = xhs_login.validate_cookies_online(
                self.cookies, fingerprint=self.account.fingerprint)
            if valid:
                self.cookies.update(updated)
                self.account.record_validation()
                nickname = (user_info or {}).get("nickname", "")
                print(f"[FETCH] 预测式检查通过{f' ({nickname})' if nickname else ''}", file=sys.stderr)
                return True
            # cookie 失效 → 尝试重登录
            print(f"[FETCH] 预测式检查发现 cookie 失效，尝试重登录...", file=sys.stderr)
            return self._attempt_relogin()
        except Exception as e:
            print(f"[FETCH] 预测式检查异常（不阻断）：{e}", file=sys.stderr)
            return True

    def _attempt_relogin(self) -> bool:
        """统一重登录入口。返回 True=成功恢复, False=失败。"""
        if not self.account.can_attempt_relogin():
            print(f"[FETCH] 账号 {self.account.alias} 重登录次数已满，跳过", file=sys.stderr)
            return False
        self.account.mark_relogin_attempt()
        print(f"[FETCH] 尝试重登录 {self.account.alias}（第 {self.account.relogin_attempts} 次）...",
              file=sys.stderr)
        try:
            import xhs_keepalive
            success, status = xhs_keepalive.keepalive_single_account(
                self.account.alias, self.account, force=True)
            if success:
                self.cookies = self.account.cookies
                self.cookie_meta = getattr(self.account, 'cookie_meta', {})
                self.session.cookies.clear()
                self.session.cookies.update(self.cookies)
                self.account.mark_relogin_success()
                print(f"[FETCH] 重登录成功: {status}", file=sys.stderr)
                return True
            print(f"[FETCH] 重登录失败: {status}", file=sys.stderr)
        except Exception as e:
            print(f"[FETCH] 重登录异常: {e}", file=sys.stderr)
        return False

    # -----------------------------------------------------------------
    # Layer 3: 会话休息（随机间隔，避免固定周期）
    # -----------------------------------------------------------------
    def _maybe_session_rest(self) -> None:
        if time.time() - self._session_start < self._session_active_duration:
            return
        rest = self._human_delay(600, 1800)  # 10-30 分钟，对数正态长尾
        print(f"[FETCH] 会话休息 {rest/60:.1f} 分钟（模拟离开）...", file=sys.stderr)
        time.sleep(rest)
        self._session_start = time.time()
        # 活跃时长也随机化：20-80 分钟，偶尔长偶尔短
        self._session_active_duration = self._human_delay(1200, 4800)
        # 休息后重建 HTTP Session（新 TLS 握手，模拟浏览器重启）
        try:
            self.session.close()
        except Exception:
            pass
        if _CURL_CFFI:
            _fp_imp = getattr(self.account, 'fingerprint', None)
            self.session = _HttpSession(impersonate=_fp_imp.impersonate if _fp_imp else xhs_config.IMPERSONATE_PROFILE)
        else:
            self.session = _HttpSession()
        self._apply_proxy()
        # 如果账号有绑定代理，优先使用（与 __init__ 保持一致）
        if self.account.proxy_url and self.proxy_pool:
            bound = self.proxy_pool.get_bound(self.account.proxy_url)
            if bound:
                self.current_proxy = bound
                self._apply_proxy()
        self.session.headers.update(xhs_config.base_headers())
        self.session.cookies.update(self.cookies)
        self._apply_fingerprint()
        self._warmed = False  # 休息后重新 warmup

    # -----------------------------------------------------------------
    # Layer 3: 请求多样性（随机间隔穿插辅助请求）
    # -----------------------------------------------------------------
    def __init_next_auxiliary_at(self) -> int:
        """随机化下次辅助请求触发计数（4-8 次之间）。"""
        return self._auxiliary_counter + random.randint(4, 8)

    def _maybe_auxiliary_request(self) -> None:
        self._auxiliary_counter += 1
        if not hasattr(self, '_next_auxiliary_at'):
            self._next_auxiliary_at = self.__init_next_auxiliary_at()
        if self._auxiliary_counter < self._next_auxiliary_at:
            return
        self._next_auxiliary_at = self.__init_next_auxiliary_at()
        actions = [
            # 刷新推荐流（模拟下拉刷新）
            ("POST", "/api/sns/web/v1/homefeed", {
                "cursor_score": "", "num": 18, "refresh_type": 1,
                "note_index": 0, "category": "homefeed_recommend",
            }),
            # 查看通知
            ("GET", "/api/sns/web/v1/you/notifications", None),
            # 查看另一类推荐（模拟切换分类 tab）
            ("POST", "/api/sns/web/v1/homefeed", {
                "cursor_score": "", "num": 18, "refresh_type": 1,
                "note_index": 0, "category": random.choice([
                    "homefeed.food_v3", "homefeed.travel_v3",
                    "homefeed.fashion_v3", "homefeed.beauty_v3",
                ]),
            }),
            # 查看个人信息（模拟点击头像/设置）
            ("GET", "/api/sns/web/v2/user/me", None),
        ]
        method, api, data = random.choice(actions)
        try:
            print(f"[FETCH] 辅助请求: {api}（增加多样性）", file=sys.stderr)
            self._call_raw(method, api, None, data, count=False)
            time.sleep(random.uniform(8, 30))
        except Exception:
            pass

    # -----------------------------------------------------------------
    # Layer 4: 时间窗口主动轮换
    # -----------------------------------------------------------------
    def _should_rotate_for_freshness(self) -> bool:
        active_time = time.time() - self._account_active_start
        if active_time > self._account_max_active:
            print(f"[FETCH] 账号 {self.account.alias} 已活跃 {active_time/60:.0f}min，主动轮换",
                  file=sys.stderr)
            return True
        if self._window_request_count >= 40:
            print(f"[FETCH] 账号 {self.account.alias} 本窗口已请求 {self._window_request_count} 次，主动轮换",
                  file=sys.stderr)
            return True
        return False

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
        if self.request_count >= xhs_config.IP_DAILY_CAP:
            raise FatalRiskError(f"达到 IP 日抓总上限 {xhs_config.IP_DAILY_CAP}，请明日再战")
        self._maybe_session_rest()
        # Layer 4: 主动轮换检查
        if self._should_rotate_for_freshness():
            rotated = self._rotate_account("主动轮换")
            if rotated:
                self._account_active_start = time.time()
                self._account_max_active = random.uniform(2400, 3600)
                self._window_request_count = 0
            else:
                # 轮换失败，缩短下次检查间隔（5-10 分钟后重试）
                self._account_active_start = time.time()
                self._account_max_active = random.uniform(300, 600)
        # 预测式 cookie 预检
        self._predictive_cookie_check()
        self.warmup()
        self._throttle()
        result = self._call_raw(method, api, params, data, count=True)
        self._window_request_count += 1
        # 定期持久化 cookie 到磁盘
        if self.request_count % xhs_config.COOKIE_PERSIST_INTERVAL == 0:
            self.account.save_cookies()
        return result

    def _referer_for(self, api: str, params: dict | None = None) -> str:
        """根据 API 路径返回合理的 referer，模拟真实浏览器导航来源。"""
        if "search" in api:
            # 搜索 referer 带上 keyword 参数
            keyword = ""
            if params and "keyword" in params:
                keyword = "?keyword=" + str(params["keyword"]) + "&source=web_search_result_notes"
            return xhs_config.WEB_BASE + "/search_result" + keyword
        elif "feed" in api or "homefeed" in api:
            return xhs_config.WEB_BASE + "/explore"
        elif "user_posted" in api or "otherinfo" in api:
            # 用户 profile referer 带上 user_id
            user_id = ""
            if params and "user_id" in params:
                user_id = "/" + str(params["user_id"])
            return xhs_config.WEB_BASE + "/user/profile" + user_id
        elif "note" in api and ("feed" not in api):
            # 笔记详情 referer
            note_id = ""
            if params and "note_id" in params:
                note_id = "/" + str(params["note_id"])
            return xhs_config.WEB_BASE + "/explore" + note_id
        elif "comment" in api:
            return xhs_config.WEB_BASE + "/explore/"
        elif "you/notifications" in api:
            return xhs_config.WEB_BASE + "/notification"
        elif "user/me" in api:
            return xhs_config.WEB_BASE + "/user/profile/self"
        else:
            return xhs_config.WEB_BASE + "/explore"

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
        _platform = self._current_platform()
        sign_headers = self.signer.sign(full_api, body, a1, method, platform=_platform)
        headers = {**self.session.headers, **sign_headers}

        # 动态 referer：根据 API 上下文设置，模拟真实浏览器导航
        headers["referer"] = self._referer_for(api, params)

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
            print(f"[FETCH] 网络异常：{e}（将重试 1 次）", file=sys.stderr)
            xhs_log.log_request(api, method, 0, None, str(e)[:80], duration,
                                 sign_mode=self._sign_mode_label,
                                 speed_mode=self._speed_mode_label,
                                 account=self.account.alias, proxy=proxy_label)
            # 网络异常可能是代理问题，切换代理后重试一次
            if self.current_proxy:
                self.current_proxy.mark_failure()
                self._rotate_proxy("网络异常")
            time.sleep(10)
            # 单次重试 — 重新签名（旧签名可能已过期）
            a1 = self.cookies.get("a1", "")
            sign_headers = self.signer.sign(full_api, body, a1, method, platform=_platform)
            fresh_headers = {**self.session.headers, **sign_headers}
            try:
                if method == "GET":
                    resp = self.session.get(url, headers=fresh_headers, timeout=20)
                else:
                    resp = self.session.post(
                        url,
                        headers=fresh_headers,
                        data=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
                        timeout=20,
                    )
            except Exception as e2:
                print(f"[FETCH] 重试仍然失败：{e2}", file=sys.stderr)
                raise e2 from e

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
            except Exception as e:
                print(f"[FETCH] 警告: cookie 同步失败 ({e})，签名与实际 cookie 可能不一致",
                      file=sys.stderr)

        # Cookie 完整性检查：首次请求后检查是否缺少动态 cookie
        if count and not self._cookie_integrity_warned and self.request_count >= 3:
            self._cookie_integrity_warned = True
            missing = xhs_config.OPTIONAL_COOKIE_KEYS - self.cookies.keys()
            if missing:
                print(f"[FETCH] 警告: cookie 缺少 {missing}，风控风险可能升高",
                      file=sys.stderr)

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
                    # 统一重登录：先走 keepalive fallback 链（限制递归深度防无限循环）
                    if _retry_depth < 3:
                        if self._attempt_relogin():
                            return self._call_raw(method, api, params, data, count=count, _retry_depth=_retry_depth + 1)
                        # 重登录失败 → 尝试换号
                        if self._rotate_account("cookie 失效"):
                            return self._call_raw(method, api, params, data, count=False, _retry_depth=_retry_depth + 1)
                    raise FatalRiskError("cookie 失效且无可用账号，请手动登录")
                if code == -104:
                    # 多层 b1 刷新 + 重试
                    if _retry_depth < 1:
                        refreshed = False
                        # Layer 1: 从缓存刷新 b1
                        print("[FETCH] -104 签名过期，尝试刷新 b1 缓存...", file=sys.stderr)
                        try:
                            if hasattr(self.signer, '_instances'):
                                emb = self.signer._instances.get("embed-js")
                                if emb:
                                    refreshed = emb.inject_b1()
                            elif hasattr(self.signer, 'inject_b1'):
                                refreshed = self.signer.inject_b1()
                        except Exception as e:
                            print(f"[FETCH] b1 缓存刷新失败: {e}", file=sys.stderr)
                        # Layer 2: 缓存未变 → 启动浏览器强制提取新 b1
                        if not refreshed:
                            print("[FETCH] b1 缓存未更新，启动浏览器强制提取...", file=sys.stderr)
                            try:
                                new_b1 = extract_b1_standalone()
                                if new_b1:
                                    if hasattr(self.signer, '_instances'):
                                        emb = self.signer._instances.get("embed-js")
                                        if emb:
                                            refreshed = emb.inject_b1(new_b1, force=True)
                                    elif hasattr(self.signer, 'inject_b1'):
                                        refreshed = self.signer.inject_b1(new_b1, force=True)
                                    print(f"[FETCH] 浏览器提取 b1 {'成功' if refreshed else '失败'}",
                                          file=sys.stderr)
                            except Exception as e:
                                print(f"[FETCH] 浏览器 b1 提取失败: {e}", file=sys.stderr)
                        return self._call_raw(method, api, params, data, count=count,
                                              _retry_depth=_retry_depth + 1)
                    # 搜索接口 -104：降级到浏览器 DOM 搜索
                    if "/search/" in api:
                        return self._dom_search_fallback(api, data)
                    # 非搜索接口 -104：等待后重试一次（b1 可能刚被刷新）
                    if _retry_depth < 1:
                        print(f"[FETCH] 非搜索接口 -104，等待 30s 后重试: {api}", file=sys.stderr)
                        time.sleep(random.uniform(20, 40))
                        return self._call_raw(method, api, params, data, count=count,
                                              _retry_depth=_retry_depth + 1)
                    raise FatalRiskError(f"接口 {api} 返回 -104（b1 刷新后仍失败）")
                # 未知错误码告警
                _KNOWN_CODES = {-100, -104, -101, -109, -102, -105, -110}
                if code not in _KNOWN_CODES:
                    print(f"[UNKNOWN-CODE] 未识别错误码 code={code} msg={msg} api={api}",
                          file=sys.stderr)
                else:
                    print(f"[FETCH] success=False code={code} msg={msg}", file=sys.stderr)
            self.consecutive_460 = 0
            return payload

        if status == 460:
            self.consecutive_460 += 1
            # 时间衰减：30 分钟无 460 则重置计数
            if self._last_460_time and time.time() - self._last_460_time > 1800:
                self._total_460_retries = 0
            self._last_460_time = time.time()
            self._total_460_retries += 1
            print(f"[FETCH] 460 风控 ×{self.consecutive_460}（累计重试 {self._total_460_retries}）", file=sys.stderr)
            if self._total_460_retries > 8:
                raise FatalRiskError("460 风控累计重试超过 8 次，建议换号或等待 24h")
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
                    return self._call_raw(method, api, params, data, count=False, _retry_depth=_retry_depth + 1)
                # 单账号：提示用户等待而非静默浏览器接管
                raise FatalRiskError(
                    f"单账号 {self.account.alias} 触发连续 460，已冷却 30 分钟。"
                    "请等待冷却结束后重试，或添加更多账号（login --name <alias>）"
                )
            time.sleep(random.uniform(60, 120))
            return self._call_raw(method, api, params, data, count=False, _retry_depth=_retry_depth + 1)

        if status == 461:
            if _retry_depth >= 3:
                raise FatalRiskError(
                    f"账号 {self.account.alias} 连续 3 次触发 461 验证码，"
                    "停止重试以免无限循环（可能需要人工过验证码或更换 IP）"
                )
            print(f"[FETCH] 461 验证码（重试 {_retry_depth + 1}/3）", file=sys.stderr)
            # 先尝试重登录（可能只是 cookie 过期导致的 461）
            if self._attempt_relogin():
                return self._call_raw(method, api, params, data, count=False, _retry_depth=_retry_depth + 1)
            # 重登录失败 → 原有冷却 + 换号逻辑
            self.account.mark_461()  # 使用默认 240min 冷却
            self.account_mgr.save_state()
            # 先尝试切账号
            if self._rotate_account("461 验证码"):
                return self._call_raw(method, api, params, data, count=False, _retry_depth=_retry_depth + 1)
            # 否则切浏览器接管（仅交互模式）
            if self.autonomous:
                raise FatalRiskError(
                    f"账号 {self.account.alias} 触发 461 验证码且无可用备选账号，"
                    "自主模式下跳过浏览器接管，请检查账号状态"
                )
            return self._browser_fetch(method, api, params, data)

        if status == 429:
            if _retry_depth >= 5:
                raise FatalRiskError("429 频率超限，连续重试 5 次仍被限流")
            backoff = 60 * (2 ** _retry_depth) + random.uniform(0, 30)
            print(f"[FETCH] 429 频率超限，指数退避 {backoff:.0f}s（重试 {_retry_depth + 1}/5）",
                  file=sys.stderr)
            time.sleep(backoff)
            return self._call_raw(method, api, params, data, count=False,
                                  _retry_depth=_retry_depth + 1)

        if status == 403:
            if _retry_depth >= 3:
                raise FatalRiskError("403 IP/UA 异常，连续重试 3 次仍被拒绝")
            print(f"[FETCH] 403 IP/UA 异常（重试 {_retry_depth + 1}/3）", file=sys.stderr)
            # 尝试轮换代理（IP 可能被标记）
            if self.current_proxy:
                self.current_proxy.mark_failure()
                self._rotate_proxy("403 IP 异常")
            time.sleep(random.uniform(30, 60))
            return self._call_raw(method, api, params, data, count=False,
                                  _retry_depth=_retry_depth + 1)

        # 406：签名头缺失（x-rap-param / x-xray-traceid）→ 重试一次，仍失败走浏览器接管
        if status == 406:
            if _retry_depth < 1:
                print(f"[FETCH] 406 签名头缺失，重试一次（{api}）", file=sys.stderr)
                return self._call_raw(method, api, params, data, count=False,
                                      _retry_depth=_retry_depth + 1)
            print(f"[FETCH] 406 持续，走浏览器接管（真实浏览器原生生成 x-rap/x-xray）", file=sys.stderr)
            if not self.autonomous:
                return self._browser_fetch(method, api, params, data)
            raise FatalRiskError(f"406 签名头缺失且自主模式无法浏览器接管: {api}")

        raise FatalRiskError(f"未知状态码 {status}: {resp.text[:200]}")

    def _dom_search_fallback(self, api: str, data: dict | None) -> dict:
        """搜索 API 返回 -104 时，降级到浏览器 __INITIAL_STATE__ 提取。"""
        # DOM 搜索也计入 IP 日抓总上限
        if self.request_count >= xhs_config.IP_DAILY_CAP:
            raise FatalRiskError(f"达到 IP 日抓总上限 {xhs_config.IP_DAILY_CAP}，请明日再战")
        # 注意：request_count 已在 _call_raw 中递增，此处不再重复计数
        if not data:
            return {"success": False, "code": -104, "data": {"items": [], "has_more": False}}
        keyword = data.get("keyword", "")
        page = data.get("page", 1)
        print(f"[FETCH] 搜索 -104，降级到浏览器 DOM 搜索：keyword={keyword} page={page}",
              file=sys.stderr)
        try:
            if self.dom_search is None:
                self.dom_search = PlaywrightDomSearch(self.cookies, platform=self._current_platform(), cookie_meta=self.cookie_meta)
            result = self.dom_search.search(keyword, page)
            n = len(result.get("items", []))
            print(f"[FETCH] DOM 搜索完成：{n} 条结果", file=sys.stderr)
            return {"success": True, "data": result, "code": 0}
        except Exception as e:
            print(f"[FETCH] DOM 搜索也失败：{e}", file=sys.stderr)
            raise FatalRiskError(f"搜索 API -104 且 DOM 搜索失败：{e}") from e

    # ── 搜索 API 小时配额 ────────────────────────────────────────

    class _SearchSlot:
        """预占的搜索配额槽位。confirm() 确认消耗，release() 释放回池。"""
        __slots__ = ("_ts", "_confirmed", "_released", "_owner")

        def __init__(self, ts: float, owner: "Fetcher"):
            self._ts = ts
            self._confirmed = False
            self._released = False
            self._owner = owner

        def confirm(self):
            self._confirmed = True

        def release(self):
            if not self._confirmed and not self._released:
                self._released = True
                try:
                    self._owner._search_api_calls.remove(self._ts)
                except ValueError:
                    pass

    def _reserve_search_slot(self):
        """预占一个搜索 API 配额槽位。返回 _SearchSlot 或 None（配额已满）。"""
        now = time.time()
        self._search_api_calls = [
            t for t in self._search_api_calls if now - t < 3600
        ]
        remaining = xhs_config.SEARCH_HOURLY_QUOTA - len(self._search_api_calls)
        if remaining <= 0:
            print(f"[SEARCH-QUOTA] 搜索 API 配额已满 "
                  f"({len(self._search_api_calls)}/{xhs_config.SEARCH_HOURLY_QUOTA})",
                  file=sys.stderr)
            return None
        ts = time.time()
        self._search_api_calls.append(ts)
        print(f"[SEARCH-QUOTA] 搜索 API 配额 "
              f"{len(self._search_api_calls)}/{xhs_config.SEARCH_HOURLY_QUOTA}，"
              f"剩余 {remaining - 1} 次", file=sys.stderr)
        return Fetcher._SearchSlot(ts, self)

    def search_dom(self, keyword: str, page: int = 1) -> dict:
        """直接走 DOM 搜索，跳过 API。用于配额已满或主动选择 DOM 模式。"""
        # DOM 搜索有独立上限，不计入 API 日抓限额（从 XHS 角度看是正常浏览器浏览）
        if self._dom_search_count >= xhs_config.DOM_SEARCH_DAILY_CAP:
            raise FatalRiskError(f"DOM 搜索日上限 {xhs_config.DOM_SEARCH_DAILY_CAP} 已满，请明日再战")
        # 注意：DOM 搜索仅检查独立上限，跳过节流/warmup/会话休息等安全层
        self._dom_search_count += 1
        self.account.dom_search_count = self._dom_search_count
        self.account.mark_used()
        # 避免连续启动浏览器（最小延迟，防止行为不像人）
        time.sleep(random.uniform(30, 90))
        try:
            if self.dom_search is None:
                self.dom_search = PlaywrightDomSearch(self.cookies, platform=self._current_platform(), cookie_meta=self.cookie_meta)
            result = self.dom_search.search(keyword, page)
            n = len(result.get("items", []))
            print(f"[SEARCH-QUOTA] DOM 搜索完成：keyword={keyword} page={page} "
                  f"结果={n}条", file=sys.stderr)
            return {"success": True, "data": result, "code": 0}
        except Exception as e:
            print(f"[SEARCH-QUOTA] DOM 搜索失败：{e}", file=sys.stderr)
            raise FatalRiskError(f"DOM 搜索失败：{e}") from e

    def _browser_fetch(
        self, method: str, api: str, params: dict | None, data: dict | None
    ) -> dict:
        # 使用独立 PlaywrightTakeover 浏览器（注入账号 cookies 后发 API 请求）
        # 真实浏览器自动附加全部签名头（x-s/x-t/x-xray/x-rap），绕过 406
        # 注意：不复用 PlaywrightSigner 的浏览器，因为它的上下文没有账号 cookies，
        # 且 Playwright 要求在 add_cookies 前先导航到目标域，时机难以协调。

        # 关闭 PlaywrightSigner 的浏览器（如果存在），避免 sync_playwright 双重启动
        # Playwright Sync API 基于 greenlets+asyncio，两个实例在同一线程会冲突
        signer = self.signer
        inner_pw = None
        if isinstance(signer, xhs_sign.AutoSigner):
            inner_pw = signer._instances.get("playwright")
        elif isinstance(signer, xhs_sign.PlaywrightSigner):
            inner_pw = signer
        if inner_pw is not None and hasattr(inner_pw, 'close'):
            inner_pw.close()

        if self.browser_takeover is None:
            self.browser_takeover = PlaywrightTakeover(self.cookies, headless=False,
                                                        cookie_meta=self.cookie_meta)
        try:
            payload = self.browser_takeover.fetch(method, api, params, data)
            self.consecutive_460 = 0
            return payload
        except Exception as e:
            print(f"[FETCH] 浏览器接管失败：{e}", file=sys.stderr)
            raise FatalRiskError(
                "风控强度超出处理能力，建议换号或等待 24h"
            ) from e

    def close(self) -> None:
        if self.dom_search:
            self.dom_search.close()
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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


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

    def __init__(self, cookies: dict[str, str], headless: bool = False,
                 cookie_meta: dict[str, dict] | None = None) -> None:
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
        # 注入 cookies（带 domain/path/secure 属性）
        self._inject_cookies(cookies, cookie_meta)
        self.page = self.ctx.new_page()
        # 注入 stealth 脚本隐藏 Playwright 自动化特征
        self.page.add_init_script(PlaywrightDomSearch._STEALTH_JS)
        try:
            from playwright_stealth import stealth_sync  # type: ignore
            stealth_sync(self.page)
        except ImportError:
            pass
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

    def _inject_cookies(self, cookies: dict[str, str],
                        cookie_meta: dict[str, dict] | None = None) -> None:
        """注入 cookies 到浏览器上下文（带 domain/path/secure 属性）。"""
        pw_cookies = []
        for n, v in cookies.items():
            pw_cookie: dict = {"name": n, "value": v}
            meta = (cookie_meta or {}).get(n)
            if meta:
                pw_cookie["domain"] = meta.get("domain", ".xiaohongshu.com")
                pw_cookie["path"] = meta.get("path", "/")
                if meta.get("secure"):
                    pw_cookie["secure"] = True
                if meta.get("httpOnly"):
                    pw_cookie["httpOnly"] = True
                if "sameSite" in meta:
                    pw_cookie["sameSite"] = meta["sameSite"]
                if "expires" in meta:
                    pw_cookie["expires"] = meta["expires"]
            else:
                pw_cookie["domain"] = ".xiaohongshu.com"
                pw_cookie["path"] = "/"
            pw_cookies.append(pw_cookie)
        if pw_cookies:
            self.ctx.add_cookies(pw_cookies)

    def close(self) -> None:
        try:
            self.ctx.close()
            self._pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# PlaywrightDomSearch：搜索 -104 时降级到浏览器导航 + __INITIAL_STATE__ 提取
# ---------------------------------------------------------------------------
# 基础 stealth 脚本（模块级常量，避免类定义期间的 NameError）
_STEALTH_JS_BASE = """
// 隐藏 navigator.webdriver
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
// 补全 window.chrome（headless 缺失）
if (!window.chrome) window.chrome = {runtime: {}, csi: function(){}, loadTimes: function(){}};
"""


class PlaywrightDomSearch:
    """搜索 API 返回 -104 时，降级到浏览器导航 + __INITIAL_STATE__ 提取。

    双模式启动：
    1. 真实浏览器模式（优先）：关闭用户 Edge → 用真实 profile 启动 → 有完整
       cookies + localStorage（含 b1 签名令牌）→ 页面 JS 自身 API 调用不受 -104 影响
    2. Playwright 降级模式：用 Playwright Chromium + 注入 cookies（best-effort）
    """

    @staticmethod
    def _stealth_for(platform: str = "Windows") -> str:
        """根据平台生成完整 stealth 脚本（WebGL 指纹与账号 OS 对齐）。"""
        if platform == "macOS":
            webgl = """
    const _origGetParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Apple Inc.';
        if (param === 37446) return 'Apple M1';
        return _origGetParam.call(this, param);
    };"""
        else:
            webgl = """
    const _origGetParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Google Inc. (Intel)';
        if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)';
        return _origGetParam.call(this, param);
    };"""
        return _STEALTH_JS_BASE + webgl

    # 兼容旧引用
    _STEALTH_JS = _stealth_for.__func__("Windows")

    def __init__(self, cookies: dict[str, str], headless: bool = True,
                 platform: str = "Windows",
                 cookie_meta: dict[str, dict] | None = None) -> None:
        self._mode = "none"
        self._pw = None
        self._ctx = None
        self._page = None
        self._channel = ""
        self._stealth_js = self._stealth_for(platform)
        self._cookie_meta = cookie_meta or {}

        # 优先：用真实浏览器 profile（有完整 cookies + localStorage）
        real = self._find_real_browser()
        if real:
            try:
                self._init_real_profile(real[0], real[1])
                return
            except Exception as e:
                print(f"[DOM-SEARCH] 真实浏览器模式失败: {e}，降级到 Playwright",
                      file=sys.stderr)

        # 降级：用 Playwright Chromium + 注入 cookies（best-effort）
        self._init_playwright_fallback(cookies, headless)

    # ------------------------------------------------------------------
    # 浏览器检测与进程管理（复用 xhs_login_native.py 模式）
    # ------------------------------------------------------------------

    @staticmethod
    def _find_real_browser() -> tuple[Path, str] | None:
        """返回 (profile_path, channel_name) 或 None。

        仅检测 Chromium 系浏览器（Firefox 不支持 Playwright channel 模式）。
        """
        import os
        if sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA", "")
            for name, channel in [
                ("Microsoft/Edge/User Data", "msedge"),
                ("Google/Chrome/User Data", "chrome"),
                ("BraveSoftware/Brave-Browser/User Data", "brave"),
            ]:
                p = os.path.join(local, name)
                if os.path.isdir(p):
                    return Path(p), channel
        elif sys.platform == "darwin":
            home = os.path.expanduser("~")
            for name, channel in [
                ("Library/Application Support/Google/Chrome", "chrome"),
                ("Library/Application Support/Microsoft Edge", "msedge"),
                ("Library/Application Support/BraveSoftware/Brave-Browser", "brave"),
            ]:
                p = os.path.join(home, name)
                if os.path.isdir(p):
                    return Path(p), channel
        else:
            # Linux
            home = os.path.expanduser("~")
            for name, channel in [
                (".config/google-chrome", "chrome"),
                (".config/microsoft-edge", "msedge"),
                (".config/BraveSoftware/Brave-Browser", "brave"),
            ]:
                p = os.path.join(home, name)
                if os.path.isdir(p):
                    return Path(p), channel
        return None

    @staticmethod
    def _close_running_browser(channel: str) -> None:
        """关闭运行中的浏览器（释放 profile 锁）。"""
        if sys.platform == "win32":
            proc_map = {"msedge": "msedge.exe", "chrome": "chrome.exe",
                        "brave": "brave.exe"}
            subprocess.run(["taskkill", "/IM", proc_map.get(channel, channel), "/F"],
                           capture_output=True, timeout=15)
        elif sys.platform == "darwin":
            app_map = {"msedge": "Microsoft Edge", "chrome": "Google Chrome",
                       "brave": "Brave Browser"}
            subprocess.run(["pkill", "-f", app_map.get(channel, channel)],
                           capture_output=True, timeout=15)
        else:
            proc_map = {"msedge": "msedge", "chrome": "google-chrome",
                        "brave": "brave-browser"}
            subprocess.run(["pkill", "-x", proc_map.get(channel, channel)],
                           capture_output=True, timeout=15)
        time.sleep(2)

    @staticmethod
    def _reopen_browser(channel: str) -> None:
        """搜索结束后重开浏览器给用户。"""
        if sys.platform == "win32":
            exe_map = {"msedge": "msedge.exe", "chrome": "chrome.exe",
                       "brave": "brave.exe"}
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            subprocess.Popen(["cmd", "/c", "start", "", exe_map.get(channel, channel)],
                             creationflags=flags)
        elif sys.platform == "darwin":
            app_map = {"msedge": "Microsoft Edge", "chrome": "Google Chrome",
                       "brave": "Brave Browser"}
            subprocess.Popen(["open", "-a", app_map.get(channel, channel)])
        else:
            cmd_map = {"msedge": "microsoft-edge", "chrome": "google-chrome",
                       "brave": "brave-browser"}
            subprocess.Popen([cmd_map.get(channel, channel)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)

    # ------------------------------------------------------------------
    # 初始化：真实浏览器 profile（优先）
    # ------------------------------------------------------------------

    def _init_real_profile(self, profile_path: Path, channel: str) -> None:
        """用真实浏览器 profile 启动，有完整 cookies + localStorage。"""
        from playwright.sync_api import sync_playwright  # type: ignore
        self._channel = channel

        print(f"[DOM-SEARCH] 检测到 {channel}，将短暂关闭浏览器以进行安全搜索...",
              file=sys.stderr)

        # 1. 关闭运行中的浏览器
        self._close_running_browser(channel)

        try:
            # 2. 用真实 profile 启动
            self._pw = sync_playwright().start()
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                channel=channel,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )

            # 3. 注入 stealth
            self._ctx.add_init_script(self._stealth_js)

            self._page = self._ctx.new_page()
            try:
                from playwright_stealth import stealth_sync  # type: ignore
                stealth_sync(self._page)
            except ImportError:
                pass

            self._mode = "real_profile"

            # 4. 导航到 XHS 建立会话
            self._page.goto(xhs_config.WEB_BASE + "/explore",
                            wait_until="domcontentloaded", timeout=30000)
            self._page.wait_for_timeout(3000)
            print(f"[DOM-SEARCH] 已连接真实浏览器 ({channel})", file=sys.stderr)
        except Exception as e:
            print(f"[DOM-SEARCH] 真实浏览器初始化失败: {e}，尝试重开浏览器...",
                  file=sys.stderr)
            # 清理失败的 Playwright 资源
            self.close()
            # 恢复用户浏览器
            try:
                self._reopen_browser(channel)
            except Exception:
                print(f"[DOM-SEARCH] 重开浏览器也失败，请手动打开 {channel}",
                      file=sys.stderr)
            raise

    # ------------------------------------------------------------------
    # 初始化：Playwright 降级模式
    # ------------------------------------------------------------------

    def _init_playwright_fallback(self, cookies: dict[str, str],
                                  headless: bool) -> None:
        """Playwright Chromium + 注入 cookies（best-effort，缺少 localStorage）。"""
        from playwright.sync_api import sync_playwright  # type: ignore
        self._pw = sync_playwright().start()
        profile = xhs_config.DATA_DIR / "pw_profile_dom_search"
        profile.mkdir(parents=True, exist_ok=True)
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self._ctx.add_init_script(self._stealth_js)
        self._inject_cookies(cookies, cookie_meta=getattr(self, '_cookie_meta', None))
        self._page = self._ctx.new_page()
        try:
            from playwright_stealth import stealth_sync  # type: ignore
            stealth_sync(self._page)
        except ImportError:
            pass
        self._page.goto(xhs_config.WEB_BASE + "/",
                        wait_until="domcontentloaded")
        self._page.wait_for_timeout(3000)
        self._mode = "playwright_fallback"
        print("[DOM-SEARCH] 降级到 Playwright 模式（缺少 localStorage，效果有限）",
              file=sys.stderr)

    # ------------------------------------------------------------------
    # Cookie 注入（仅降级模式需要）
    # ------------------------------------------------------------------

    def _inject_cookies(self, cookies: dict[str, str],
                        cookie_meta: dict[str, dict] | None = None) -> None:
        pw_cookies = []
        for n, v in cookies.items():
            pw_cookie: dict = {"name": n, "value": v}
            meta = (cookie_meta or {}).get(n)
            if meta:
                pw_cookie["domain"] = meta.get("domain", ".xiaohongshu.com")
                pw_cookie["path"] = meta.get("path", "/")
                if meta.get("secure"):
                    pw_cookie["secure"] = True
                if meta.get("httpOnly"):
                    pw_cookie["httpOnly"] = True
                if "sameSite" in meta:
                    pw_cookie["sameSite"] = meta["sameSite"]
                if "expires" in meta:
                    pw_cookie["expires"] = meta["expires"]
            else:
                pw_cookie["domain"] = ".xiaohongshu.com"
                pw_cookie["path"] = "/"
            pw_cookies.append(pw_cookie)
        if pw_cookies:
            self._ctx.add_cookies(pw_cookies)

    # ------------------------------------------------------------------
    # 搜索（两种模式共用）
    # ------------------------------------------------------------------

    @property
    def page(self):
        return self._page

    def search(self, keyword: str, page: int = 1) -> dict:
        """导航到搜索页，等待结果加载，提取数据。"""
        from urllib.parse import quote

        url = (
            f"{xhs_config.WEB_BASE}/search_result"
            f"?keyword={quote(keyword)}&source=web_search_result_notes&page={page}"
        )

        # 拦截搜索 API 响应（真实浏览器模式下，页面 JS 会调搜索 API）
        api_data: list[dict] = []

        def _on_response(resp):
            if "/search/notes" in resp.url and resp.status == 200:
                try:
                    body = resp.json()
                    items = (body.get("data") or {}).get("items") or []
                    if items:
                        api_data.extend(items)
                except Exception:
                    pass

        self._page.on("response", _on_response)

        # 导航到搜索页
        self._page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # 等待页面 JS 加载数据（真实模式需要更久，让 API 调用完成）
        wait_ms = 6000 + random.randint(1000, 3000) if self._mode == "real_profile" \
                  else 2000 + random.randint(500, 1500)
        self._page.wait_for_timeout(wait_ms)

        # 移除响应监听器
        try:
            self._page.remove_listener("response", _on_response)
        except Exception:
            pass

        # 优先使用拦截到的 API 数据（最完整，含 xsec_token）
        if api_data:
            print(f"[DOM-SEARCH] 拦截到 {len(api_data)} 条 API 搜索结果",
                  file=sys.stderr)
            return {"items": api_data,
                    "has_more": len(api_data) >= 10}

        # 降级：从 __INITIAL_STATE__ 提取
        raw = self._extract_state_js() or self._extract_state_html()
        if raw:
            result = self._parse_search_state(raw)
            if result.get("items"):
                return result

        # 最后兜底：从 DOM 提取 note_id
        return self._extract_from_dom()

    def _extract_state_js(self) -> dict | None:
        """通过 page.evaluate 提取 __INITIAL_STATE__。"""
        return self._page.evaluate("""() => {
            try {
                const state = window.__INITIAL_STATE__;
                if (!state) return null;
                return JSON.parse(JSON.stringify(state));
            } catch(e) { return null; }
        }""")

    def _extract_state_html(self) -> dict | None:
        """从 HTML 源码中 regex 匹配 __INITIAL_STATE__。"""
        import re
        html = self._page.content()
        m = re.search(
            r'window\.__INITIAL_STATE__\s*=\s*({.+?})\s*</script>',
            html, re.DOTALL,
        )
        if not m:
            return None
        clean = re.sub(r'\bundefined\b', 'null', m.group(1))
        try:
            return json.loads(clean)
        except (json.JSONDecodeError, ValueError):
            return None

    def _extract_from_dom(self) -> dict:
        """从 DOM 中提取 note_id 列表（最后兜底）。"""
        items = self._page.evaluate("""() => {
            const links = document.querySelectorAll('a[href*="/explore/"]');
            const seen = new Set();
            const results = [];
            for (const a of links) {
                const m = a.href.match(/\\/explore\\/([a-f0-9]{24})/);
                if (m && !seen.has(m[1])) {
                    seen.add(m[1]);
                    results.push({id: m[1], model_type: 'note', note_card: {}});
                }
            }
            return results;
        }""")
        return {"items": items, "has_more": False}

    def _parse_search_state(self, state: dict) -> dict:
        """将 __INITIAL_STATE__.search 转换为与 API 一致的格式。"""
        search_data = state.get("search") or {}
        items_raw: list = []

        # 多路径兼容
        notes = search_data.get("notes") or {}
        if isinstance(notes, dict):
            items_raw = notes.get("items") or []
        if not items_raw:
            items_raw = search_data.get("feeds") or []
        if not items_raw and isinstance(notes, dict):
            inner = notes.get("value") or {}
            if isinstance(inner, dict):
                items_raw = inner.get("items") or []

        items = []
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            mt = item.get("model_type", "")
            if mt and mt != "note":
                continue
            if "note_card" in item or "id" in item:
                item.setdefault("model_type", "note")
            items.append(item)

        return {"items": items, "has_more": bool(items) and len(items) >= 10}

    # ------------------------------------------------------------------
    # 关闭与清理
    # ------------------------------------------------------------------

    def close(self) -> None:
        channel = self._channel
        mode = self._mode
        try:
            if self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        # 真实浏览器模式下，关闭后重开浏览器给用户
        if mode == "real_profile" and channel:
            self._reopen_browser(channel)
