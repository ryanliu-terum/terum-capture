"""Tests for the Tier 3 (DRAFT) delivery hooks: install/uninstall + the two hook entry points.

Focus: the load-bearing safety property is FAIL-OPEN — any error prints nothing and never
raises — plus idempotent, non-clobbering settings.json edits. Live hook I/O is not exercised
(runtime-untested by design; this is a draft).
"""
import io
import json

import pytest

from terum_capture import commands, delivery_hooks
from terum_capture.delivery_hooks import (
    _statement_from_tool,
    _format_context,
    _format_conflict,
    install_delivery_hooks,
    uninstall_delivery_hooks,
    run_prompt_hook,
    run_pretooluse_hook,
    DELIVERY_HOOK_MARKER,
)


def _set_stdin(monkeypatch, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


class TestInstallUninstall:
    def test_installs_both_events_and_is_idempotent(self, tmp_path, monkeypatch):
        settings = tmp_path / ".claude" / "settings.json"
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", settings)

        install_delivery_hooks()
        install_delivery_hooks()  # second call must not duplicate

        data = json.loads(settings.read_text())
        for event in ("UserPromptSubmit", "PreToolUse"):
            ours = [g for g in data["hooks"][event]
                    if any(DELIVERY_HOOK_MARKER in h["command"] for h in g["hooks"])]
            assert len(ours) == 1

    def test_preserves_existing_stop_hook(self, tmp_path, monkeypatch):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "keep me"}]}]}}))
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", settings)

        install_delivery_hooks()

        data = json.loads(settings.read_text())
        assert data["hooks"]["Stop"] == [{"hooks": [{"command": "keep me"}]}]
        assert "UserPromptSubmit" in data["hooks"]

    def test_uninstall_removes_only_ours(self, tmp_path, monkeypatch):
        settings = tmp_path / ".claude" / "settings.json"
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", settings)
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"command": "keep me"}]}],
            "PreToolUse": [{"matcher": "*", "hooks": [{"command": "someone-elses-hook"}]}],
        }}))
        install_delivery_hooks()
        uninstall_delivery_hooks()

        data = json.loads(settings.read_text())
        assert data["hooks"]["Stop"] == [{"hooks": [{"command": "keep me"}]}]
        # the unrelated PreToolUse hook survives; ours is gone
        assert any("someone-elses-hook" in h["command"] for g in data["hooks"]["PreToolUse"] for h in g["hooks"])
        assert not any(DELIVERY_HOOK_MARKER in h["command"]
                       for g in data["hooks"].get("UserPromptSubmit", []) for h in g["hooks"])

    def test_install_warn_dont_crash_on_bad_settings(self, tmp_path, monkeypatch, capsys):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{not json")
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", settings)

        install_delivery_hooks()  # must not raise

        assert "Warning" in capsys.readouterr().out


class TestStatementFromTool:
    def test_bash_uses_command(self):
        assert _statement_from_tool("Bash", {"command": "rm -rf /"}) == "rm -rf /"

    def test_edit_uses_file_path(self):
        assert _statement_from_tool("Edit", {"file_path": "auth.ts"}) == "modify auth.ts"

    def test_unknown_tool_returns_empty(self):
        assert _statement_from_tool("Read", {"file_path": "x"}) == ""

    def test_non_dict_input_returns_empty(self):
        assert _statement_from_tool("Bash", "not-a-dict") == ""


class TestFormatters:
    def test_context_formats_results(self):
        out = _format_context({"results": [{"summary": "we use Redis for rate limits"}]})
        assert "Redis" in out and out.startswith("Relevant team context")

    def test_context_empty_is_none(self):
        assert _format_context({"results": []}) is None
        assert _format_context(None) is None

    def test_conflict_formats_candidates(self):
        out = _format_conflict({"candidates": [{"decision": "no in-memory rate limiters"}]})
        assert "in-memory" in out

    def test_conflict_empty_is_none(self):
        assert _format_conflict({"candidates": []}) is None


class TestPromptHookFailOpen:
    def test_emits_context_on_success(self, tmp_path, monkeypatch, capsys):
        _set_stdin(monkeypatch, json.dumps({"user_prompt": "add rate limiting"}))
        monkeypatch.setattr(delivery_hooks, "load_config",
                            lambda: {"api_key": "trm_x", "api_url": "https://api.terum.ai/api"})
        posted = {}

        class Resp:
            status_code = 200
            def json(self): return {"results": [{"summary": "use Upstash Redis"}]}

        def fake_post(url, json=None, headers=None, timeout=None):
            posted.update(url=url, body=json)
            return Resp()

        monkeypatch.setattr(delivery_hooks.httpx, "post", fake_post)

        run_prompt_hook()

        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "Upstash Redis" in out["hookSpecificOutput"]["additionalContext"]
        assert posted["url"].endswith("/hooks/retrieve")
        assert posted["body"] == {"mode": "context", "text": "add rate limiting"}

    def test_no_config_prints_nothing(self, monkeypatch, capsys):
        _set_stdin(monkeypatch, json.dumps({"user_prompt": "hi"}))
        monkeypatch.setattr(delivery_hooks, "load_config", lambda: None)
        run_prompt_hook()
        assert capsys.readouterr().out == ""

    def test_network_error_prints_nothing(self, monkeypatch, capsys):
        _set_stdin(monkeypatch, json.dumps({"user_prompt": "hi"}))
        monkeypatch.setattr(delivery_hooks, "load_config",
                            lambda: {"api_key": "trm_x", "api_url": "https://api.terum.ai/api"})

        def boom(*a, **k):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(delivery_hooks.httpx, "post", boom)
        run_prompt_hook()  # must not raise
        assert capsys.readouterr().out == ""

    def test_garbage_stdin_prints_nothing(self, monkeypatch, capsys):
        _set_stdin(monkeypatch, "{not json")
        run_prompt_hook()
        assert capsys.readouterr().out == ""


class TestPreToolUseHook:
    def test_unknown_tool_skips_retrieval(self, monkeypatch, capsys):
        _set_stdin(monkeypatch, json.dumps({"tool_name": "Read", "tool_input": {"file_path": "x"}}))

        def fail(*a, **k):
            raise AssertionError("must not POST for an unknown tool")

        monkeypatch.setattr(delivery_hooks.httpx, "post", fail)
        run_pretooluse_hook()
        assert capsys.readouterr().out == ""

    def test_bash_conflict_emits_warning(self, monkeypatch, capsys):
        _set_stdin(monkeypatch, json.dumps({"tool_name": "Bash", "tool_input": {"command": "drop table"}}))
        monkeypatch.setattr(delivery_hooks, "load_config",
                            lambda: {"api_key": "trm_x", "api_url": "https://api.terum.ai/api"})

        class Resp:
            status_code = 200
            def json(self): return {"candidates": [{"decision": "never drop prod tables"}]}

        monkeypatch.setattr(delivery_hooks.httpx, "post", lambda *a, **k: Resp())
        run_pretooluse_hook()

        out = json.loads(capsys.readouterr().out)
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert "never drop prod tables" in out["hookSpecificOutput"]["additionalContext"]
