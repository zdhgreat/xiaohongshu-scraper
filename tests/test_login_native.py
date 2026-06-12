"""Tests for xhs_login_native — cross-platform cookie extraction."""

import os
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

import xhs_login_native


# ── Profile directory ──────────────────────────────────────────────

class TestProfileDir:
    def test_returns_none_for_unknown_browser(self):
        assert xhs_login_native._profile_dir("safari") is None

    def test_returns_string_for_edge_or_none(self):
        result = xhs_login_native._profile_dir("edge")
        assert result is None or isinstance(result, str)

    def test_returns_string_for_chrome_or_none(self):
        result = xhs_login_native._profile_dir("chrome")
        assert result is None or isinstance(result, str)

    def test_returns_string_for_firefox_or_none(self):
        result = xhs_login_native._profile_dir("firefox")
        assert result is None or isinstance(result, str)

    def test_returns_string_for_brave_or_none(self):
        result = xhs_login_native._profile_dir("brave")
        assert result is None or isinstance(result, str)


# ── Process name ───────────────────────────────────────────────────

class TestProcessName:
    def test_edge(self):
        name = xhs_login_native._process_name("edge")
        assert isinstance(name, str) and len(name) > 0

    def test_chrome(self):
        name = xhs_login_native._process_name("chrome")
        assert isinstance(name, str) and len(name) > 0

    def test_firefox(self):
        name = xhs_login_native._process_name("firefox")
        assert isinstance(name, str) and len(name) > 0

    def test_brave(self):
        name = xhs_login_native._process_name("brave")
        assert isinstance(name, str) and len(name) > 0


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
            xhs_login_native.extract_cookies("safari")

    def test_rejects_missing_profile(self):
        with patch.object(xhs_login_native, "_profile_dir", return_value=None):
            with pytest.raises(FileNotFoundError, match="未找到"):
                xhs_login_native.extract_cookies("edge")


# ── Firefox profile parsing ────────────────────────────────────────

class TestFirefoxDefaultProfile:
    def test_returns_none_when_no_firefox(self):
        with patch.object(xhs_login_native, "_profile_dir", return_value=None):
            assert xhs_login_native._firefox_default_profile() is None

    def test_returns_none_when_no_profiles_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(xhs_login_native, "_profile_dir", return_value=tmp):
                assert xhs_login_native._firefox_default_profile() is None

    def test_parses_profiles_ini_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "abc123.default-release")
            os.makedirs(profile_dir)
            ini_path = os.path.join(tmp, "profiles.ini")
            with open(ini_path, "w", encoding="utf-8") as f:
                f.write("[Profile0]\nName=default\nIsRelative=1\n"
                        "Path=abc123.default-release\nDefault=1\n")
            with patch.object(xhs_login_native, "_profile_dir", return_value=tmp):
                result = xhs_login_native._firefox_default_profile()
                assert result == profile_dir

    def test_falls_back_to_default_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "xyz789.default-release")
            os.makedirs(profile_dir)
            ini_path = os.path.join(tmp, "profiles.ini")
            with open(ini_path, "w", encoding="utf-8") as f:
                f.write("[Profile0]\nName=user\nIsRelative=1\n"
                        "Path=xyz789.default-release\n")
            with patch.object(xhs_login_native, "_profile_dir", return_value=tmp):
                result = xhs_login_native._firefox_default_profile()
                assert result == profile_dir


# ── Firefox cookie extraction ──────────────────────────────────────

class TestExtractFirefoxCookies:
    def test_reads_cookies_from_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create a mock cookies.sqlite
            db = os.path.join(tmp, "cookies.sqlite")
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE moz_cookies "
                "(id INTEGER PRIMARY KEY, name TEXT, value TEXT, "
                "host TEXT, path TEXT, expiry INTEGER, "
                "isSecure INTEGER DEFAULT 0, isHttpOnly INTEGER DEFAULT 0, "
                "sameSite INTEGER DEFAULT 1)"
            )
            conn.execute(
                "INSERT INTO moz_cookies (name, value, host, path, expiry, isSecure, isHttpOnly, sameSite) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("web_session", "ws123", ".xiaohongshu.com", "/", 9999999999, 1, 1, 1)
            )
            conn.execute(
                "INSERT INTO moz_cookies (name, value, host, path, expiry, isSecure, isHttpOnly, sameSite) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("a1", "a1val", ".xiaohongshu.com", "/", 9999999999, 1, 0, 0)
            )
            conn.commit()
            conn.close()

            with patch.object(xhs_login_native, "_firefox_default_profile",
                              return_value=tmp):
                cookies, cookie_meta = xhs_login_native._extract_firefox_cookies()
                assert cookies["web_session"] == "ws123"
                assert cookies["a1"] == "a1val"
                assert cookie_meta["web_session"]["domain"] == ".xiaohongshu.com"
                assert cookie_meta["web_session"]["secure"] is True
                assert cookie_meta["web_session"]["httpOnly"] is True

    def test_raises_when_no_profile(self):
        with patch.object(xhs_login_native, "_firefox_default_profile",
                          return_value=None):
            with pytest.raises(FileNotFoundError, match="未找到 Firefox"):
                xhs_login_native._extract_firefox_cookies()

    def test_raises_when_missing_cookies(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "cookies.sqlite")
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE moz_cookies "
                "(id INTEGER PRIMARY KEY, name TEXT, value TEXT, "
                "host TEXT, path TEXT, expiry INTEGER, "
                "isSecure INTEGER DEFAULT 0, isHttpOnly INTEGER DEFAULT 0, "
                "sameSite INTEGER DEFAULT 1)"
            )
            # Only a1, missing web_session
            conn.execute(
                "INSERT INTO moz_cookies (name, value, host, path, expiry, isSecure, isHttpOnly, sameSite) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("a1", "val", ".xiaohongshu.com", "/", 9999999999, 0, 0, 1)
            )
            conn.commit()
            conn.close()

            with patch.object(xhs_login_native, "_firefox_default_profile",
                              return_value=tmp):
                with pytest.raises(ValueError, match="cookie 不完整"):
                    xhs_login_native._extract_firefox_cookies()
