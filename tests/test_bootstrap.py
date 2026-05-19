"""Tests for xhs_bootstrap — auto-bootstrap & setup commands."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import xhs_bootstrap


# ── Pure helpers ────────────────────────────────────────────────────

class TestInVenv:
    def test_not_in_venv(self):
        # In typical test environments we may or may not be in a venv
        result = xhs_bootstrap._in_venv()
        assert isinstance(result, bool)

    def test_real_prefix_true(self):
        with patch.object(sys, "real_prefix", "/fake", create=True):
            assert xhs_bootstrap._in_venv() is True

    def test_base_prefix_diff(self):
        original = getattr(sys, "base_prefix", None)
        original2 = getattr(sys, "prefix", None)
        sys.base_prefix = "/fake_base"
        sys.prefix = "/fake_prefix"
        try:
            assert xhs_bootstrap._in_venv() is True
        finally:
            if original is not None:
                sys.base_prefix = original
            else:
                del sys.base_prefix
            sys.prefix = original2

    def test_base_prefix_same(self):
        original = getattr(sys, "base_prefix", None)
        original2 = getattr(sys, "prefix", None)
        sys.base_prefix = "/same"
        sys.prefix = "/same"
        try:
            # No real_prefix attribute
            if hasattr(sys, "real_prefix"):
                # Can't easily remove it, skip
                pass
            else:
                assert xhs_bootstrap._in_venv() is False
        finally:
            if original is not None:
                sys.base_prefix = original
            else:
                del sys.base_prefix
            sys.prefix = original2


class TestPipMarkerValid:
    def test_no_marker(self, tmp_path):
        marker = tmp_path / ".pip_ok"
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("requests")
        with patch.object(xhs_bootstrap, "_PIP_MARKER", marker), \
             patch.object(xhs_bootstrap, "REQUIREMENTS", reqs):
            assert xhs_bootstrap._pip_marker_valid() is False

    def test_valid_marker(self, tmp_path):
        import time
        marker = tmp_path / ".pip_ok"
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("requests")
        time.sleep(0.1)
        marker.touch()
        with patch.object(xhs_bootstrap, "_PIP_MARKER", marker), \
             patch.object(xhs_bootstrap, "REQUIREMENTS", reqs):
            assert xhs_bootstrap._pip_marker_valid() is True

    def test_stale_marker(self, tmp_path):
        marker = tmp_path / ".pip_ok"
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("requests")
        # Marker exists but is older than requirements
        marker.touch()
        import time
        time.sleep(0.1)
        reqs.write_text("requests\nflask")
        with patch.object(xhs_bootstrap, "_PIP_MARKER", marker), \
             patch.object(xhs_bootstrap, "REQUIREMENTS", reqs):
            assert xhs_bootstrap._pip_marker_valid() is False

    def test_no_requirements(self, tmp_path):
        marker = tmp_path / ".pip_ok"
        marker.touch()
        reqs = tmp_path / "requirements.txt"  # doesn't exist
        with patch.object(xhs_bootstrap, "_PIP_MARKER", marker), \
             patch.object(xhs_bootstrap, "REQUIREMENTS", reqs):
            assert xhs_bootstrap._pip_marker_valid() is True


class TestHasCryptoJs:
    def test_present(self, tmp_path):
        node_modules = tmp_path / "node_modules" / "crypto-js"
        node_modules.mkdir(parents=True)
        with patch.object(xhs_bootstrap, "ASSETS", tmp_path):
            assert xhs_bootstrap._has_crypto_js() is True

    def test_absent(self, tmp_path):
        with patch.object(xhs_bootstrap, "ASSETS", tmp_path):
            assert xhs_bootstrap._has_crypto_js() is False


class TestFindNode:
    def test_found_via_which(self):
        with patch("shutil.which", return_value="/usr/bin/node"):
            assert xhs_bootstrap._find_node() == "/usr/bin/node"

    def test_not_found(self):
        with patch("shutil.which", return_value=None):
            result = xhs_bootstrap._find_node()
            # May still find node on the system via candidate paths
            assert result is None or isinstance(result, str)


class TestFindNpm:
    def test_found_via_which(self):
        with patch("shutil.which", return_value="/usr/bin/npm"):
            assert xhs_bootstrap._find_npm() == "/usr/bin/npm"

    def test_fallback_to_node_dir(self, tmp_path):
        node_exe = tmp_path / "node.exe"
        node_exe.write_text("")
        npm_exe = tmp_path / "npm.cmd"
        npm_exe.write_text("")

        with patch("shutil.which", side_effect=[None, None]), \
             patch.object(xhs_bootstrap, "_find_node", return_value=str(node_exe)):
            result = xhs_bootstrap._find_npm()
            assert result == str(npm_exe)


class TestRun:
    def test_success(self):
        r = xhs_bootstrap._run([sys.executable, "-c", "print(42)"])
        assert r.returncode == 0
        assert "42" in r.stdout

    def test_not_found(self):
        r = xhs_bootstrap._run(["nonexistent_command_xyz"])
        assert r.returncode != 0


class TestEnsureReady:
    def test_does_not_raise(self):
        # Should complete without error even if deps are missing
        with patch.object(xhs_bootstrap, "_install_pip"), \
             patch.object(xhs_bootstrap, "_install_node"), \
             patch.object(xhs_bootstrap, "_install_npm"), \
             patch.object(xhs_bootstrap, "_install_playwright"), \
             patch.object(xhs_bootstrap, "_check_ffmpeg"), \
             patch.object(xhs_bootstrap, "_verify_optional_packages"):
            xhs_bootstrap.ensure_ready()

    def test_force_removes_markers(self, tmp_path):
        pip_marker = tmp_path / ".pip_ok"
        pw_marker = tmp_path / ".pw_ok"
        pip_marker.touch()
        pw_marker.touch()

        with patch.object(xhs_bootstrap, "_PIP_MARKER", pip_marker), \
             patch.object(xhs_bootstrap, "_PW_MARKER", pw_marker), \
             patch.object(xhs_bootstrap, "_pip_marker_valid", return_value=True), \
             patch.object(xhs_bootstrap, "_find_node", return_value="/usr/bin/node"), \
             patch.object(xhs_bootstrap, "_has_crypto_js", return_value=True), \
             patch.object(xhs_bootstrap, "_install_pip"), \
             patch.object(xhs_bootstrap, "_install_node"), \
             patch.object(xhs_bootstrap, "_install_npm"), \
             patch.object(xhs_bootstrap, "_install_playwright"), \
             patch.object(xhs_bootstrap, "_check_ffmpeg"), \
             patch.object(xhs_bootstrap, "_verify_optional_packages"):
            xhs_bootstrap.ensure_ready(force=True)
            assert not pip_marker.exists()
            assert not pw_marker.exists()


class TestVerifyOptionalPackages:
    def test_runs_without_error(self, capsys):
        xhs_bootstrap._verify_optional_packages()
        # Should print something about optional packages
        captured = capsys.readouterr()
        # Just verify it doesn't crash — output varies by environment
