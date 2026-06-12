"""测试 cookie 元数据辅助函数和 Playwright 注入。"""
import sys
from pathlib import Path

# 让 scripts/ 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from xhs_login import _build_cookie_result, _build_sqlite_cookie_meta


class TestBuildCookieResult:
    def test_basic(self):
        raw = [
            {"name": "a1", "value": "v1", "domain": ".xiaohongshu.com",
             "path": "/", "secure": True, "httpOnly": False},
            {"name": "web_session", "value": "ws", "domain": ".xiaohongshu.com",
             "path": "/", "secure": True, "httpOnly": True},
        ]
        cookies, meta = _build_cookie_result(raw)
        assert cookies == {"a1": "v1", "web_session": "ws"}
        assert meta["a1"]["domain"] == ".xiaohongshu.com"
        assert meta["a1"]["secure"] is True
        assert meta["web_session"]["httpOnly"] is True

    def test_domain_filter(self):
        raw = [
            {"name": "a1", "value": "v1", "domain": ".xiaohongshu.com"},
            {"name": "other", "value": "x", "domain": ".example.com"},
        ]
        cookies, meta = _build_cookie_result(raw, domain_filter="xiaohongshu")
        assert "a1" in cookies
        assert "other" not in cookies

    def test_empty_input(self):
        cookies, meta = _build_cookie_result([])
        assert cookies == {}
        assert meta == {}

    def test_no_metadata_keys(self):
        raw = [{"name": "a1", "value": "v1"}]
        cookies, meta = _build_cookie_result(raw, domain_filter="")
        assert cookies == {"a1": "v1"}
        assert meta == {}

    def test_duplicate_name_last_wins(self):
        raw = [
            {"name": "a1", "value": "first", "domain": ".xiaohongshu.com"},
            {"name": "a1", "value": "second", "domain": ".xhscdn.com"},
        ]
        cookies, meta = _build_cookie_result(raw, domain_filter="")
        assert cookies["a1"] == "second"
        assert meta["a1"]["domain"] == ".xhscdn.com"


class TestBuildSqliteCookieMeta:
    def test_basic(self):
        meta = _build_sqlite_cookie_meta(".xiaohongshu.com", "/", 1, 1, 1, 9999999999)
        assert meta["domain"] == ".xiaohongshu.com"
        assert meta["path"] == "/"
        assert meta["secure"] is True
        assert meta["httpOnly"] is True
        assert meta["sameSite"] == "Lax"
        assert meta["expires"] == 9999999999.0

    def test_not_secure_not_httponly(self):
        meta = _build_sqlite_cookie_meta(".xhscdn.com", "/api", 0, 0, None, 0)
        assert meta["domain"] == ".xhscdn.com"
        assert "secure" not in meta
        assert "httpOnly" not in meta
        assert "expires" not in meta

    def test_samesite_string(self):
        meta = _build_sqlite_cookie_meta(".xhs.com", "/", 0, 0, "Strict", None)
        assert meta["sameSite"] == "Strict"

    def test_samesite_int_strict(self):
        meta = _build_sqlite_cookie_meta(".xhs.com", "/", 0, 0, 2, None)
        assert meta["sameSite"] == "Strict"
