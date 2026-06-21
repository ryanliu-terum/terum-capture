"""Tests for Stop-hook install/uninstall in settings.json.

These guard the matcher-group shape Claude Code requires for Stop hooks
({"hooks": [{...}]}) and the migration of the legacy flat shape
({"type", "command", "timeout"}) written by older versions. Getting the
shape wrong made `claude doctor` report the hook as invalid.
"""
import json
import tempfile
from pathlib import Path

import pytest

import terum_capture.commands as commands

GROUP = {"hooks": [{"type": "command", "command": "terum-capture upload", "timeout": 15}]}
FLAT = {"type": "command", "command": "terum-capture upload", "timeout": 15}
OTHER = {"hooks": [{"type": "command", "command": "echo hi"}]}


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(commands, "CLAUDE_SETTINGS", path)
    return path


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


class TestConfigureHook:
    def test_fresh_install_writes_matcher_group_shape(self, settings_path):
        commands._configure_hook()
        assert _read(settings_path)["hooks"]["Stop"] == [GROUP]

    def test_migrates_legacy_flat_entry_in_place(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [FLAT]}})
        commands._configure_hook()
        # Migrated to the group shape, not appended alongside the flat one.
        assert _read(settings_path)["hooks"]["Stop"] == [GROUP]

    def test_idempotent_when_already_correct(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [GROUP]}})
        commands._configure_hook()
        assert _read(settings_path)["hooks"]["Stop"] == [GROUP]

    def test_collapses_duplicate_entries(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [FLAT, FLAT]}})
        commands._configure_hook()
        assert _read(settings_path)["hooks"]["Stop"] == [GROUP]

    def test_preserves_unrelated_stop_hook(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [OTHER]}})
        commands._configure_hook()
        assert _read(settings_path)["hooks"]["Stop"] == [OTHER, GROUP]

    def test_preserves_other_settings_keys(self, settings_path):
        _write(settings_path, {"model": "opus", "hooks": {"Stop": []}})
        commands._configure_hook()
        out = _read(settings_path)
        assert out["model"] == "opus"
        assert out["hooks"]["Stop"] == [GROUP]


class TestRemoveHook:
    def test_removes_group_shape(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [GROUP]}})
        commands._remove_hook()
        assert "hooks" not in _read(settings_path)

    def test_removes_legacy_flat_shape_keeping_others(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [FLAT, OTHER]}})
        commands._remove_hook()
        assert _read(settings_path)["hooks"]["Stop"] == [OTHER]

    def test_noop_when_settings_absent(self, settings_path):
        # No file written; should not raise or create one.
        commands._remove_hook()
        assert not settings_path.exists()
