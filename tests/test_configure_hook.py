"""Tests for Stop-hook install/uninstall in settings.json.

These guard:
  - the matcher-group shape Claude Code requires for Stop hooks ({"hooks": [{...}]});
  - migration of older forms — the legacy flat shape ({"type", "command", "timeout"})
    and the legacy bare `terum-capture upload` shim command — to the current form;
  - routing the hook through the signed Python interpreter (`python -m terum_capture
    upload`) instead of the unsigned console-script .exe that Windows Smart App
    Control / WDAC block on enforcing machines (which silently killed capture).
"""
import json
import sys
from pathlib import Path

import pytest

import terum_capture.commands as commands


def _group() -> dict:
    """The canonical entry the installer writes today: python-routed, group shape."""
    return {"hooks": [commands._hook_entry()]}


# Legacy forms written by older versions — the bare, unsigned console-script shim.
LEGACY_FLAT = {"type": "command", "command": "terum-capture upload", "timeout": 15}
LEGACY_GROUP = {"hooks": [{"type": "command", "command": "terum-capture upload", "timeout": 15}]}
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


class TestHookCommand:
    def test_routes_through_signed_interpreter_not_the_shim(self):
        cmd = commands._hook_command()
        # Invokes the running interpreter, not the unsigned `terum-capture.exe` shim.
        assert cmd == f'"{Path(sys.executable).as_posix()}" -m terum_capture upload'
        assert "-m terum_capture upload" in cmd
        assert cmd != "terum-capture upload"

    def test_detects_legacy_and_python_command_forms(self):
        assert commands._is_our_stop_entry(LEGACY_FLAT)
        assert commands._is_our_stop_entry(LEGACY_GROUP)
        assert commands._is_our_stop_entry(_group())
        assert not commands._is_our_stop_entry(OTHER)


class TestConfigureHook:
    def test_fresh_install_writes_python_routed_group_shape(self, settings_path):
        commands._configure_hook()
        assert _read(settings_path)["hooks"]["Stop"] == [_group()]

    def test_migrates_legacy_flat_entry_to_python_form(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [LEGACY_FLAT]}})
        commands._configure_hook()
        # Migrated to the group shape AND the python-routed command, not appended.
        assert _read(settings_path)["hooks"]["Stop"] == [_group()]

    def test_migrates_legacy_group_command_to_python_form(self, settings_path):
        # Right shape already, but the old bare-shim command — refresh it in place.
        _write(settings_path, {"hooks": {"Stop": [LEGACY_GROUP]}})
        commands._configure_hook()
        assert _read(settings_path)["hooks"]["Stop"] == [_group()]

    def test_idempotent_when_already_correct(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [_group()]}})
        commands._configure_hook()
        assert _read(settings_path)["hooks"]["Stop"] == [_group()]

    def test_collapses_duplicate_entries(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [LEGACY_FLAT, LEGACY_FLAT]}})
        commands._configure_hook()
        assert _read(settings_path)["hooks"]["Stop"] == [_group()]

    def test_preserves_unrelated_stop_hook(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [OTHER]}})
        commands._configure_hook()
        assert _read(settings_path)["hooks"]["Stop"] == [OTHER, _group()]

    def test_preserves_other_settings_keys(self, settings_path):
        _write(settings_path, {"model": "opus", "hooks": {"Stop": []}})
        commands._configure_hook()
        out = _read(settings_path)
        assert out["model"] == "opus"
        assert out["hooks"]["Stop"] == [_group()]


class TestRemoveHook:
    def test_removes_python_form(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [_group()]}})
        commands._remove_hook()
        assert "hooks" not in _read(settings_path)

    def test_removes_legacy_flat_shape_keeping_others(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [LEGACY_FLAT, OTHER]}})
        commands._remove_hook()
        assert _read(settings_path)["hooks"]["Stop"] == [OTHER]

    def test_noop_when_settings_absent(self, settings_path):
        # No file written; should not raise or create one.
        commands._remove_hook()
        assert not settings_path.exists()
