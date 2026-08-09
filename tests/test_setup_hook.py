"""Tests for `terum-capture setup-hook` (hook-only refresh, no key minted).

These tests are why tests/conftest.py exists. They patched `_configure_hook` and assumed that
covered `cmd_setup_hook()`'s side effects. PR #11 then added two more real-filesystem writes to
that function and these tests were not updated, so the suite began rewriting the developer's live
~/.claude/settings.json and ~/.claude/CLAUDE.md on every run (bug-559). Every side effect is now
pinned explicitly, so widening the function again fails here instead of leaking into ~.

Since capture became project-scoped, `setup-hook` no longer writes one fixed path: it refreshes
whichever scopes ALREADY hold a terum hook (global and/or the current project) and installs none.
That refresh-only rule is the load-bearing part and is pinned below — without it `update`, which
runs this on every upgrade, would hand a machine-wide hook to someone who chose a per-project one.
"""
from unittest.mock import patch

import terum_capture.commands as commands


def _install_global_hook():
    """Put a real terum Stop hook in the (isolated) global settings, via the real installer."""
    commands._configure_hook(commands.CLAUDE_SETTINGS)


def test_cmd_setup_hook_refreshes_an_installed_hook(capsys):
    _install_global_hook()
    with patch.object(commands, "_configure_hook") as cfg:
        commands.cmd_setup_hook()
    cfg.assert_called_once_with(commands.CLAUDE_SETTINGS)
    out = capsys.readouterr().out
    assert "hook refreshed" in out.lower()


def test_cmd_setup_hook_refreshes_the_project_hook_too(capsys):
    """Both scopes can hold a hook, and a drift-repair command that only knew about one
    would leave the other pinned to a stale interpreter/timeout forever."""
    project_settings, _ = commands._scope_targets(False)
    commands._configure_hook(project_settings)

    with patch.object(commands, "_configure_hook") as cfg:
        commands.cmd_setup_hook()

    cfg.assert_called_once_with(project_settings)
    assert "hook refreshed" in capsys.readouterr().out.lower()


def test_cmd_setup_hook_never_installs_a_hook(capsys):
    """The whole point of refresh-only. With no hook anywhere, `setup-hook` must create
    nothing — `update` runs it unconditionally, including on a machine mid-onboarding."""
    project_settings, _ = commands._scope_targets(False)

    commands.cmd_setup_hook()

    assert not commands.CLAUDE_SETTINGS.exists()
    assert not project_settings.exists()
    assert "no terum hook found" in capsys.readouterr().out.lower()


def test_cmd_setup_hook_mints_no_key():
    # It must never touch httpx / key creation — unlike full `setup`.
    with patch.object(commands, "_refresh_installed_hooks"), patch.object(commands, "httpx") as http:
        commands.cmd_setup_hook()
    http.post.assert_not_called()
    http.get.assert_not_called()


def test_cmd_setup_hook_refreshes_all_managed_artifacts():
    """The full side-effect set, pinned. `setup-hook` is the refresh-drift repair command, so a
    managed artifact it forgets stays stale forever — and one it touches WITHOUT a test is how
    bug-559 reached the developer's real home directory."""
    with patch.object(commands, "_refresh_installed_hooks") as hooks, \
         patch.object(commands, "_upsert_mcp_usage_claude_md") as mcp, \
         patch.object(commands, "_refresh_delivery_hook_if_installed") as delivery:
        commands.cmd_setup_hook()

    hooks.assert_called_once_with()
    delivery.assert_called_once()
    # add_if_missing=False is load-bearing: a maintenance command must REFRESH the nudge block,
    # never ADD it to a machine that never opted in.
    mcp.assert_called_once_with(add_if_missing=False)
