"""共享测试 fixtures。"""
import sys
from pathlib import Path

import pytest

# 确保 scripts/ 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import xhs_config
import xhs_storage


@pytest.fixture
def db_conn():
    """创建内存 SQLite 数据库并应用 schema。"""
    conn = xhs_storage._create_in_memory()
    yield conn
    conn.close()


@pytest.fixture
def sample_note_item():
    """搜索 API 返回的典型笔记 item。"""
    return {
        "id": "note_abc123",
        "model_type": "note",
        "xsec_token": "token_abc",
        "xsec_source": "pc_search",
        "note_card": {
            "note_id": "note_abc123",
            "title": "测试笔记标题",
            "desc": "这是笔记描述内容",
            "type": "note",
            "user": {
                "user_id": "user_001",
                "nickname": "测试用户",
            },
            "interact_info": {
                "liked_count": "1.2万",
                "collected_count": "3,456",
                "comment_count": 789,
                "share_count": "100",
            },
            "ip_location": "上海",
            "tag_list": [
                {"name": "美食"},
                {"name": "探店"},
            ],
            "time": 1700000000000,  # ms timestamp
            "image_list": [],
            "video": None,
        },
    }


@pytest.fixture
def sample_user_info():
    """用户 API 返回的典型 basic_info。"""
    return {
        "user_id": "user_001",
        "red_id": "user_001",
        "nickname": "测试用户",
        "avatar": "https://example.com/avatar.jpg",
        "desc": "这是用户的个人简介",
        "fans": "5.6万",
        "follows": 123,
        "notes": 456,
        "ip_location": "北京",
    }


@pytest.fixture
def sample_comment():
    """评论 API 返回的典型评论。"""
    return {
        "id": "comment_001",
        "content": "这是评论内容",
        "like_count": "42",
        "ip_location": "上海",
        "create_time": 1700000000,
        "user_info": {
            "user_id": "user_002",
            "nickname": "评论者",
        },
        "pictures": [
            {"url_default": "https://example.com/pic1.jpg"},
        ],
        "target_comment": {
            "id": "comment_000",
        },
        "sub_comments": [],
    }
