"""Tests for the once-a-day upkeep pass (self-heal + staleness check)."""
import time
from unittest.mock import MagicMock, patch

import pytest

import terum_capture.maintenance as m
from terum_capture import __version__


@pytest.fixture
def tmp_terum(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "TERUM_DIR", tmp_path)
    monkeypatch.setattr(m, "STAMP_FILE", tmp_path / ".last_maintenance")
    monkeypatch.setattr(m, "UPDATE_MARKER", tmp_path / ".update_available")
    return tmp_path


class TestVersionCompare:
    @pytest.mark.parametrize("latest,current,expected", [
        ("0.2.0", "0.1.0", True),
        ("0.1.0", "0.2.0", False),
        ("0.2.0", "0.2.0", False),
        ("1.0.0", "0.9.9", True),
        ("0.10.0", "0.9.0", True),    # numeric compare, not lexical
        ("v0.3.0", "0.2.0", True),    # leading "v" tolerated
        ("0.3.0rc1", "0.2.0", True),  # pre-release suffix stripped to leading digits
        ("", "0.1.0", False),
        ("garbage", "0.1.0", False),
    ])
    def test_is_newer(self, latest, current, expected):
        assert m.is_newer(latest, current) is expected


class TestDueGuard:
    def test_due_when_no_stamp(self, tmp_terum):
        assert m._due(time.time()) is True

    def test_not_due_right_after_touch(self, tmp_terum):
        now = time.time()
        m._touch_stamp(now)
        assert m._due(now + 10) is False

    def test_due_after_interval(self, tmp_terum):
        now = time.time()
        m._touch_stamp(now)
        assert m._due(now + m.CHECK_INTERVAL_SECONDS + 1) is True


class TestRunDailyMaintenance:
    def _config(self):
        return {"api_url": "https://api.terum.ai/api", "api_key": "trm_abc123"}

    def test_skips_everything_when_not_due(self, tmp_terum):
        m._touch_stamp(time.time())
        heal = MagicMock()
        with patch.object(m, "_self_heal_hook", heal), patch.object(m, "httpx") as httpx_mock:
            m.run_daily_maintenance(self._config())
        heal.assert_not_called()
        httpx_mock.get.assert_not_called()

    def test_stamps_and_heals_when_due(self, tmp_terum):
        heal = MagicMock()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"version": __version__}  # current -> no nag
        with patch.object(m, "_self_heal_hook", heal), patch.object(m, "httpx") as httpx_mock:
            httpx_mock.get.return_value = resp
            m.run_daily_maintenance(self._config())
        heal.assert_called_once()
        assert (tmp_terum / ".last_maintenance").exists()

    def test_records_marker_and_nags_when_behind(self, tmp_terum, capsys):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"version": "9.9.9"}
        with patch.object(m, "_self_heal_hook"), patch.object(m, "httpx") as httpx_mock:
            httpx_mock.get.return_value = resp
            m.run_daily_maintenance(self._config())
        err = capsys.readouterr().err
        assert "9.9.9" in err and "terum-capture update" in err
        assert m.read_update_available() == "9.9.9"

    def test_clears_stale_marker_when_current(self, tmp_terum):
        (tmp_terum / ".update_available").write_text("9.9.9")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"version": __version__}
        with patch.object(m, "_self_heal_hook"), patch.object(m, "httpx") as httpx_mock:
            httpx_mock.get.return_value = resp
            m.run_daily_maintenance(self._config())
        assert not (tmp_terum / ".update_available").exists()
        assert m.read_update_available() is None

    def test_fails_open_on_404(self, tmp_terum, capsys):
        resp = MagicMock(status_code=404)
        with patch.object(m, "_self_heal_hook"), patch.object(m, "httpx") as httpx_mock:
            httpx_mock.get.return_value = resp
            m.run_daily_maintenance(self._config())
        assert capsys.readouterr().err == ""
        assert m.read_update_available() is None

    def test_fails_open_on_network_error(self, tmp_terum):
        with patch.object(m, "_self_heal_hook"), patch.object(m, "httpx") as httpx_mock:
            httpx_mock.get.side_effect = Exception("offline")
            m.run_daily_maintenance(self._config())  # must not raise
        assert m.read_update_available() is None

    def test_unconfigured_still_self_heals_but_skips_check(self, tmp_terum):
        heal = MagicMock()
        with patch.object(m, "_self_heal_hook", heal), patch.object(m, "httpx") as httpx_mock:
            m.run_daily_maintenance(None)
        heal.assert_called_once()          # self-heal runs even with no API key
        httpx_mock.get.assert_not_called()  # but no server call without config
