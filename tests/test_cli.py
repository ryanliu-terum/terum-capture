"""Tests for the CLI dispatcher (src/terum_capture/cli.py), covering the
Tier 1 --mcp/--no-mcp flag wiring on `setup` and the Tier 2 `mcp install`
subcommand (SPEC-mcp-install.md §7).

`cli.py` imports command functions lazily INSIDE each branch
(`from terum_capture.commands import cmd_setup`), so patching the attribute
on `terum_capture.cli.cmd_setup` etc. would not be seen by `main()` — instead
monkeypatch the attribute on the already-imported `terum_capture.commands`
module, then invoke `main()` with `sys.argv` set. This mirrors the lazy-import
pattern the rest of the command functions are already tested against.
"""
import sys

import pytest

from terum_capture import commands
from terum_capture.cli import main


def run_cli(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["terum-capture"] + argv)
    main()


class TestSetupMcpFlagWiring:
    def test_no_flags_passes_none(self, monkeypatch):
        recorded = {}

        def fake_cmd_setup(api_url=None, token=None, mcp=None, delivery=None):
            recorded["mcp"] = mcp
            recorded["api_url"] = api_url
            recorded["token"] = token
            recorded["delivery"] = delivery

        monkeypatch.setattr(commands, "cmd_setup", fake_cmd_setup)

        run_cli(monkeypatch, ["setup"])

        assert recorded["mcp"] is None
        assert recorded["delivery"] is None
        assert recorded["api_url"] is None
        assert recorded["token"] is None

    def test_mcp_flag_forces_true(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            commands, "cmd_setup",
            lambda api_url=None, token=None, mcp=None, delivery=None: recorded.update(mcp=mcp, delivery=delivery),
        )

        run_cli(monkeypatch, ["setup", "--mcp"])

        assert recorded["mcp"] is True

    def test_no_mcp_flag_forces_false(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            commands, "cmd_setup",
            lambda api_url=None, token=None, mcp=None, delivery=None: recorded.update(mcp=mcp, delivery=delivery),
        )

        run_cli(monkeypatch, ["setup", "--no-mcp"])

        assert recorded["mcp"] is False

    def test_mcp_flag_combined_with_url_and_token(self, monkeypatch):
        recorded = {}

        def fake_cmd_setup(api_url=None, token=None, mcp=None, delivery=None):
            recorded["api_url"] = api_url
            recorded["token"] = token
            recorded["mcp"] = mcp
            recorded["delivery"] = delivery

        monkeypatch.setattr(commands, "cmd_setup", fake_cmd_setup)

        run_cli(monkeypatch, [
            "setup", "--url", "https://example.com/api", "--token", "abc123", "--mcp",
        ])

        assert recorded["api_url"] == "https://example.com/api"
        assert recorded["token"] == "abc123"
        assert recorded["mcp"] is True


class TestMcpInstallSubcommand:
    def test_mcp_install_default_client(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            commands, "cmd_mcp_install", lambda client: recorded.setdefault("client", client)
        )

        run_cli(monkeypatch, ["mcp", "install"])

        assert recorded["client"] == "claude"

    def test_mcp_install_explicit_client(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            commands, "cmd_mcp_install", lambda client: recorded.setdefault("client", client)
        )

        run_cli(monkeypatch, ["mcp", "install", "--client", "cursor"])

        assert recorded["client"] == "cursor"

    def test_mcp_install_unknown_client_exits_1(self, monkeypatch, capsys):
        called = {}
        monkeypatch.setattr(
            commands, "cmd_mcp_install", lambda client: called.setdefault("hit", True)
        )

        with pytest.raises(SystemExit) as exc_info:
            run_cli(monkeypatch, ["mcp", "install", "--client", "vscode"])

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Error: unknown MCP client 'vscode'. Use claude or cursor." in out
        assert "hit" not in called

    def test_mcp_install_client_flag_missing_value_exits_1(self, monkeypatch, capsys):
        called = {}
        monkeypatch.setattr(
            commands, "cmd_mcp_install", lambda client: called.setdefault("hit", True)
        )

        with pytest.raises(SystemExit) as exc_info:
            run_cli(monkeypatch, ["mcp", "install", "--client"])

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Error: --client requires a value (claude|cursor)." in out
        assert "hit" not in called

    def test_mcp_install_client_cursor_still_dispatches(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            commands, "cmd_mcp_install", lambda client: recorded.setdefault("client", client)
        )

        run_cli(monkeypatch, ["mcp", "install", "--client", "cursor"])

        assert recorded["client"] == "cursor"

    def test_mcp_without_install_prints_usage_and_exits_1(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_cli(monkeypatch, ["mcp"])

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Usage: terum-capture mcp install [--client claude|cursor]" in out

    def test_mcp_unknown_subcommand_prints_usage_and_exits_1(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_cli(monkeypatch, ["mcp", "bogus"])

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Usage: terum-capture mcp install [--client claude|cursor]" in out


class TestUsageBanners:
    def test_no_args_lists_mcp_in_commands(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_cli(monkeypatch, [])

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Commands: upload, setup, backfill, status, update, setup-hook, logout, mcp" in out

    def test_unknown_command_lists_mcp_in_commands(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_cli(monkeypatch, ["bogus"])

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Unknown command: bogus" in out
        assert "Commands: upload, setup, backfill, status, update, setup-hook, logout, mcp" in out


class TestExistingCommandsUnaffected:
    """Adversarial-ish regression check: the new elif branch must not have
    disturbed dispatch for the pre-existing commands."""

    def test_status_still_dispatches(self, monkeypatch):
        called = {}
        monkeypatch.setattr(commands, "cmd_status", lambda: called.setdefault("hit", True))

        run_cli(monkeypatch, ["status"])

        assert called.get("hit") is True

    def test_logout_still_dispatches(self, monkeypatch):
        called = {}
        monkeypatch.setattr(commands, "cmd_logout", lambda: called.setdefault("hit", True))

        run_cli(monkeypatch, ["logout"])

        assert called.get("hit") is True

    def test_upload_still_dispatches(self, monkeypatch):
        from terum_capture import upload as upload_module

        called = {}
        monkeypatch.setattr(upload_module, "cmd_upload", lambda: called.setdefault("hit", True))

        run_cli(monkeypatch, ["upload"])

        assert called.get("hit") is True


class TestSetupDeliveryFlagWiring:
    def _record(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            commands, "cmd_setup",
            lambda api_url=None, token=None, mcp=None, delivery=None: recorded.update(delivery=delivery),
        )
        return recorded

    def test_delivery_flag_forces_true(self, monkeypatch):
        recorded = self._record(monkeypatch)
        run_cli(monkeypatch, ["setup", "--delivery"])
        assert recorded["delivery"] is True

    def test_no_delivery_flag_forces_false(self, monkeypatch):
        recorded = self._record(monkeypatch)
        run_cli(monkeypatch, ["setup", "--no-delivery"])
        assert recorded["delivery"] is False
