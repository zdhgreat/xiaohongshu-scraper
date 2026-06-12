"""测试 xhs_image 模块：配置、图片分析 DB 集成。"""
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
        assert isinstance(cfg, dict)

    def test_load_config_default(self, tmp_path, monkeypatch):
        """无配置文件时返回默认值。"""
        monkeypatch.setattr(xhs_image, "CONFIG_PATH", tmp_path / "image_config.json")
        cfg = xhs_image.load_config()
        assert isinstance(cfg, dict)

    def test_save_and_load_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(xhs_image, "DATA", tmp_path)
        monkeypatch.setattr(xhs_image, "CONFIG_PATH", tmp_path / "image_config.json")
        cfg = {"enabled": True}
        xhs_image.save_config(cfg)
        loaded = xhs_image.load_config()
        assert loaded["enabled"] is True


# ---------------------------------------------------------------------------
# Image analysis DB integration
# ---------------------------------------------------------------------------

class TestImageAnalysisDB:
    def test_update_image_analysis(self, db_conn):
        """update_image_analysis 应写入 OCR 字段。"""
        note = {"note_id": "img_note_1", "title": "测试图文", "type": "note"}
        xhs_storage.upsert_note(db_conn, note)

        xhs_storage.update_image_analysis(
            db_conn, "img_note_1",
            ocr_text="图片中的文字",
            summary="",
            mermaid="",
        )

        row = xhs_storage.get_note(db_conn, "img_note_1")
        assert row["image_ocr_text"] == "图片中的文字"
        assert row["image_summary"] == ""
        assert row["image_mermaid"] == ""

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
    def test_render_with_ocr_only(self, db_conn, tmp_path, monkeypatch):
        """仅有 OCR 时 images.md 应包含图片文字段落。"""
        note = {"note_id": "md_img_1", "title": "旅游攻略", "type": "note"}
        xhs_storage.upsert_note(db_conn, note)
        xhs_storage.update_image_analysis(
            db_conn, "md_img_1",
            ocr_text="Day1 成都→九寨沟",
            summary="",
            mermaid="",
        )

        monkeypatch.setattr(xhs_storage, "OUTPUT_DIR", tmp_path)
        files = xhs_storage.write_markdown_files(db_conn, "md_img_1")
        images_md = next((f for f in files if f.name == "images.md"), None)
        assert images_md is not None, f"images.md not found in {files}"
        content = images_md.read_text(encoding="utf-8")
        assert "图片文字" in content
        assert "Day1 成都→九寨沟" in content

    def test_render_without_image_analysis(self, db_conn, tmp_path, monkeypatch):
        """无图片分析数据时不应生成 images.md。"""
        note = {"note_id": "md_img_2", "title": "普通笔记", "type": "note"}
        xhs_storage.upsert_note(db_conn, note)

        monkeypatch.setattr(xhs_storage, "OUTPUT_DIR", tmp_path)
        files = xhs_storage.write_markdown_files(db_conn, "md_img_2")
        images_md = next((f for f in files if f.name == "images.md"), None)
        assert images_md is None, "images.md should not be generated without image analysis"


# ---------------------------------------------------------------------------
# CSV with image columns
# ---------------------------------------------------------------------------

class TestImageCSV:
    def test_csv_has_correct_headers(self, db_conn, tmp_path):
        """CSV 应包含精简后的 14 列。"""
        note = {"note_id": "csv_img_1", "title": "CSV测试", "type": "note"}
        xhs_storage.upsert_note(db_conn, note)
        xhs_storage.update_image_analysis(
            db_conn, "csv_img_1",
            ocr_text="OCR text",
        )

        csv_path = tmp_path / "test.csv"
        files = xhs_storage.write_csv(db_conn, path=csv_path)

        content = csv_path.read_text(encoding="utf-8-sig")
        lines = content.strip().split("\n")
        headers = lines[0].split(",")
        assert len(xhs_storage.CSV_HEADERS) == 14
        assert "标题" in xhs_storage.CSV_HEADERS
        assert "正文摘要" in xhs_storage.CSV_HEADERS
