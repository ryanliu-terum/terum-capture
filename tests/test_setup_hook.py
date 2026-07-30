"""Tests for `terum-capture setup-hook` (hook-only refresh, no key minted).

These tests are why tests/conftest.py exists. They patched `_configure_hook` and assumed that
covered `cmd_setup_hook()`'s side effects. PR #11 then added two more real-filesystem writes to
that function and these tests were not updated, so the suite began rewriting the developer's live
~/.claude/settings.json and ~/.claude/CLAUDE.md on every run (bug-559). Every side effect is now
pinned explicitly, so widening the function again fails here instead of leaking into ~.
"""
from unittest.mock import patch

import terum_capture.commands as commands


def test_cmd_setup_hook_refreshes_config_and_prints(capsys):
    with patch.object(commands, "_configure_hook") as cfg:
        commands.cmd_setup_hook()
    cfg.assert_called_once()
    out = capsys.readouterr().out
    assert "hook refreshed" in out.lower()


def test_cmd_setup_hook_mints_no_key(capsys):
    # It must never touch httpx / key creation — unlike full `setup`.
    with patch.object(commands, "_configure_hook"), patch.object(commands, "httpx") as http:
        commands.cmd_setup_hook()
    http.post.assert_not_called()
    http.get.assert_not_called()


def test_cmd_setup_hook_refreshes_all_managed_artifacts():
    """The full side-effect set, pinned. `setup-hook` is the refresh-drift repair command, so a
    managed artifact it forgets stays stale forever — and one it touches WITHOUT a test is how
    bug-559 reached the developer's real home directory."""
    with patch.object(commands, "_configure_hook") as cfg, \
         patch.object(commands, "_upsert_mcp_usage_claude_md") as mcp, \
         patch.object(commands, "_refresh_delivery_hook_if_installed") as delivery:
        commands.cmd_setup_hook()

    cfg.assert_called_once()
    delivery.assert_called_once()
    # add_if_missing=False is load-bearing: a maintenance command must REFRESH the nudge block,
    # never ADD it to a machine that never opted in.
    mcp.assert_called_once_with(add_if_missing=False)
