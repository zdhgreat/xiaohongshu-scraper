"""测试 xhs_proxy 模块。"""
import time

from xhs_proxy import Proxy, ProxyPool


class TestProxy:
    def test_basic(self):
        p = Proxy(url="http://localhost:8080")
        assert p.url == "http://localhost:8080"
        assert p.is_available()

    def test_cooldown(self):
        p = Proxy(url="http://localhost:8080")
        p.cooldown_until = time.time() + 300
        assert not p.is_available()

    def test_mark_failure(self):
        p = Proxy(url="http://localhost:8080")
        p.mark_failure()
        assert not p.is_available()
        assert p.fail_count == 1

    def test_mark_success_resets_fail_count(self):
        p = Proxy(url="http://localhost:8080")
        p.mark_failure()
        p.mark_success()
        assert p.fail_count == 0  # fail_count reset
        assert p.total_calls == 1
        # cooldown_until not cleared by mark_success (by design)


class TestProxyPool:
    def test_empty(self):
        pool = ProxyPool(None)
        assert not pool.is_active()

    def test_round_robin(self):
        p1 = Proxy(url="http://h1:8080")
        p2 = Proxy(url="http://h2:8080")
        pool = ProxyPool.__new__(ProxyPool)
        pool.proxies = [p1, p2]
        pool._idx = 0

        first = pool.next_available()
        assert first is not None
        second = pool.next_available()
        assert second is not None
        assert first.url != second.url  # 轮询

    def test_skip_cooldown(self):
        p1 = Proxy(url="http://h1:8080")
        p1.cooldown_until = time.time() + 3600
        p2 = Proxy(url="http://h2:8080")
        pool = ProxyPool.__new__(ProxyPool)
        pool.proxies = [p1, p2]
        pool._idx = 0

        selected = pool.next_available()
        assert selected is not None
        assert selected.url == "http://h2:8080"  # 跳过冷却中的 p1
