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
    DAILY_HARD_CAP, COOKIES_PATH as LEGACY_COOKIES,
    ACCOUNTS_DIR, ACCOUNTS_STATE, restrict_file as _restrict_file,
    assign_fingerprint,
)


class AccountError(RuntimeError):
    pass


@dataclass
class Account:
    alias: str
    cookies_path: Path
    cookies: dict[str, str] = field(default_factory=dict)
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
    speed_mode: str | None = None  # 账号专属速率：None=跟随全局 CLI 参数

    def load(self) -> None:
        if not self.cookies_path.exists():
            raise AccountError(f"account file missing: {self.cookies_path}")
        self.cookies = json.loads(self.cookies_path.read_text(encoding="utf-8"))

    def save_cookies(self) -> None:
        self.cookies_path.parent.mkdir(parents=True, exist_ok=True)
        self.cookies_path.write_text(json.dumps(self.cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        _restrict_file(self.cookies_path)

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
        if self.daily_count >= DAILY_HARD_CAP:
            return False, f"daily cap {DAILY_HARD_CAP} 已满"
        return True, "ok"

    def mark_used(self) -> None:
        now = time.time()
        today = date.today().isoformat()
        if self.daily_date != today:
            self.daily_count = 0
            self.daily_date = today
        self.daily_count += 1
        self.total_calls += 1
        self.last_used = now

    def mark_460(self, cooldown_min: int = 30) -> None:
        self.last_460_count += 1
        self.cooldown_until = time.time() + cooldown_min * 60
        print(f"[ACCOUNT {self.alias}] 触发 460，冷却 {cooldown_min} 分钟（累计 460×{self.last_460_count}）",
              file=sys.stderr)

    def mark_461(self, cooldown_min: int = 120) -> None:
        self.last_461_count += 1
        self.cooldown_until = time.time() + cooldown_min * 60
        print(f"[ACCOUNT {self.alias}] 触发 461，冷却 {cooldown_min} 分钟（累计 461×{self.last_461_count}）",
              file=sys.stderr)

    def mark_invalid(self) -> None:
        """启动预检失败，24h 冷却。"""
        self.cooldown_until = time.time() + 24 * 3600
        print(f"[ACCOUNT {self.alias}] cookie 验证失败，冷却 24 小时", file=sys.stderr)


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

    def save_state(self) -> None:
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
        } for a in self.accounts.values()}
        try:
            ACCOUNTS_STATE.parent.mkdir(parents=True, exist_ok=True)
            # 原子写入：先写临时文件再 rename，防止多进程并发写时读到半写状态
            tmp = ACCOUNTS_STATE.with_suffix('.tmp')
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(ACCOUNTS_STATE)
        except OSError:
            pass

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
        """选 last_used 最久未用且可用的账号。"""
        if not self.accounts:
            raise AccountError("无可用账号；先跑 login 命令")
        # 按 last_used 升序（最久未用优先）
        candidates = sorted(self.accounts.values(), key=lambda a: a.last_used)
        skipped = []
        for acc in candidates:
            ok, reason = acc.is_available()
            if ok:
                return acc
            skipped.append(f"{acc.alias}({reason})")
        raise AccountError(f"所有账号不可用：{', '.join(skipped)}")

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
