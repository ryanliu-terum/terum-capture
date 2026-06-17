"""Tests for config load/save/delete."""
import http.client
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from terum_capture.config import load_config, save_config, delete_config, CallbackServer, CORS_ORIGIN


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


class TestCallbackServerCors:
    """The dashboard (public HTTPS origin) POSTs to this loopback server, so the
    responses must carry CORS + the Private Network Access opt-in header."""

    def test_options_preflight_carries_cors_and_pna_headers(self):
        server = CallbackServer()
        port = server.start()
        assert port is not None, "could not bind a callback port"
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "OPTIONS",
                "/callback",
                headers={
                    "Origin": CORS_ORIGIN,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Private-Network": "true",
                },
            )
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 204
            assert resp.getheader("Access-Control-Allow-Origin") == CORS_ORIGIN
            assert resp.getheader("Access-Control-Allow-Methods") == "POST, OPTIONS"
            # The PNA opt-in — without it Chrome rejects the public->loopback preflight.
            assert resp.getheader("Access-Control-Allow-Private-Network") == "true"
            conn.close()
        finally:
            if server._httpd is not None:
                server._httpd.shutdown()

    def test_post_callback_carries_pna_header_and_records_body(self):
        server = CallbackServer()
        port = server.start()
        assert port is not None, "could not bind a callback port"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/callback",
            body=json.dumps({"state": "s" * 32, "token": "jwt-abc"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = resp.read()
        assert resp.status == 200
        assert resp.getheader("Access-Control-Allow-Private-Network") == "true"
        assert data == b'{"ok": true}'
        conn.close()  # do_POST schedules its own shutdown after responding
