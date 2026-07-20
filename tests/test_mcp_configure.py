"""Tests for the _configure_mcp helper (MCP install, Tier 3 of SPEC-mcp-install.md)."""
import json
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from terum_capture import commands
from terum_capture.commands import _configure_mcp

API_KEY = "trm_test123456789012345678901234"
API_URL = "https://api.terum.ai/api"


class FakeCompletedProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


class TestConfigureMcpClaudePrimary:
    def test_primary_path_success(self, tmp_path, monkeypatch):
        claude_json = tmp_path / ".claude.json"
        monkeypatch.setattr(commands, "CLAUDE_JSON", claude_json)

        recorded = {}

        def fake_run(argv, **kwargs):
            recorded["argv"] = argv
            recorded["kwargs"] = kwargs
            return FakeCompletedProcess(returncode=0)

        monkeypatch.setattr(commands.shutil, "which", lambda name: "/usr/local/bin/claude")
        monkeypatch.setattr(commands.subprocess, "run", fake_run)

        result = _configure_mcp(API_KEY, API_URL, client="claude")

        assert result == "installed"
        argv = recorded["argv"]
        assert "--scope" in argv
        assert argv[argv.index("--scope") + 1] == "user"
        assert f"{API_URL}/mcp" in argv
        header_idx = argv.index("--header") + 1
        assert API_KEY in argv[header_idx]
        # never touched the fallback file
        assert not claude_json.exists()


class TestConfigureMcpClaudeFallback:
    def test_fallback_write_preserves_existing_keys(self, tmp_path, monkeypatch):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "theme": "dark",
            "mcpServers": {
                "other": {"type": "http", "url": "https://example.com/mcp", "headers": {}},
            },
        }))
        monkeypatch.setattr(commands, "CLAUDE_JSON", claude_json)
        monkeypatch.setattr(commands.shutil, "which", lambda name: None)

        def fail_run(*a, **k):
            raise AssertionError("subprocess.run should not be called when claude is missing")

        monkeypatch.setattr(commands.subprocess, "run", fail_run)

        result = _configure_mcp(API_KEY, API_URL, client="claude")

        assert result == "installed"
        data = json.loads(claude_json.read_text())
        assert data["theme"] == "dark"
        assert data["mcpServers"]["other"]["url"] == "https://example.com/mcp"
        assert data["mcpServers"]["terum"] == {
            "type": "http",
            "url": f"{API_URL}/mcp",
            "headers": {"Authorization": f"Bearer {API_KEY}"},
        }


class TestConfigureMcpIdempotency:
    def test_already_present_short_circuits(self, tmp_path, monkeypatch):
        claude_json = tmp_path / ".claude.json"
        existing = {
            "mcpServers": {
                "terum": {"type": "http", "url": f"{API_URL}/mcp", "headers": {"Authorization": "Bearer trm_old"}},
            },
        }
        claude_json.write_text(json.dumps(existing, indent=2) + "\n")
        before_bytes = claude_json.read_bytes()
        monkeypatch.setattr(commands, "CLAUDE_JSON", claude_json)

        def fail_run(*a, **k):
            raise AssertionError("subprocess.run should not be called when already configured")

        monkeypatch.setattr(commands.subprocess, "run", fail_run)
        # shutil.which should not even matter since the idempotency check happens first,
        # but leave it truthy to prove the short-circuit precedes the primary path.
        monkeypatch.setattr(commands.shutil, "which", lambda name: "/usr/local/bin/claude")

        result = _configure_mcp(API_KEY, API_URL, client="claude")

        assert result == "already"
        assert claude_json.read_bytes() == before_bytes


class TestConfigureMcpCursor:
    def test_cursor_write_shape_and_merge(self, tmp_path, monkeypatch):
        cursor_mcp = tmp_path / ".cursor" / "mcp.json"
        cursor_mcp.parent.mkdir(parents=True)
        cursor_mcp.write_text(json.dumps({
            "mcpServers": {"other": {"url": "https://example.com/mcp", "headers": {}}},
        }))
        monkeypatch.setattr(commands, "CURSOR_MCP", cursor_mcp)

        result = _configure_mcp(API_KEY, API_URL, client="cursor")

        assert result == "installed"
        data = json.loads(cursor_mcp.read_text())
        assert data["mcpServers"]["other"]["url"] == "https://example.com/mcp"
        assert data["mcpServers"]["terum"] == {
            "url": f"{API_URL}/mcp",
            "headers": {"Authorization": f"Bearer {API_KEY}"},
        }
        assert "type" not in data["mcpServers"]["terum"]

    def test_cursor_creates_missing_parent_dir(self, tmp_path, monkeypatch):
        cursor_mcp = tmp_path / "nested" / ".cursor" / "mcp.json"
        monkeypatch.setattr(commands, "CURSOR_MCP", cursor_mcp)

        result = _configure_mcp(API_KEY, API_URL, client="cursor")

        assert result == "installed"
        assert cursor_mcp.exists()


class TestConfigureMcpBadJson:
    def test_bad_json_target_not_overwritten(self, tmp_path, monkeypatch):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text("{ not json")
        before_bytes = claude_json.read_bytes()
        monkeypatch.setattr(commands, "CLAUDE_JSON", claude_json)
        monkeypatch.setattr(commands.shutil, "which", lambda name: None)

        result = _configure_mcp(API_KEY, API_URL, client="claude")

        assert result == "failed"
        assert claude_json.read_bytes() == before_bytes


class TestConfigureMcpUnknownClient:
    def test_unknown_client_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(commands, "CLAUDE_JSON", tmp_path / ".claude.json")
        result = _configure_mcp(API_KEY, API_URL, client="vscode")
        assert result == "failed"


class TestConfigureMcpNullMcpServers:
    """Regression coverage: a config file that parses as valid JSON but has a non-dict
    `mcpServers` value (e.g. {"mcpServers": null}) must never crash `_configure_mcp`.
    Pre-fix, `existing_config.get("mcpServers", {})` returns None (the default only
    applies when the key is ABSENT, not when present-but-null), and `NAME in None`
    raises TypeError. Terum should be treated as absent and installation should proceed.
    """

    def test_claude_null_mcp_servers_does_not_raise_and_installs(self, tmp_path, monkeypatch):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"mcpServers": None}))
        monkeypatch.setattr(commands, "CLAUDE_JSON", claude_json)
        monkeypatch.setattr(commands.shutil, "which", lambda name: None)

        def fail_run(*a, **k):
            raise AssertionError("subprocess.run should not be called when claude is missing")

        monkeypatch.setattr(commands.subprocess, "run", fail_run)

        result = _configure_mcp(API_KEY, API_URL, client="claude")

        assert result == "installed"
        data = json.loads(claude_json.read_text())
        assert data["mcpServers"]["terum"] == {
            "type": "http",
            "url": f"{API_URL}/mcp",
            "headers": {"Authorization": f"Bearer {API_KEY}"},
        }

    def test_claude_non_dict_mcp_servers_via_primary_path_does_not_raise(self, tmp_path, monkeypatch):
        """Same bug, but hit while `claude` CLI IS present, so the idempotency check
        (not the fallback write) is the only code that touches the null value before
        the primary subprocess path runs."""
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"mcpServers": "not-a-dict"}))
        monkeypatch.setattr(commands, "CLAUDE_JSON", claude_json)
        monkeypatch.setattr(commands.shutil, "which", lambda name: "/usr/local/bin/claude")
        monkeypatch.setattr(commands.subprocess, "run", lambda *a, **k: FakeCompletedProcess(returncode=0))

        result = _configure_mcp(API_KEY, API_URL, client="claude")

        assert result == "installed"

    def test_cursor_null_mcp_servers_does_not_raise_and_installs(self, tmp_path, monkeypatch):
        cursor_mcp = tmp_path / ".cursor" / "mcp.json"
        cursor_mcp.parent.mkdir(parents=True)
        cursor_mcp.write_text(json.dumps({"mcpServers": None}))
        monkeypatch.setattr(commands, "CURSOR_MCP", cursor_mcp)

        result = _configure_mcp(API_KEY, API_URL, client="cursor")

        assert result == "installed"
        data = json.loads(cursor_mcp.read_text())
        assert data["mcpServers"]["terum"] == {
            "url": f"{API_URL}/mcp",
            "headers": {"Authorization": f"Bearer {API_KEY}"},
        }


class TestConfigureMcpFilePermissions:
    @pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics differ on win32")
    def test_newly_created_file_gets_mode_600(self, tmp_path, monkeypatch):
        cursor_mcp = tmp_path / ".cursor" / "mcp.json"
        monkeypatch.setattr(commands, "CURSOR_MCP", cursor_mcp)

        result = _configure_mcp(API_KEY, API_URL, client="cursor")

        assert result == "installed"
        mode = stat.S_IMODE(cursor_mcp.stat().st_mode)
        assert mode == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics differ on win32")
    def test_merge_into_existing_file_does_not_force_mode_600(self, tmp_path, monkeypatch):
        cursor_mcp = tmp_path / ".cursor" / "mcp.json"
        cursor_mcp.parent.mkdir(parents=True)
        cursor_mcp.write_text(json.dumps({
            "mcpServers": {"other": {"url": "https://example.com/mcp", "headers": {}}},
        }))
        cursor_mcp.chmod(0o644)
        monkeypatch.setattr(commands, "CURSOR_MCP", cursor_mcp)

        result = _configure_mcp(API_KEY, API_URL, client="cursor")

        assert result == "installed"
        mode = stat.S_IMODE(cursor_mcp.stat().st_mode)
        assert mode == 0o644


class TestConfigureMcpAdversarial:
    def test_trailing_slash_api_url_produces_sane_mcp_url(self, tmp_path, monkeypatch):
        """A naive f-string join (f"{api_url}/mcp") on an api_url with a trailing slash
        would produce a double-slash URL (".../api//mcp"). The header string must also be
        exactly 'Authorization: Bearer <key>' with a single space, not e.g. leaked into the
        URL or duplicated.
        """
        claude_json = tmp_path / ".claude.json"
        monkeypatch.setattr(commands, "CLAUDE_JSON", claude_json)
        monkeypatch.setattr(commands.shutil, "which", lambda name: None)

        trailing_slash_url = "https://api.terum.ai/api/"
        result = _configure_mcp(API_KEY, trailing_slash_url, client="claude")

        assert result == "installed"
        data = json.loads(claude_json.read_text())
        entry = data["mcpServers"]["terum"]
        assert "//mcp" not in entry["url"]
        assert entry["headers"]["Authorization"] == f"Bearer {API_KEY}"
