"""Delivery opt-in surfaces (2026-07-29): the default-YES setup prompt and the
refresh-only behaviors setup-hook/update gained.

Consent law pinned here: `setup` may ask (default Yes on a TTY) or honor --delivery /
--no-delivery; `setup-hook` (what `update` runs) refreshes managed artifacts but NEVER
installs the delivery hook or the nudge block on a machine that didn't opt in.
"""
import json

from terum_capture import commands, delivery_hooks
from terum_capture.commands import (
    _maybe_install_delivery_interactive,
    _refresh_delivery_hook_if_installed,
)
from terum_capture.delivery_hooks import DELIVERY_HOOK_MARKER


class TestSetupPrompt:
    def _spy(self, monkeypatch):
        calls = []
        monkeypatch.setattr(delivery_hooks, "install_delivery_hooks", lambda: calls.append(1))
        return calls

    def test_tty_default_yes_installs(self, monkeypatch):
        calls = self._spy(monkeypatch)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "")  # bare Enter = default Yes
        _maybe_install_delivery_interactive(None)
        assert calls == [1]

    def test_tty_no_skips(self, monkeypatch):
        calls = self._spy(monkeypatch)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_: "n")
        _maybe_install_delivery_interactive(None)
        assert calls == []

    def test_non_tty_skips_silently(self, monkeypatch):
        calls = self._spy(monkeypatch)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        _maybe_install_delivery_interactive(None)
        assert calls == []

    def test_forced_yes_never_prompts(self, monkeypatch):
        calls = self._spy(monkeypatch)

        def no_input(*_):
            raise AssertionError("must not prompt on --delivery")

        monkeypatch.setattr("builtins.input", no_input)
        _maybe_install_delivery_interactive(True)
        assert calls == [1]

    def test_forced_no(self, monkeypatch):
        calls = self._spy(monkeypatch)
        _maybe_install_delivery_interactive(False)
        assert calls == []

    def test_eof_on_input_treated_as_skip(self, monkeypatch):
        calls = self._spy(monkeypatch)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def eof(*_):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        _maybe_install_delivery_interactive(None)
        assert calls == []


class TestSetupHookRefresh:
    def _settings(self, tmp_path, monkeypatch, content):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        if content is not None:
            settings.write_text(content)
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", settings)
        return settings

    def test_refreshes_when_installed(self, tmp_path, monkeypatch):
        ours = {"matcher": "*", "hooks": [{"command": f"x {DELIVERY_HOOK_MARKER} prompt"}]}
        self._settings(tmp_path, monkeypatch, json.dumps({"hooks": {"UserPromptSubmit": [ours]}}))
        calls = []
        monkeypatch.setattr(delivery_hooks, "install_delivery_hooks", lambda: calls.append(1))
        _refresh_delivery_hook_if_installed()
        assert calls == [1]

    def test_never_installs_when_absent(self, tmp_path, monkeypatch):
        self._settings(tmp_path, monkeypatch, json.dumps({"hooks": {"Stop": []}}))
        calls = []
        monkeypatch.setattr(delivery_hooks, "install_delivery_hooks", lambda: calls.append(1))
        _refresh_delivery_hook_if_installed()
        assert calls == []

    def test_missing_settings_noop(self, tmp_path, monkeypatch):
        settings = tmp_path / ".claude" / "settings.json"  # never written
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", settings)
        _refresh_delivery_hook_if_installed()  # must not raise or create the file
        assert not settings.exists()

    def test_garbage_settings_warns_not_crashes(self, tmp_path, monkeypatch, capsys):
        self._settings(tmp_path, monkeypatch, "{not json")
        _refresh_delivery_hook_if_installed()
        assert "Warning" in capsys.readouterr().out
