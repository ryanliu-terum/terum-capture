"""Tests for `terum-capture status` (cmd_status) configuration guards.

cmd_status probes {api_url}/keys/me, reading config['api_url'] directly. A config
with an api_key but no api_url must be reported as "Not configured" and exit early,
never fall through to the HTTP probe (which would KeyError on config['api_url']).
"""
from unittest.mock import MagicMock, patch

import pytest

import terum_capture.commands as commands


def _run_status(config):
    """Run cmd_status with load_config mocked; return captured SystemExit + http mock."""
    http = MagicMock()
    with patch.object(commands, "load_config", return_value=config), \
         patch.object(commands, "httpx", http):
        with pytest.raises(SystemExit) as exc:
            commands.cmd_status()
    return exc.value, http


class TestCmdStatusGuards:
    def test_no_api_url_is_unconfigured_not_crash(self, capsys):
        # api_key present, api_url missing -> early "Not configured" exit, no HTTP probe.
        code, http = _run_status({"api_key": "trm_test123456789012"})
        out = capsys.readouterr().out
        assert code.code == 1
        assert "Not configured" in out
        assert "Status:" not in out  # never reached the /keys/me probe
        http.get.assert_not_called()

    def test_no_api_key_is_unconfigured(self, capsys):
        code, http = _run_status({"api_url": "https://api.terum.ai/api"})
        out = capsys.readouterr().out
        assert code.code == 1
        assert "Not configured" in out
        http.get.assert_not_called()

    def test_no_config_is_unconfigured(self, capsys):
        code, http = _run_status(None)
        out = capsys.readouterr().out
        assert code.code == 1
        assert "Not configured" in out
        http.get.assert_not_called()
