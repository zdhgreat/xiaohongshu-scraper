"""账号池：多账号轮换 + daily count + 风控冷却。

存储约定：
  data/cookies.json           — 默认账号（兼容老路径）
  data/accounts/<alias>.json  — 多账号文件（每个一个 alias）

每个账号文件就是一个 cookies dict（同 login 命令产物）。

AccountManager 责任：
- 加载所有可用账号
- 轮换策略：next_available()（按 last_used 最久未用优先；带冷却跳过）
- 触发风控：mark_cooldown(seconds) 让账号短期冷却
- 日抓计数：mark_used()；超 DAILY_HARD_CAP 自动冷却到次日
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any

from xhs_config import (
    COOKIES_PATH as LEGACY_COOKIES,
    ACCOUNTS_DIR, ACCOUNTS_STATE, restrict_file as _restrict_file,
    assign_fingerprint,
)
import xhs_config  # 用 xhs_config.XXX 访问可变配置，避免 from import 绑定导入时值


class AccountError(RuntimeError):
    pass


@dataclass
class Account:
    alias: str
    cookies_path: Path
    cookies: dict[str, str] = field(default_factory=dict)
    cookie_meta: dict[str, dict] = field(default_factory=dict)  # name → {domain, path, secure, ...}
    # 运行时状态（持久化在 accounts_state.json）
    last_used: float = 0.0
    cooldown_until: float = 0.0
    daily_count: int = 0
    daily_date: str = ""
    last_460_count: int = 0
    last_461_count: int = 0
    total_calls: int = 0
    fingerprint: Any = None  # FingerprintProfile，由 _load_accounts 分配
    proxy_url: str | None = None  # 绑定的专属代理 URL
    speed_mode: str | None = None  # 账号专属速率（已废弃，统一 paranoid）
    health_score: float = 1.0  # 账号健康分 0.0-1.0，低于 0.3 标记需重新登录
    dom_search_count: int = 0  # DOM 搜索日计数（独立于 API 日抓计数）
    # 重登录状态
    last_validate_ts: float = 0.0      # 上次成功在线验证 cookie 的时间
    relogin_attempts: int = 0          # 当前重登录尝试计数
    relogin_last_ts: float = 0.0      # 上次重登录尝试时间

    def load(self) -> None:
        if not self.cookies_path.exists():
            raise AccountError(f"account file missing: {self.cookies_path}")
        raw = json.loads(self.cookies_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("_version") == 2:
            self.cookies = raw.get("cookies", {})
            self.cookie_meta = raw.get("cookie_meta", {})
        else:
            # v1: flat dict[str, str]
            self.cookies = raw
            self.cookie_meta = {}

    def save_cookies(self) -> None:
        """持久化 cookies 到 JSON 文件（原子写入）。"""
        try:
            self.cookies_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"_version": 2, "cookies": self.cookies}
            if self.cookie_meta:
                data["cookie_meta"] = self.cookie_meta
            tmp = self.cookies_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.cookies_path)
        except OSError as e:
            print(f"[ACCOUNTS] 警告: 保存 cookies 失败: {e}", file=sys.stderr)

    def is_available(self) -> tuple[bool, str]:
        now = time.time()
        if now < self.cooldown_until:
            wait = int(self.cooldown_until - now)
            return False, f"cooldown {wait}s"
        # 检查日计数
        today = date.today().isoformat()
        if self.daily_date != today:
            self.daily_count = 0
            self.daily_date = today
        if self.daily_count >= xhs_config.DAILY_HARD_CAP:
            return False, f"daily cap {xhs_config.DAILY_HARD_CAP} 已满"
        return True, "ok"

    def mark_used(self) -> None:
        now = time.time()
        today = date.today().isoformat()
        if self.daily_date != today:
            self.daily_count = 0
            self.dom_search_count = 0
            self.daily_date = today
        self.daily_count += 1
        self.total_calls += 1
        self.last_used = now

    def mark_460(self, cooldown_min: int = 30) -> None:
        import random as _random
        self.last_460_count += 1
        self.health_score = max(0.0, self.health_score - 0.15)
        jitter = _random.uniform(-300, 600)  # ±5-10 分钟抖动
        actual = max(cooldown_min * 60 + jitter, 600)  # 至少 10 分钟
        self.cooldown_until = time.time() + actual
        print(f"[ACCOUNT {self.alias}] 触发 460，冷却 {actual/60:.0f} 分钟（健康 {self.health_score:.2f}）",
              file=sys.stderr)

    def mark_461(self, cooldown_min: int = 240) -> None:
        import random as _random
        self.last_461_count += 1
        self.health_score = max(0.0, self.health_score - 0.3)
        jitter = _random.uniform(-900, 1800)  # ±15-30 分钟抖动
        actual = max(cooldown_min * 60 + jitter, 1800)  # 至少 30 分钟
        self.cooldown_until = time.time() + actual
        print(f"[ACCOUNT {self.alias}] 触发 461，冷却 {actual/60:.0f} 分钟（健康 {self.health_score:.2f}）",
              file=sys.stderr)

    def mark_invalid(self) -> None:
        """启动预检失败，24h 冷却。"""
        self.cooldown_until = time.time() + 24 * 3600
        print(f"[ACCOUNT {self.alias}] cookie 验证失败，冷却 24 小时", file=sys.stderr)

    def record_validation(self) -> None:
        """记录成功在线验证，重置重登录计数，恢复健康分。"""
        self.last_validate_ts = time.time()
        self.relogin_attempts = 0
        self.health_score = min(1.0, self.health_score + 0.05)

    def can_attempt_relogin(self) -> bool:
        """是否还能尝试重登录。"""
        if self.relogin_attempts >= xhs_config.RELOGIN_MAX_ATTEMPTS:
            elapsed = time.time() - self.relogin_last_ts
            if elapsed < xhs_config.RELOGIN_COOLDOWN_MIN * 60:
                return False
            # 冷却过了，重置计数给一次机会
            self.relogin_attempts = 0
            return True
        return True

    def mark_relogin_attempt(self) -> None:
        """记录一次重登录尝试。"""
        self.relogin_attempts += 1
        self.relogin_last_ts = time.time()

    def mark_relogin_success(self) -> None:
        """记录重登录成功。

        注意：不完全归零 relogin_attempts，因为 461 场景下 relogin "成功"
        （cookie 刷新了）不代表 461 被解决。递减而非归零，确保连续 461
        时计数器仍能累积到 RELILOGIN_MAX_ATTEMPTS 上限。
        """
        self.relogin_attempts = max(0, self.relogin_attempts - 1)
        self.last_validate_ts = time.time()


class AccountManager:
    def __init__(self) -> None:
        self.accounts: dict[str, Account] = {}
        self._load_accounts()
        self._restore_state()

    def _load_accounts(self) -> None:
        # 1. 多账号文件
        if ACCOUNTS_DIR.exists():
            for f in sorted(ACCOUNTS_DIR.glob("*.json")):
                alias = f.stem
                try:
                    acc = Account(alias=alias, cookies_path=f)
                    acc.load()
                    acc.fingerprint = assign_fingerprint(alias)
                    self.accounts[alias] = acc
                except AccountError as e:
                    print(f"[ACCOUNT] 跳过 {alias}: {e}", file=sys.stderr)
        # 2. 兼容老路径 data/cookies.json
        if not self.accounts and LEGACY_COOKIES.exists():
            try:
                acc = Account(alias="default", cookies_path=LEGACY_COOKIES)
                acc.load()
                acc.fingerprint = assign_fingerprint("default")
                self.accounts["default"] = acc
            except AccountError:
                pass

    def _restore_state(self) -> None:
        if not ACCOUNTS_STATE.exists():
            return
        try:
            state = json.loads(ACCOUNTS_STATE.read_text(encoding="utf-8"))
        except Exception:
            return
        for alias, s in state.items():
            if alias in self.accounts:
                a = self.accounts[alias]
                a.last_used = s.get("last_used", 0)
                a.cooldown_until = s.get("cooldown_until", 0)
                a.daily_count = s.get("daily_count", 0)
                a.daily_date = s.get("daily_date", "")
                a.last_460_count = s.get("last_460_count", 0)
                a.last_461_count = s.get("last_461_count", 0)
                a.total_calls = s.get("total_calls", 0)
                a.proxy_url = s.get("proxy_url")
                a.speed_mode = s.get("speed_mode")
                a.last_validate_ts = s.get("last_validate_ts", 0)
                a.relogin_attempts = s.get("relogin_attempts", 0)
                a.relogin_last_ts = s.get("relogin_last_ts", 0)
                a.health_score = s.get("health_score", 1.0)
                a.dom_search_count = s.get("dom_search_count", 0)

    def save_state(self) -> None:
        # 先从磁盘读取最新状态，合并其他进程可能写入的更新
        disk_state: dict = {}
        if ACCOUNTS_STATE.exists():
            try:
                disk_state = json.loads(ACCOUNTS_STATE.read_text(encoding="utf-8"))
            except Exception:
                pass

        state = {a.alias: {
            "last_used": a.last_used,
            "cooldown_until": a.cooldown_until,
            "daily_count": a.daily_count,
            "daily_date": a.daily_date,
            "last_460_count": a.last_460_count,
            "last_461_count": a.last_461_count,
            "total_calls": a.total_calls,
            "proxy_url": a.proxy_url,
            "speed_mode": a.speed_mode,
            "last_validate_ts": a.last_validate_ts,
            "relogin_attempts": a.relogin_attempts,
            "relogin_last_ts": a.relogin_last_ts,
            "health_score": a.health_score,
            "dom_search_count": a.dom_search_count,
        } for a in self.accounts.values()}
        # 保留磁盘上存在但本进程未加载的账号状态（跨进程保护）
        for alias, s in disk_state.items():
            if alias not in state:
                state[alias] = s
        try:
            ACCOUNTS_STATE.parent.mkdir(parents=True, exist_ok=True)
            # 原子写入：先写临时文件再 rename，防止多进程并发写时读到半写状态
            tmp = ACCOUNTS_STATE.with_suffix('.tmp')
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(ACCOUNTS_STATE)
        except OSError as e:
            print(f"[ACCOUNTS] 警告: 保存状态失败: {e}", file=sys.stderr)

    def has_accounts(self) -> bool:
        return bool(self.accounts)

    def get(self, alias: str | None = None) -> Account:
        """取指定账号或 next_available。"""
        if alias:
            if alias not in self.accounts:
                raise AccountError(f"unknown account {alias}; 可用：{list(self.accounts.keys())}")
            acc = self.accounts[alias]
            ok, reason = acc.is_available()
            if not ok:
                raise AccountError(f"account {alias} 不可用：{reason}")
            return acc
        return self.next_available()

    def next_available(self) -> Account:
        """选可用的账号：从最久未用的 2-3 个中随机选一个（打破确定性轮转模式）。"""
        import random as _random
        if not self.accounts:
            raise AccountError("无可用账号；先跑 login 命令")
        # 按 last_used 升序（最久未用优先），健康分低的排后面
        candidates = sorted(self.accounts.values(),
                           key=lambda a: (a.last_used, -a.health_score))
        skipped = []
        available = []
        for acc in candidates:
            ok, reason = acc.is_available()
            if ok:
                available.append(acc)
            else:
                skipped.append(f"{acc.alias}({reason})")
        if not available:
            raise AccountError(f"所有账号不可用：{', '.join(skipped)}")
        # 从最久未用的 2-3 个中随机选，打破 A→B→C 确定性模式
        pool = available[:min(3, len(available))]
        return _random.choice(pool)

    def wait_for_available(self, timeout: float = 600) -> Account:
        """等待直到有账号可用。自动计算最短等待时间并 sleep。

        Args:
            timeout: 最大等待秒数（默认 10 分钟）。

        Raises:
            AccountError: 超时仍无可用账号。
        """
        import sys as _sys
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return self.next_available()
            except AccountError:
                pass
            # 计算最早恢复的账号的冷却剩余时间
            min_wait = None
            for acc in self.accounts.values():
                if acc.cooldown_until > time.time():
                    remaining = acc.cooldown_until - time.time() + 5  # 多等 5 秒
                    if min_wait is None or remaining < min_wait:
                        min_wait = remaining
            if min_wait is None:
                min_wait = 30  # 无冷却但不可用，等 30 秒重试
            sleep_time = min(min_wait, deadline - time.time(), 120)  # 单次最多等 2 分钟
            if sleep_time <= 0:
                break
            print(f"[ACCOUNTS] 所有账号冷却中，等待 {int(sleep_time)}s...",
                  file=_sys.stderr)
            time.sleep(sleep_time)
        return self.next_available()  # 超时后再试一次，不行就抛异常

    def stats(self) -> list[dict[str, Any]]:
        return [{
            "alias": a.alias,
            "daily_count": a.daily_count,
            "total_calls": a.total_calls,
            "last_460": a.last_460_count,
            "last_461": a.last_461_count,
            "cooldown_until": datetime.fromtimestamp(a.cooldown_until).isoformat() if a.cooldown_until > time.time() else "",
            "last_used": datetime.fromtimestamp(a.last_used).isoformat() if a.last_used else "",
        } for a in self.accounts.values()]
