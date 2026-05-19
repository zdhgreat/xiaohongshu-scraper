"""代理池：data/proxies.txt 列表 + 轮换 + 失败冷却。

文件格式：每行一个代理 URL
  http://host:port
  http://user:pass@host:port
  socks5://host:port

或者 --proxy http://... 单代理用例（不读 proxies.txt）。

策略：
- 取下一个可用代理（round-robin + 跳过冷却中的）
- 失败标记冷却 5 分钟（首次）→ 30 分钟（连续多次失败）
- 全部冷却时返回 None（让 Fetcher 决定走直连或停抓）
"""

from __future__ import annotations

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
        # 失败次数越多冷却越久
        cooldown_min = min(5 * (2 ** (self.fail_count - 1)), 60)
        self.cooldown_until = time.time() + cooldown_min * 60
        print(f"[PROXY {self.label}] 失败 #{self.fail_count}，冷却 {cooldown_min}min",
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
        self.proxies = [Proxy(url=u) for u in urls]
        self._idx = 0

    def is_active(self) -> bool:
        return bool(self.proxies)

    def next_available(self) -> Proxy | None:
        if not self.proxies:
            return None
        n = len(self.proxies)
        for _ in range(n):
            p = self.proxies[self._idx % n]
            self._idx += 1
            if p.is_available():
                return p
        # 全部冷却中
        return None

    def get_bound(self, url: str) -> Proxy | None:
        """查找绑定到指定 URL 的代理。"""
        for p in self.proxies:
            if p.url == url:
                return p if p.is_available() else None
        return None

    def __len__(self) -> int:
        return len(self.proxies)
