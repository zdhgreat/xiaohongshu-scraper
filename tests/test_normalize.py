"""测试 xhs_api 模块的 normalize 和辅助函数。"""
from xhs_api import _normalize_note, _normalize_user, _normalize_comment, _to_int, _ts_to_str


class TestToInt:
    def test_none(self):
        assert _to_int(None) == 0

    def test_int(self):
        assert _to_int(42) == 42

    def test_float(self):
        assert _to_int(3.7) == 3

    def test_string_number(self):
        assert _to_int("123") == 123

    def test_chinese_wan(self):
        assert _to_int("3.2万") == 32000

    def test_chinese_wan_integer(self):
        assert _to_int("1万") == 10000

    def test_comma_separated(self):
        assert _to_int("1,234") == 1234

    def test_comma_wan(self):
        assert _to_int("12,345") == 12345

    def test_empty_string(self):
        assert _to_int("") == 0

    def test_whitespace(self):
        assert _to_int("  ") == 0

    def test_invalid_string(self):
        assert _to_int("abc") == 0


class TestTsToStr:
    def test_none(self):
        assert _ts_to_str(None) == ""

    def test_zero(self):
        assert _ts_to_str(0) == ""

    def test_seconds_timestamp(self):
        result = _ts_to_str(1700000000)
        assert "2023" in result
        assert "-" in result

    def test_milliseconds_timestamp(self):
        result = _ts_to_str(1700000000000)
        assert "2023" in result

    def test_invalid_string(self):
        assert _ts_to_str("invalid") == "invalid"


class TestNormalizeNote:
    def test_search_item(self, sample_note_item):
        note = _normalize_note(sample_note_item)
        assert note["note_id"] == "note_abc123"
        assert note["user_id"] == "user_001"
        assert note["title"] == "测试笔记标题"
        assert note["description"] == "这是笔记描述内容"
        assert note["type"] == "note"
        assert note["liked_count"] == 12000  # "1.2万"
        assert note["collected_count"] == 3456  # "3,456"
        assert note["comment_count"] == 789
        assert note["ip_location"] == "上海"
        assert note["topics"] == ["美食", "探店"]
        assert note["xsec_token"] == "token_abc"
        assert note["xsec_source"] == "pc_search"

    def test_empty_item(self):
        note = _normalize_note({})
        assert note["note_id"] is None or note["note_id"] == ""
        assert note["liked_count"] == 0
        assert note["topics"] == []

    def test_feed_item(self):
        """Feed API 返回的数据格式（没有 id 顶层，只有 note_card）。"""
        item = {
            "id": "note_feed_001",
            "model_type": "note",
            "note_card": {
                "note_id": "note_feed_001",
                "title": "Feed 笔记",
                "desc": "Feed 描述",
                "type": "video",
                "user": {"user_id": "user_feed"},
                "interact_info": {},
                "video": {"duration": 120000},
            },
        }
        note = _normalize_note(item)
        assert note["note_id"] == "note_feed_001"
        assert note["type"] == "video"
        assert note["video_duration"] == 120  # 120000ms -> 120s


class TestNormalizeUser:
    def test_basic(self, sample_user_info):
        user = _normalize_user(sample_user_info)
        assert user["user_id"] == "user_001"
        assert user["nickname"] == "测试用户"
        assert user["fans_count"] == 56000  # "5.6万"
        assert user["follow_count"] == 123
        assert user["notes_count"] == 456
        assert user["location"] == "北京"

    def test_alternate_keys(self):
        """测试备用键名。"""
        info = {
            "userid": "user_alt",
            "nick_name": "备用昵称",
            "imageb": "https://img.jpg",
            "fans": 100,
        }
        user = _normalize_user(info)
        assert user["user_id"] == "user_alt"
        assert user["nickname"] == "备用昵称"
        assert user["avatar"] == "https://img.jpg"
        assert user["fans_count"] == 100


class TestNormalizeComment:
    def test_basic(self, sample_comment):
        c = _normalize_comment(sample_comment, "note_abc123")
        assert c["comment_id"] == "comment_001"
        assert c["note_id"] == "note_abc123"
        assert c["parent_id"] == ""
        assert c["user_id"] == "user_002"
        assert c["content"] == "这是评论内容"
        assert c["like_count"] == 42
        assert c["ip_location"] == "上海"
        assert c["pictures"] == ["https://example.com/pic1.jpg"]
        assert c["target_comment_id"] == "comment_000"

    def test_with_parent(self, sample_comment):
        c = _normalize_comment(sample_comment, "note_abc123", parent_id="comment_parent")
        assert c["parent_id"] == "comment_parent"

    def test_no_pictures(self):
        c = _normalize_comment({"id": "c1", "user_info": {}}, "n1")
        assert c["pictures"] == []
