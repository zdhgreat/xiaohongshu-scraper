"""测试 xhs_image 模块：配置、Mermaid 校验、本地分析、图片分析 DB 集成。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import xhs_image
import xhs_storage


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_default_config(self):
        cfg = xhs_image.DEFAULT_CONFIG
        assert cfg["image_mode"] == "auto"
        assert cfg["image_vision_backend"] == "api"
        assert cfg["image_mermaid"] is True

    def test_load_config_default(self, tmp_path, monkeypatch):
        """无配置文件时返回默认值。"""
        monkeypatch.setattr(xhs_image, "CONFIG_PATH", tmp_path / "image_config.json")
        cfg = xhs_image.load_config()
        assert cfg["image_mode"] == "auto"

    def test_save_and_load_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(xhs_image, "DATA", tmp_path)
        monkeypatch.setattr(xhs_image, "CONFIG_PATH", tmp_path / "image_config.json")
        cfg = {"image_mode": "vision", "api_key": "test-key"}
        xhs_image.save_config(cfg)
        loaded = xhs_image.load_config()
        assert loaded["image_mode"] == "vision"
        assert loaded["api_key"] == "test-key"
        # 默认值也合并了
        assert "image_mermaid" in loaded


# ---------------------------------------------------------------------------
# Mermaid validation
# ---------------------------------------------------------------------------

class TestMermaidValidation:
    def test_valid_graph_lr(self):
        code = "graph LR\n    A[成都] -->|飞机| B[九寨沟]"
        assert xhs_image._validate_mermaid(code) is True

    def test_valid_flowchart_td(self):
        code = "flowchart TD\n    A[开始] --> B[结束]"
        assert xhs_image._validate_mermaid(code) is True

    def test_empty(self):
        assert xhs_image._validate_mermaid("") is False
        assert xhs_image._validate_mermaid("  ") is False

    def test_invalid_prefix(self):
        assert xhs_image._validate_mermaid("digraph { A -> B }") is False
        assert xhs_image._validate_mermaid("some random text") is False

    def test_xss_rejected(self):
        code = "graph LR\n    A[<script>alert(1)</script>] --> B"
        assert xhs_image._validate_mermaid(code) is False


# ---------------------------------------------------------------------------
# Local analysis
# ---------------------------------------------------------------------------

class TestAnalyzeLocal:
    def test_empty_ocr(self):
        result = xhs_image._analyze_local("", "标题", "描述")
        assert "未识别到文字" in result

    def test_with_ocr_text(self):
        ocr = "成都到九寨沟 飞机1小时 门票169元 住宿280元"
        result = xhs_image._analyze_local(ocr, "旅游攻略", "三天两夜行程")
        assert "成都到九寨沟" in result
        assert len(result) > 50

    def test_with_title_overlap(self):
        ocr = "露营装备推荐 帐篷 睡袋"
        result = xhs_image._analyze_local(ocr, "露营装备", "")
        assert "露营" in result


# ---------------------------------------------------------------------------
# Image analysis DB integration
# ---------------------------------------------------------------------------

class TestImageAnalysisDB:
    def test_update_image_analysis(self, db_conn):
        """update_image_analysis 应写入三个字段。"""
        # 先插入一条笔记
        note = {"note_id": "img_note_1", "title": "测试图文", "type": "note"}
        xhs_storage.upsert_note(db_conn, note)

        xhs_storage.update_image_analysis(
            db_conn, "img_note_1",
            ocr_text="图片中的文字",
            summary="AI 描述内容",
            mermaid="graph LR\n    A --> B",
        )

        row = xhs_storage.get_note(db_conn, "img_note_1")
        assert row["image_ocr_text"] == "图片中的文字"
        assert row["image_summary"] == "AI 描述内容"
        assert row["image_mermaid"] == "graph LR\n    A --> B"

    def test_update_preserves_existing(self, db_conn):
        """更新图片分析不应影响已有字段。"""
        note = {"note_id": "img_note_2", "title": "测试", "type": "note",
                "video_summary": "视频摘要"}
        xhs_storage.upsert_note(db_conn, note)

        xhs_storage.update_image_analysis(
            db_conn, "img_note_2",
            ocr_text="OCR",
        )

        row = xhs_storage.get_note(db_conn, "img_note_2")
        assert row["image_ocr_text"] == "OCR"
        assert row["video_summary"] == "视频摘要"

    def test_schema_migration_has_image_columns(self, db_conn):
        """新数据库应包含三个图片分析列。"""
        cols = {r[1] for r in db_conn.execute("PRAGMA table_info(notes)").fetchall()}
        assert "image_ocr_text" in cols
        assert "image_summary" in cols
        assert "image_mermaid" in cols


# ---------------------------------------------------------------------------
# Markdown rendering with image analysis
# ---------------------------------------------------------------------------

class TestImageMarkdown:
    def test_render_with_image_analysis(self, db_conn):
        """MD 渲染应包含图片分析段落。"""
        note = {"note_id": "md_img_1", "title": "旅游攻略", "type": "note"}
        xhs_storage.upsert_note(db_conn, note)
        xhs_storage.update_image_analysis(
            db_conn, "md_img_1",
            ocr_text="Day1 成都→九寨沟",
            summary="这是一篇旅游攻略",
            mermaid="graph LR\n    A[成都] --> B[九寨沟]",
        )

        md = xhs_storage.render_markdown(db_conn, "md_img_1")
        assert "### 图片分析" in md
        assert "#### AI 描述" in md
        assert "这是一篇旅游攻略" in md
        assert "#### 图片文字" in md
        assert "Day1 成都→九寨沟" in md
        assert "#### 路线图 / 流程图" in md
        assert "```mermaid" in md
        assert "graph LR" in md

    def test_render_without_image_analysis(self, db_conn):
        """无图片分析数据时 MD 不应包含图片分析段落。"""
        note = {"note_id": "md_img_2", "title": "普通笔记", "type": "note"}
        xhs_storage.upsert_note(db_conn, note)

        md = xhs_storage.render_markdown(db_conn, "md_img_2")
        assert "### 图片分析" not in md


# ---------------------------------------------------------------------------
# CSV with image columns
# ---------------------------------------------------------------------------

class TestImageCSV:
    def test_csv_has_image_headers(self, db_conn, tmp_path):
        """CSV 应包含 26 列，含图片分析列。"""
        note = {"note_id": "csv_img_1", "title": "CSV测试", "type": "note"}
        xhs_storage.upsert_note(db_conn, note)
        xhs_storage.update_image_analysis(
            db_conn, "csv_img_1",
            ocr_text="OCR text",
            summary="Summary",
        )

        csv_path = tmp_path / "test.csv"
        xhs_storage.write_csv(db_conn, path=csv_path)

        content = csv_path.read_text(encoding="utf-8-sig")
        lines = content.strip().split("\n")
        headers = lines[0].split(",")
        assert len(xhs_storage.CSV_HEADERS) == 26
        assert "图片OCR文字" in xhs_storage.CSV_HEADERS
        assert "图片分析摘要" in xhs_storage.CSV_HEADERS
        assert "图片Mermaid图" in xhs_storage.CSV_HEADERS
