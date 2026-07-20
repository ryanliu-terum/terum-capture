"""Tests for the Tier 1 setup-prompt and Tier 2 `mcp install` entry points
(SPEC-mcp-install.md §4, §6, §8, §10). These test the WIRING around
`_configure_mcp`, not the writer itself (that's tests/test_mcp_configure.py) —
`_configure_mcp` is monkeypatched throughout.
"""
import builtins

import pytest

from terum_capture import commands
from terum_capture.commands import _maybe_configure_mcp_interactive, cmd_mcp_install

API_KEY = "trm_test123456789012345678901234"
API_URL = "https://api.terum.ai/api"


class FakeStdin:
    """Stand-in for sys.stdin exposing only the isatty() this code calls."""

    def __init__(self, is_a_tty: bool):
        self._is_a_tty = is_a_tty

    def isatty(self) -> bool:
        return self._is_a_tty


class TestMaybeConfigureMcpInteractive:
    def test_non_tty_choice_none_skips_silently(self, monkeypatch):
        monkeypatch.setattr(commands.sys, "stdin", FakeStdin(False))
        called = {}
        monkeypatch.setattr(commands, "_configure_mcp", lambda *a, **k: called.setdefault("hit", True))

        _maybe_configure_mcp_interactive(API_KEY, API_URL, None)

        assert "hit" not in called

    def test_forced_yes_calls_even_when_non_tty(self, monkeypatch):
        monkeypatch.setattr(commands.sys, "stdin", FakeStdin(False))
        recorded = {}

        def fake_configure_mcp(api_key, api_url, client="claude"):
            recorded["api_key"] = api_key
            recorded["api_url"] = api_url
            recorded["client"] = client
            return "installed"

        monkeypatch.setattr(commands, "_configure_mcp", fake_configure_mcp)

        _maybe_configure_mcp_interactive(API_KEY, API_URL, True)

        assert recorded["api_key"] == API_KEY
        assert recorded["api_url"] == API_URL
        assert recorded["client"] == "claude"

    def test_forced_skip_never_calls_regardless_of_tty(self, monkeypatch):
        monkeypatch.setattr(commands.sys, "stdin", FakeStdin(True))
        called = {}
        monkeypatch.setattr(commands, "_configure_mcp", lambda *a, **k: called.setdefault("hit", True))

        _maybe_configure_mcp_interactive(API_KEY, API_URL, False)

        assert "hit" not in called

    def test_interactive_default_yes_calls(self, monkeypatch):
        monkeypatch.setattr(commands.sys, "stdin", FakeStdin(True))
        monkeypatch.setattr(builtins, "input", lambda prompt="": "")
        called = {}

        def fake_configure_mcp(*a, **k):
            called["hit"] = True
            return "installed"

        monkeypatch.setattr(commands, "_configure_mcp", fake_configure_mcp)

        _maybe_configure_mcp_interactive(API_KEY, API_URL, None)

        assert called.get("hit") is True

    def test_interactive_no_skips(self, monkeypatch):
        monkeypatch.setattr(commands.sys, "stdin", FakeStdin(True))
        monkeypatch.setattr(builtins, "input", lambda prompt="": "n")
        called = {}
        monkeypatch.setattr(commands, "_configure_mcp", lambda *a, **k: called.setdefault("hit", True))

        _maybe_configure_mcp_interactive(API_KEY, API_URL, None)

        assert "hit" not in called

    def test_interactive_whitespace_uppercase_yes_calls(self, monkeypatch):
        """Adversarial: '  Y  ' (whitespace + uppercase) must be treated as yes."""
        monkeypatch.setattr(commands.sys, "stdin", FakeStdin(True))
        monkeypatch.setattr(builtins, "input", lambda prompt="": "  Y  ")
        called = {}

        def fake_configure_mcp(*a, **k):
            called["hit"] = True
            return "installed"

        monkeypatch.setattr(commands, "_configure_mcp", fake_configure_mcp)

        _maybe_configure_mcp_interactive(API_KEY, API_URL, None)

        assert called.get("hit") is True

    def test_headless_eof_on_input_does_not_crash(self, monkeypatch):
        """A TTY that then raises EOFError/KeyboardInterrupt on input() must be treated
        as a skip, never crash setup."""
        monkeypatch.setattr(commands.sys, "stdin", FakeStdin(True))

        def raise_eof(prompt=""):
            raise EOFError()

        monkeypatch.setattr(builtins, "input", raise_eof)
        called = {}
        monkeypatch.setattr(commands, "_configure_mcp", lambda *a, **k: called.setdefault("hit", True))

        _maybe_configure_mcp_interactive(API_KEY, API_URL, None)

        assert "hit" not in called


class TestCmdMcpInstall:
    def test_no_config_exits_1(self, monkeypatch, capsys):
        monkeypatch.setattr(commands, "load_config", lambda: None)

        with pytest.raises(SystemExit) as exc_info:
            cmd_mcp_install()

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Not configured. Run: terum-capture setup" in out

    def test_no_api_key_exits_1(self, monkeypatch, capsys):
        monkeypatch.setattr(commands, "load_config", lambda: {"api_url": API_URL})

        with pytest.raises(SystemExit) as exc_info:
            cmd_mcp_install()

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Not configured. Run: terum-capture setup" in out

    def test_happy_path_calls_configure_mcp_with_stored_config(self, monkeypatch):
        monkeypatch.setattr(
            commands, "load_config", lambda: {"api_key": API_KEY, "api_url": API_URL}
        )
        recorded = {}

        def fake_configure_mcp(api_key, api_url, client="claude"):
            recorded["api_key"] = api_key
            recorded["api_url"] = api_url
            recorded["client"] = client
            return "installed"

        monkeypatch.setattr(commands, "_configure_mcp", fake_configure_mcp)

        cmd_mcp_install(client="cursor")

        assert recorded["api_key"] == API_KEY
        assert recorded["api_url"] == API_URL
        assert recorded["client"] == "cursor"

    def test_happy_path_default_client_is_claude(self, monkeypatch):
        monkeypatch.setattr(
            commands, "load_config", lambda: {"api_key": API_KEY, "api_url": API_URL}
        )
        recorded = {}

        def fake_configure_mcp(api_key, api_url, client="claude"):
            recorded["client"] = client
            return "installed"

        monkeypatch.setattr(commands, "_configure_mcp", fake_configure_mcp)

        cmd_mcp_install()

        assert recorded["client"] == "claude"

    def test_failed_configure_mcp_exits_1(self, monkeypatch, capsys):
        """FIX 2b: cmd_mcp_install must sys.exit(1) when _configure_mcp reports "failed",
        so the exit-code contract holds for a write failure, not just missing config."""
        monkeypatch.setattr(
            commands, "load_config", lambda: {"api_key": API_KEY, "api_url": API_URL}
        )
        monkeypatch.setattr(commands, "_configure_mcp", lambda *a, **k: "failed")

        with pytest.raises(SystemExit) as exc_info:
            cmd_mcp_install()

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Could not configure MCP for Claude Code." in out

    def test_missing_api_url_falls_back_to_default(self, monkeypatch):
        """FIX 4: cmd_mcp_install must not KeyError on a config missing api_url — it
        should fall back to DEFAULT_API_URL, matching cmd_status's config.get() pattern."""
        monkeypatch.setattr(commands, "load_config", lambda: {"api_key": API_KEY})
        recorded = {}

        def fake_configure_mcp(api_key, api_url, client="claude"):
            recorded["api_url"] = api_url
            return "installed"

        monkeypatch.setattr(commands, "_configure_mcp", fake_configure_mcp)

        cmd_mcp_install()

        assert recorded["api_url"] == commands.DEFAULT_API_URL
