"""Tests for xhs_login_native — cross-platform cookie extraction."""

import sys
from unittest.mock import MagicMock, patch

import pytest

import xhs_login_native


# ── Profile directory ──────────────────────────────────────────────

class TestProfileDir:
    def test_returns_none_for_unknown_browser(self):
        assert xhs_login_native._profile_dir("firefox") is None

    def test_returns_string_for_edge_or_none(self):
        result = xhs_login_native._profile_dir("edge")
        assert result is None or isinstance(result, str)

    def test_returns_string_for_chrome_or_none(self):
        result = xhs_login_native._profile_dir("chrome")
        assert result is None or isinstance(result, str)


# ── Process name ───────────────────────────────────────────────────

class TestProcessName:
    def test_edge(self):
        name = xhs_login_native._process_name("edge")
        assert isinstance(name, str)
        assert len(name) > 0

    def test_chrome(self):
        name = xhs_login_native._process_name("chrome")
        assert isinstance(name, str)
        assert len(name) > 0


# ── Browser running check ─────────────────────────────────────────

class TestIsBrowserRunning:
    def test_returns_bool(self):
        result = xhs_login_native._is_browser_running("edge")
        assert isinstance(result, bool)

    def test_handles_exception(self):
        with patch("subprocess.run", side_effect=OSError("test")):
            assert xhs_login_native._is_browser_running("edge") is False


# ── Close / reopen browser ─────────────────────────────────────────

class TestCloseBrowser:
    def test_does_not_raise(self):
        # Just verify it doesn't crash
        xhs_login_native._close_browser("edge")


class TestReopenBrowser:
    def test_does_not_raise(self):
        xhs_login_native._reopen_browser("edge")


# ── Check logged in ────────────────────────────────────────────────

class TestCheckLoggedIn:
    def test_returns_false_on_exception(self):
        page = MagicMock()
        page.evaluate.side_effect = Exception("page crashed")
        assert xhs_login_native.check_logged_in(page) is False

    def test_returns_true_when_logged_in(self):
        page = MagicMock()
        page.evaluate.return_value = True
        assert xhs_login_native.check_logged_in(page) is True

    def test_returns_false_when_guest(self):
        page = MagicMock()
        page.evaluate.return_value = False
        assert xhs_login_native.check_logged_in(page) is False


# ── extract_cookies validation ─────────────────────────────────────

class TestExtractCookies:
    def test_rejects_unknown_browser(self):
        with pytest.raises(ValueError, match="不支持的浏览器"):
            xhs_login_native.extract_cookies("firefox")

    def test_rejects_missing_profile(self):
        with patch.object(xhs_login_native, "_profile_dir", return_value=None):
            with pytest.raises(FileNotFoundError, match="未找到"):
                xhs_login_native.extract_cookies("edge")
