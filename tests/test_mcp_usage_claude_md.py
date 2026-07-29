"""Tests for the MCP-usage CLAUDE.md guidance (Tier 1 of the in-flow delivery work).

_upsert_mcp_usage_claude_md writes a block telling the agent WHEN to call the Terum MCP
tools, because pull-only delivery never fires if the model doesn't reach for them. It must be
idempotent, append-only, warn-don't-crash (mirrors _append_claude_md), wired to BOTH Claude
MCP entry points on a non-failed result, and NEVER written for Cursor (no ~/.claude/CLAUDE.md).
"""
import pytest

from terum_capture import commands
from terum_capture.commands import _upsert_mcp_usage_claude_md, MCP_USAGE_HEADER

API_KEY = "trm_test123456789012345678901234"
API_URL = "https://api.terum.ai/api"


class TestAppendMcpUsage:
    def test_creates_and_writes_block(self, tmp_path, monkeypatch):
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        monkeypatch.setattr(commands, "CLAUDE_MD", claude_md)

        _upsert_mcp_usage_claude_md()

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

        _upsert_mcp_usage_claude_md()
        _upsert_mcp_usage_claude_md()

        assert claude_md.read_text().count(MCP_USAGE_HEADER) == 1

    def test_preserves_existing_content(self, tmp_path, monkeypatch):
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True)
        claude_md.write_text("# My global rules\n\nAlways be concise.")  # no trailing newline
        monkeypatch.setattr(commands, "CLAUDE_MD", claude_md)

        _upsert_mcp_usage_claude_md()

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

        _upsert_mcp_usage_claude_md()  # must not raise

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


class TestUpsertRefresh:
    """The managed-span refresh added 2026-07-29: shipped wording changes must reach
    existing installs on update, without ever touching user content outside the block."""

    LEGACY_BLOCK = (
        "## Terum Team Knowledge (MCP)\n\n"
        "OLD WORDING that shipped in an earlier release.\n"
        "- old bullet\n"
    )

    def _md(self, tmp_path, monkeypatch, content):
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True)
        claude_md.write_text(content)
        monkeypatch.setattr(commands, "CLAUDE_MD", claude_md)
        return claude_md

    def test_fresh_append_includes_end_marker(self, tmp_path, monkeypatch):
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        monkeypatch.setattr(commands, "CLAUDE_MD", claude_md)
        _upsert_mcp_usage_claude_md()
        assert commands.MCP_USAGE_END_MARKER in claude_md.read_text()

    def test_legacy_block_refreshed_up_to_next_heading(self, tmp_path, monkeypatch):
        claude_md = self._md(
            tmp_path, monkeypatch,
            "# Global\n\n" + self.LEGACY_BLOCK + "\n## User Section\n\nkeep me\n",
        )
        _upsert_mcp_usage_claude_md(add_if_missing=False)
        text = claude_md.read_text()
        assert "OLD WORDING" not in text
        assert "check team knowledge before concluding" in text  # current wording arrived
        assert commands.MCP_USAGE_END_MARKER in text             # migrated to marker form
        assert "## User Section" in text and "keep me" in text   # user content untouched
        assert text.count(MCP_USAGE_HEADER) == 1

    def test_legacy_block_at_eof_refreshed(self, tmp_path, monkeypatch):
        claude_md = self._md(tmp_path, monkeypatch, "# Global\n\n" + self.LEGACY_BLOCK)
        _upsert_mcp_usage_claude_md(add_if_missing=False)
        text = claude_md.read_text()
        assert "OLD WORDING" not in text and "# Global" in text

    def test_marker_block_replaced_exactly_content_below_preserved(self, tmp_path, monkeypatch):
        stale = (
            "## Terum Team Knowledge (MCP)\n\nstale marker-form wording\n"
            + commands.MCP_USAGE_END_MARKER + "\n"
        )
        claude_md = self._md(
            tmp_path, monkeypatch,
            "intro\n\n" + stale + "\nnot a heading, plain user text\n## Later\nkeep\n",
        )
        _upsert_mcp_usage_claude_md(add_if_missing=False)
        text = claude_md.read_text()
        assert "stale marker-form wording" not in text
        # the plain-text line right after the marker survives (legacy scan would have eaten it)
        assert "not a heading, plain user text" in text
        assert "## Later" in text and "keep" in text

    def test_refresh_only_never_adds(self, tmp_path, monkeypatch):
        claude_md = self._md(tmp_path, monkeypatch, "# Global\n\nno terum block here\n")
        _upsert_mcp_usage_claude_md(add_if_missing=False)
        assert MCP_USAGE_HEADER not in claude_md.read_text()
