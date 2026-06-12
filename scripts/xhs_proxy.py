"""代理池：data/proxies.txt 列表 + 轮换 + 失败冷却。

文件格式：每行一个代理 URL
  http://host:port
  http://user:pass@host:port
  socks5://host:port

或者 --proxy http://... 单代理用例（不读 proxies.txt）。

策略：
- 取下一个可用代理（round-robin + 跳过冷却中的）
- 失败标记冷却 5 分钟（首次）→ 30 分钟（连续多次失败），加随机抖动
- 全部冷却时返回 None（让 Fetcher 决定走直连或停抓）
"""

from __future__ import annotations

import random as _random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXIES_FILE = ROOT / "data" / "proxies.txt"


@dataclass
class Proxy:
    url: str
    fail_count: int = 0
    cooldown_until: float = 0.0
    total_calls: int = 0

    @property
    def label(self) -> str:
        # 把账密剥掉，只留 host:port 给日志
        u = self.url
        if "@" in u:
            u = u.split("@", 1)[1]
        if "://" in u:
            u = u.split("://", 1)[1]
        return u

    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until

    def mark_failure(self) -> None:
        self.fail_count += 1
        # 失败次数越多冷却越久，加随机抖动
        cooldown_min = min(5 * (2 ** (self.fail_count - 1)), 60)
        jitter = _random.uniform(-60, 120)  # ±1-2 分钟抖动
        actual = max(cooldown_min * 60 + jitter, 60)  # 至少 1 分钟
        self.cooldown_until = time.time() + actual
        print(f"[PROXY {self.label}] 失败 #{self.fail_count}，冷却 {actual/60:.0f}min",
              file=sys.stderr)

    def mark_success(self) -> None:
        self.fail_count = 0
        self.total_calls += 1


class ProxyPool:
    def __init__(self, proxies: list[str] | None = None) -> None:
        urls: list[str] = []
        if proxies:
            urls = [p.strip() for p in proxies if p.strip()]
        elif PROXIES_FILE.exists():
            urls = [
                line.strip()
                for line in PROXIES_FILE.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            # 保护含认证信息的代理文件权限
            try:
                from xhs_config import restrict_file as _rf
                _rf(PROXIES_FILE)
            except Exception:
                pass
        self.proxies = [Proxy(url=u) for u in urls]
        self._idx = 0

    def is_active(self) -> bool:
        return bool(self.proxies)

    def next_available(self) -> Proxy | None:
        """从所有可用代理中随机选一个（打破确定性 Round-Robin 模式）。"""
        if not self.proxies:
            return None
        available = [p for p in self.proxies if p.is_available()]
        if not available:
            return None
        return _random.choice(available)

    def get_bound(self, url: str) -> Proxy | None:
        """查找绑定到指定 URL 的代理。"""
        for p in self.proxies:
            if p.url == url:
                return p if p.is_available() else None
        return None

    def earliest_recovery(self) -> float | None:
        """返回最快可用的代理还需等待的秒数；无代理或已有可用时返回 None。"""
        if not self.proxies:
            return None
        now = time.time()
        min_wait = None
        for p in self.proxies:
            if p.is_available():
                return None  # 已有可用代理
            wait = p.cooldown_until - now
            if min_wait is None or wait < min_wait:
                min_wait = wait
        return max(min_wait, 0) if min_wait is not None else None

    def __len__(self) -> int:
        return len(self.proxies)
