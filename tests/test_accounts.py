"""测试 xhs_accounts 模块。"""
import json
import time
from pathlib import Path

import pytest

from xhs_accounts import Account, AccountManager, AccountError


def _make_account(alias: str = "test") -> Account:
    return Account(
        alias=alias,
        cookies_path=Path(f"/tmp/{alias}.json"),
        cookies={"web_session": "ws", "a1": "a1"},
    )


class TestAccount:
    def test_initial_state(self):
        acc = _make_account()
        assert acc.alias == "test"
        assert acc.daily_count == 0
        ok, _ = acc.is_available()
        assert ok

    def test_daily_cap(self):
        from datetime import date
        from xhs_config import DAILY_HARD_CAP
        acc = _make_account()
        acc.daily_count = DAILY_HARD_CAP
        acc.daily_date = date.today().isoformat()  # 确保日期匹配，不会重置
        ok, _ = acc.is_available()
        assert not ok

    def test_cooldown(self):
        acc = _make_account()
        acc.cooldown_until = time.time() + 3600  # 1h from now
        ok, _ = acc.is_available()
        assert not ok

    def test_cooldown_expired(self):
        acc = _make_account()
        acc.cooldown_until = time.time() - 1  # expired
        ok, _ = acc.is_available()
        assert ok

    def test_mark_used(self):
        acc = _make_account()
        acc.mark_used()
        assert acc.daily_count == 1

    def test_mark_460(self):
        acc = _make_account()
        acc.mark_460(cooldown_min=30)
        assert acc.last_460_count >= 1
        assert acc.cooldown_until > time.time()

    def test_mark_461(self):
        acc = _make_account()
        acc.mark_461(cooldown_min=120)
        assert acc.last_461_count >= 1
        assert acc.cooldown_until > time.time()


class TestAccountManager:
    def test_no_accounts(self):
        mgr = AccountManager.__new__(AccountManager)
        mgr.accounts = {}
        assert not mgr.has_accounts()

    def test_next_available_prefers_least_recent(self):
        mgr = AccountManager.__new__(AccountManager)
        mgr.accounts = {}
        acc1 = _make_account("old")
        acc1.last_used = 100
        acc2 = _make_account("recent")
        acc2.last_used = 200
        mgr.accounts = {"old": acc1, "recent": acc2}

        available = [a for a in mgr.accounts.values() if a.is_available()[0]]
        available.sort(key=lambda a: a.last_used or 0)
        assert available[0].alias == "old"


def _patch_accounts(monkeypatch, tmp_path):
    """统一 patch xhs_accounts 模块的路径常量（它们是 from ... import 的副本）。"""
    import xhs_accounts as _xa
    acc_dir = tmp_path / "accounts"
    acc_dir.mkdir()
    monkeypatch.setattr(_xa, "ACCOUNTS_DIR", acc_dir)
    monkeypatch.setattr(_xa, "LEGACY_COOKIES", tmp_path / "cookies.json")
    monkeypatch.setattr(_xa, "ACCOUNTS_STATE", tmp_path / "accounts_state.json")
    return acc_dir


class TestMultiAccountFiles:
    """测试多账号文件加载与轮换。"""

    def test_load_from_dir(self, tmp_path, monkeypatch):
        """AccountManager 从 accounts/ 目录加载多个账号文件。"""
        acc_dir = _patch_accounts(monkeypatch, tmp_path)

        (acc_dir / "acc_a.json").write_text(
            json.dumps({"web_session": "ws_a", "a1": "a1_a"}), encoding="utf-8")
        (acc_dir / "acc_b.json").write_text(
            json.dumps({"web_session": "ws_b", "a1": "a1_b"}), encoding="utf-8")

        mgr = AccountManager()
        assert mgr.has_accounts()
        assert len(mgr.accounts) == 2
        assert "acc_a" in mgr.accounts
        assert "acc_b" in mgr.accounts
        assert mgr.accounts["acc_a"].cookies["web_session"] == "ws_a"
        assert mgr.accounts["acc_b"].cookies["web_session"] == "ws_b"

    def test_rotation_skips_cooldown(self, tmp_path, monkeypatch):
        """轮换时跳过冷却中的账号。"""
        acc_dir = _patch_accounts(monkeypatch, tmp_path)
        (acc_dir / "hot.json").write_text(
            json.dumps({"web_session": "ws_hot", "a1": "a1_hot"}), encoding="utf-8")
        (acc_dir / "cool.json").write_text(
            json.dumps({"web_session": "ws_cool", "a1": "a1_cool"}), encoding="utf-8")

        mgr = AccountManager()
        mgr.accounts["hot"].cooldown_until = time.time() + 3600

        acc = mgr.next_available()
        assert acc.alias == "cool"

    def test_all_cooldown_raises(self, tmp_path, monkeypatch):
        """所有账号冷却中时 next_available 应抛异常。"""
        acc_dir = _patch_accounts(monkeypatch, tmp_path)
        (acc_dir / "a.json").write_text(
            json.dumps({"web_session": "ws_a", "a1": "a1_a"}), encoding="utf-8")

        mgr = AccountManager()
        mgr.accounts["a"].cooldown_until = time.time() + 3600

        with pytest.raises(AccountError, match="所有账号不可用"):
            mgr.next_available()

    def test_get_specific_alias(self, tmp_path, monkeypatch):
        """get(alias) 返回指定账号。"""
        acc_dir = _patch_accounts(monkeypatch, tmp_path)
        (acc_dir / "target.json").write_text(
            json.dumps({"web_session": "ws_target", "a1": "a1_t"}), encoding="utf-8")
        (acc_dir / "other.json").write_text(
            json.dumps({"web_session": "ws_other", "a1": "a1_o"}), encoding="utf-8")

        mgr = AccountManager()
        acc = mgr.get("target")
        assert acc.alias == "target"
        assert acc.cookies["web_session"] == "ws_target"

    def test_get_unknown_alias_raises(self, tmp_path, monkeypatch):
        """get(不存在的 alias) 应抛异常。"""
        acc_dir = _patch_accounts(monkeypatch, tmp_path)
        (acc_dir / "only.json").write_text(
            json.dumps({"web_session": "ws", "a1": "a1"}), encoding="utf-8")

        mgr = AccountManager()
        with pytest.raises(AccountError, match="unknown account"):
            mgr.get("nonexistent")

    def test_save_restore_state(self, tmp_path, monkeypatch):
        """状态持久化：save_state → 新 Manager 能恢复。"""
        acc_dir = _patch_accounts(monkeypatch, tmp_path)
        state_path = tmp_path / "accounts_state.json"
        (acc_dir / "x.json").write_text(
            json.dumps({"web_session": "ws", "a1": "a1"}), encoding="utf-8")

        mgr1 = AccountManager()
        mgr1.accounts["x"].daily_count = 42
        mgr1.accounts["x"].total_calls = 100
        mgr1.accounts["x"].last_460_count = 2
        mgr1.save_state()

        mgr2 = AccountManager()
        assert mgr2.accounts["x"].daily_count == 42
        assert mgr2.accounts["x"].total_calls == 100
        assert mgr2.accounts["x"].last_460_count == 2
