"""测试 xhs_storage 模块。"""
import json

from xhs_storage import upsert_note, upsert_user, upsert_comment, get_note, save_search_page


class TestUpsertNote:
    def test_insert(self, db_conn, sample_note_item):
        from xhs_api import _normalize_note
        note = _normalize_note(sample_note_item)
        upsert_note(db_conn, note)
        row = get_note(db_conn, note["note_id"])
        assert row is not None
        assert row["note_id"] == "note_abc123"
        assert row["title"] == "测试笔记标题"
        assert row["liked_count"] == 12000

    def test_update_preserves_xsec_token(self, db_conn):
        """xsec_token 在更新时应保留非空旧值。"""
        note_v1 = {
            "note_id": "n1", "user_id": "u1", "title": "V1",
            "xsec_token": "token_v1", "xsec_source": "pc_search",
        }
        upsert_note(db_conn, note_v1)

        note_v2 = {
            "note_id": "n1", "user_id": "u1", "title": "V2",
            "xsec_token": "", "xsec_source": "",
        }
        upsert_note(db_conn, note_v2)

        row = get_note(db_conn, "n1")
        assert row["title"] == "V2"  # 标题更新
        assert row["xsec_token"] == "token_v1"  # token 保留

    def test_update_overwrites_with_nonempty_token(self, db_conn):
        """新 token 非空时应覆盖旧 token。"""
        note_v1 = {"note_id": "n2", "xsec_token": "old_token"}
        upsert_note(db_conn, note_v1)

        note_v2 = {"note_id": "n2", "xsec_token": "new_token"}
        upsert_note(db_conn, note_v2)

        row = get_note(db_conn, "n2")
        assert row["xsec_token"] == "new_token"


class TestUpsertUser:
    def test_insert(self, db_conn, sample_user_info):
        from xhs_api import _normalize_user
        user = _normalize_user(sample_user_info)
        upsert_user(db_conn, user)
        row = db_conn.execute("SELECT * FROM users WHERE user_id = ?", ("user_001",)).fetchone()
        assert row is not None
        assert row["nickname"] == "测试用户"
        assert row["fans_count"] == 56000


class TestUpsertComment:
    def test_insert(self, db_conn, sample_comment):
        from xhs_api import _normalize_comment
        c = _normalize_comment(sample_comment, "note_abc123")
        upsert_comment(db_conn, c)
        row = db_conn.execute("SELECT * FROM comments WHERE comment_id = ?", ("comment_001",)).fetchone()
        assert row is not None
        assert row["content"] == "这是评论内容"
        assert row["like_count"] == 42


class TestSearchCache:
    def test_save_and_read(self, db_conn):
        save_search_page(db_conn, "测试关键词", 1, ["n1", "n2", "n3"])
        row = db_conn.execute(
            "SELECT * FROM search_cache WHERE keyword = ? AND page = ?",
            ("测试关键词", 1),
        ).fetchone()
        assert row is not None
        ids = json.loads(row["note_ids_json"])
        assert ids == ["n1", "n2", "n3"]
