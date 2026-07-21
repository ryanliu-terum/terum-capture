"""Tests for the MCP-usage CLAUDE.md guidance (Tier 1 of the in-flow delivery work).

_append_mcp_usage_claude_md writes a block telling the agent WHEN to call the Terum MCP
tools, because pull-only delivery never fires if the model doesn't reach for them. It must be
idempotent, append-only, warn-don't-crash (mirrors _append_claude_md), wired to BOTH Claude
MCP entry points on a non-failed result, and NEVER written for Cursor (no ~/.claude/CLAUDE.md).
"""
import pytest

from terum_capture import commands
from terum_capture.commands import _append_mcp_usage_claude_md, MCP_USAGE_HEADER

API_KEY = "trm_test123456789012345678901234"
API_URL = "https://api.terum.ai/api"


class TestAppendMcpUsage:
    def test_creates_and_writes_block(self, tmp_path, monkeypatch):
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        monkeypatch.setattr(commands, "CLAUDE_MD", claude_md)

        _append_mcp_usage_claude_md()

        text = claude_md.read_text()
        assert MCP_USAGE_HEADER in text
        # names all three tools so the agent knows what to call
        assert "search_team_knowledge" in text
        assert "check_decision" in text
        assert "get_standing_decisions" in text
        # draws the line vs. automatic capture so it doesn't read as contradicting the capture block
        assert "pull-only" in text

    def test_idempotent_no_duplicate(self, tmp_path, monkeypatch):
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        monkeypatch.setattr(commands, "CLAUDE_MD", claude_md)

        _append_mcp_usage_claude_md()
        _append_mcp_usage_claude_md()

        assert claude_md.read_text().count(MCP_USAGE_HEADER) == 1

    def test_preserves_existing_content(self, tmp_path, monkeypatch):
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True)
        claude_md.write_text("# My global rules\n\nAlways be concise.")  # no trailing newline
        monkeypatch.setattr(commands, "CLAUDE_MD", claude_md)

        _append_mcp_usage_claude_md()

        text = claude_md.read_text()
        assert "# My global rules" in text
        assert "Always be concise." in text
        assert MCP_USAGE_HEADER in text
        # a separating newline was inserted before the appended block
        assert "Always be concise.\n" in text

    def test_warn_dont_crash_on_unwritable(self, tmp_path, monkeypatch, capsys):
        # Point CLAUDE_MD at a path whose parent is a FILE, so mkdir/open raises — must be swallowed.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        monkeypatch.setattr(commands, "CLAUDE_MD", blocker / "CLAUDE.md")

        _append_mcp_usage_claude_md()  # must not raise

        assert "Warning" in capsys.readouterr().out


class TestWiredIntoClaudeEntryPoints:
    def _fixture(self, tmp_path, monkeypatch, result):
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        monkeypatch.setattr(commands, "CLAUDE_MD", claude_md)
        monkeypatch.setattr(commands, "_configure_mcp", lambda *a, **k: result)
        return claude_md

    @pytest.mark.parametrize("result", ["installed", "already"])
    def test_mcp_install_claude_appends_on_success(self, tmp_path, monkeypatch, result):
        claude_md = self._fixture(tmp_path, monkeypatch, result)
        monkeypatch.setattr(commands, "load_config", lambda: {"api_key": API_KEY, "api_url": API_URL})

        commands.cmd_mcp_install(client="claude")

        assert MCP_USAGE_HEADER in claude_md.read_text()

    def test_mcp_install_cursor_never_appends(self, tmp_path, monkeypatch):
        # Cursor has no ~/.claude/CLAUDE.md — the guidance must NOT be written for it.
        claude_md = self._fixture(tmp_path, monkeypatch, "installed")
        monkeypatch.setattr(commands, "load_config", lambda: {"api_key": API_KEY, "api_url": API_URL})

        commands.cmd_mcp_install(client="cursor")

        assert not claude_md.exists()

    def test_mcp_install_failed_does_not_append(self, tmp_path, monkeypatch):
        claude_md = self._fixture(tmp_path, monkeypatch, "failed")
        monkeypatch.setattr(commands, "load_config", lambda: {"api_key": API_KEY, "api_url": API_URL})

        with pytest.raises(SystemExit):
            commands.cmd_mcp_install(client="claude")  # failed → exit 1

        assert not claude_md.exists()

    def test_interactive_forced_yes_appends(self, tmp_path, monkeypatch):
        claude_md = self._fixture(tmp_path, monkeypatch, "installed")

        commands._maybe_configure_mcp_interactive(API_KEY, API_URL, choice=True)

        assert MCP_USAGE_HEADER in claude_md.read_text()
