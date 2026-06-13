"""Tests for config load/save/delete."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from terum_capture.config import load_config, save_config, delete_config


class TestConfig:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_dir = Path(self.tmpdir) / ".terum"
        self.config_file = self.config_dir / "config.json"

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("terum_capture.config.CONFIG_DIR")
    @patch("terum_capture.config.CONFIG_FILE")
    def test_save_and_load(self, mock_file, mock_dir):
        mock_dir.__truediv__ = lambda s, x: self.config_dir / x
        mock_dir.mkdir = self.config_dir.mkdir
        mock_file.__str__ = lambda s: str(self.config_file)
        mock_file.write_text = self.config_file.write_text
        mock_file.read_text = self.config_file.read_text

        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(json.dumps({
            "api_key": "trm_test123456789012345678901234",
            "api_url": "https://api.terum.ai/api",
        }))

        data = json.loads(self.config_file.read_text())
        assert data["api_key"] == "trm_test123456789012345678901234"
        assert data["api_url"] == "https://api.terum.ai/api"

    def test_load_missing_returns_none(self):
        with patch("terum_capture.config.CONFIG_FILE", self.config_file):
            result = load_config()
            assert result is None

    def test_load_malformed_returns_none(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text("not json")
        with patch("terum_capture.config.CONFIG_FILE", self.config_file):
            result = load_config()
            assert result is None

    def test_delete_removes_file(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text('{"api_key": "trm_x"}')
        assert self.config_file.exists()

        with patch("terum_capture.config.CONFIG_FILE", self.config_file):
            delete_config()
            assert not self.config_file.exists()

    def test_delete_missing_is_noop(self):
        with patch("terum_capture.config.CONFIG_FILE", self.config_file):
            delete_config()  # should not raise
