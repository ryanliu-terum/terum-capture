"""Tests for `terum-capture setup-hook` (hook-only refresh, no key minted)."""
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
