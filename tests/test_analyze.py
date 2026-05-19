"""Tests for xhs_analyze — sentiment analysis & topic clustering."""

import json

import pytest

import xhs_storage
import xhs_analyze


# ── Sentiment helpers ──────────────────────────────────────────────

class TestSentimentLabel:
    def test_negative(self):
        assert xhs_analyze._sentiment_label(0.1) == "负面"

    def test_negative_boundary(self):
        assert xhs_analyze._sentiment_label(0.39) == "负面"

    def test_neutral(self):
        assert xhs_analyze._sentiment_label(0.5) == "中性"

    def test_neutral_boundary_low(self):
        assert xhs_analyze._sentiment_label(0.4) == "中性"

    def test_neutral_boundary_high(self):
        assert xhs_analyze._sentiment_label(0.7) == "中性"

    def test_positive(self):
        assert xhs_analyze._sentiment_label(0.8) == "正面"

    def test_positive_boundary(self):
        assert xhs_analyze._sentiment_label(0.71) == "正面"


class TestSimpleSentiment:
    def test_empty(self):
        assert xhs_analyze._simple_sentiment("") == 0.5

    def test_positive_words(self):
        score = xhs_analyze._simple_sentiment("这个真的很好看，推荐！")
        assert score > 0.5

    def test_negative_words(self):
        score = xhs_analyze._simple_sentiment("太差了，非常失望，垃圾")
        assert score < 0.5

    def test_mixed(self):
        score = xhs_analyze._simple_sentiment("好看但是太坑了")
        # Positive "好看" + negative "太坑" should net close to 0.5
        assert 0.3 < score < 0.7

    def test_no_keywords(self):
        assert xhs_analyze._simple_sentiment("这个价格还行") == 0.5


class TestScoreSentiment:
    def test_empty(self):
        assert xhs_analyze._score_sentiment("") == 0.5

    def test_whitespace(self):
        assert xhs_analyze._score_sentiment("   ") == 0.5

    def test_returns_float(self):
        score = xhs_analyze._score_sentiment("好看")
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0


# ── Keyword extraction ─────────────────────────────────────────────

class TestExtractKeywords:
    def test_empty(self):
        assert xhs_analyze._extract_keywords("") == []

    def test_whitespace(self):
        assert xhs_analyze._extract_keywords("   ") == []

    def test_chinese_text(self):
        text = "这个美食探店非常好吃，推荐大家去尝试一下"
        kws = xhs_analyze._extract_keywords(text, top_k=3)
        assert isinstance(kws, list)
        # Should extract some 2-4 char Chinese phrases
        for kw in kws:
            assert 2 <= len(kw) <= 4

    def test_top_k(self):
        text = "美食探店好吃推荐好看漂亮实用"
        kws = xhs_analyze._extract_keywords(text, top_k=2)
        assert len(kws) <= 2

    def test_filters_stopwords(self):
        # "的" is a stopword
        text = "我的天啊你的事"
        kws = xhs_analyze._extract_keywords(text, top_k=5)
        for kw in kws:
            assert kw != "的"


# ── Integration: analyze_sentiment with DB ─────────────────────────

class TestAnalyzeSentiment:
    @pytest.fixture(autouse=True)
    def _setup_db(self, db_conn):
        self.conn = db_conn
        # Insert a note + comments
        xhs_storage.upsert_note(db_conn, {
            "note_id": "n1", "title": "测试", "description": "",
            "type": "normal", "user_id": "u1", "nickname": "作者",
            "liked_count": 0, "collected_count": 0, "comment_count": 0,
            "share_count": 0, "xsec_token": "", "topics": [],
            "ip_location": "", "cover_url": "", "ts": 1700000000,
        })
        db_conn.commit()

        # Positive comment
        xhs_storage.upsert_comment(db_conn, {
            "comment_id": "c1", "note_id": "n1", "content": "真的很好看，推荐！",
            "like_count": 10, "user_id": "u2", "nickname": "好评者",
            "avatar": "", "ip_location": "", "create_time": 1700000000,
            "pictures": "[]", "sub_comment_count": 0,
            "parent_id": "", "reply_to": "",
        })
        # Negative comment
        xhs_storage.upsert_comment(db_conn, {
            "comment_id": "c2", "note_id": "n1", "content": "太差了，非常失望",
            "like_count": 5, "user_id": "u3", "nickname": "差评者",
            "avatar": "", "ip_location": "", "create_time": 1700000000,
            "pictures": "[]", "sub_comment_count": 0,
            "parent_id": "", "reply_to": "",
        })
        db_conn.commit()

    def test_by_note_id(self):
        result = xhs_analyze.analyze_sentiment(self.conn, note_id="n1")
        assert result["total"] == 2
        assert "distribution" in result
        assert result["distribution"]["正面"] + result["distribution"]["负面"] + result["distribution"]["中性"] == 2

    def test_no_comments(self):
        result = xhs_analyze.analyze_sentiment(self.conn, note_id="nonexistent")
        assert result["total"] == 0

    def test_all_comments(self):
        result = xhs_analyze.analyze_sentiment(self.conn)
        assert result["total"] == 2


class TestAnalyzeTopics:
    @pytest.fixture(autouse=True)
    def _setup_db(self, db_conn):
        self.conn = db_conn
        # Insert notes with topics
        for i in range(3):
            xhs_storage.upsert_note(db_conn, {
                "note_id": f"n{i}", "title": f"美食探店{i}", "description": "好吃推荐",
                "type": "normal", "user_id": "u1", "nickname": "作者",
                "liked_count": 100 * (i + 1), "collected_count": 50, "comment_count": 10,
                "share_count": 5, "xsec_token": "", "topics": ["美食", "探店"],
                "ip_location": "上海", "cover_url": "", "ts": 1700000000 + i,
            })
        db_conn.commit()

    def test_basic(self):
        result = xhs_analyze.analyze_topics(self.conn)
        assert result["total"] == 3
        assert "tags" in result
        assert "keywords" in result
        assert "types" in result
        assert "engagement" in result

    def test_tags_count(self):
        result = xhs_analyze.analyze_topics(self.conn)
        # "美食" and "探店" each appear 3 times
        tags_dict = dict(result["tags"])
        assert tags_dict.get("美食") == 3
        assert tags_dict.get("探店") == 3

    def test_engagement(self):
        result = xhs_analyze.analyze_topics(self.conn)
        eng = result["engagement"]
        assert eng["total_likes"] == 100 + 200 + 300
        assert eng["total_collects"] == 150
        assert eng["total_comments"] == 30

    def test_ip_locations(self):
        result = xhs_analyze.analyze_topics(self.conn)
        ips = dict(result["ip_locations"])
        assert ips.get("上海") == 3

    def test_keyword_filter(self):
        result = xhs_analyze.analyze_topics(self.conn, keyword="不存在的关键词")
        assert result["total"] == 0

    def test_empty_db(self):
        conn = xhs_storage._create_in_memory()
        result = xhs_analyze.analyze_topics(conn)
        assert result["total"] == 0
        conn.close()


# ── CLI handler ─────────────────────────────────────────────────────

class TestCmdAnalyze:
    def test_topics_text_output(self, db_conn, capsys):
        xhs_storage.upsert_note(db_conn, {
            "note_id": "n1", "title": "测试笔记", "description": "",
            "type": "normal", "user_id": "u1", "nickname": "作者",
            "liked_count": 10, "collected_count": 5, "comment_count": 2,
            "share_count": 1, "xsec_token": "", "topics": '["测试"]',
            "ip_location": "", "cover_url": "", "ts": 1700000000,
        })
        db_conn.commit()

        class Args:
            type = "topics"
            output = "text"
            keyword = None
            user = None
            note = None

        rc = xhs_analyze.cmd_analyze(Args())
        assert rc == 0
        captured = capsys.readouterr()
        assert "话题聚类报告" in captured.out

    def test_sentiment_json_output(self, db_conn, capsys):
        xhs_storage.upsert_note(db_conn, {
            "note_id": "n1", "title": "测试", "description": "",
            "type": "normal", "user_id": "u1", "nickname": "作者",
            "liked_count": 0, "collected_count": 0, "comment_count": 0,
            "share_count": 0, "xsec_token": "", "topics": [],
            "ip_location": "", "cover_url": "", "ts": 1700000000,
        })
        xhs_storage.upsert_comment(db_conn, {
            "comment_id": "c1", "note_id": "n1", "content": "好看",
            "like_count": 0, "user_id": "u2", "nickname": "评论者",
            "avatar": "", "ip_location": "", "create_time": 1700000000,
            "pictures": "[]", "sub_comment_count": 0,
            "parent_id": "", "reply_to": "",
        })
        db_conn.commit()

        class Args:
            type = "sentiment"
            output = "json"
            keyword = None
            user = None
            note = "n1"

        from unittest.mock import patch
        with patch("xhs_storage.connect", return_value=db_conn):
            rc = xhs_analyze.cmd_analyze(Args())
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["total"] == 1
