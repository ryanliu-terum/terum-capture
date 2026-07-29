"""Tests for the Tier 3 delivery hook: install/uninstall, the UserPromptSubmit entry point,
context formatting, and instruction dosing.

Load-bearing safety properties pinned here: FAIL-OPEN (any error prints nothing — or less —
and never raises), idempotent non-clobbering settings.json edits, prompt-lane-only install
(PreToolUse is swept out, never wired), and the dosed self-check instruction (1st prompt +
every 5th, session-keyed). Live hook I/O is not exercised (that's the E2E pass).
"""
import io
import json

import pytest

from terum_capture import commands, delivery_hooks
from terum_capture.delivery_hooks import (
    _format_context,
    _instruction_due,
    cmd_delivery,
    install_delivery_hooks,
    uninstall_delivery_hooks,
    run_prompt_hook,
    CONTEXT_MARKER,
    DELIVERY_HOOK_MARKER,
    INSTRUCTION_EVERY_N,
    REMINDER_MARKER,
    SELF_CHECK_INSTRUCTION,
    _STATE_MAX_SESSIONS,
)

CONFIG = {"api_key": "trm_x", "api_url": "https://api.terum.ai/api"}


def _set_stdin(monkeypatch, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "delivery_state.json"
    monkeypatch.setattr(delivery_hooks, "STATE_FILE", path)
    return path


class TestInstallUninstall:
    def test_installs_prompt_hook_only_and_is_idempotent(self, tmp_path, monkeypatch):
        settings = tmp_path / ".claude" / "settings.json"
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", settings)

        install_delivery_hooks()
        install_delivery_hooks()  # second call must not duplicate

        data = json.loads(settings.read_text())
        ours = [g for g in data["hooks"]["UserPromptSubmit"]
                if any(DELIVERY_HOOK_MARKER in h["command"] for h in g["hooks"])]
        assert len(ours) == 1
        assert "PreToolUse" not in data["hooks"]  # prompt-lane only — conflict lane withdrawn

    def test_install_sweeps_stale_draft_pretooluse(self, tmp_path, monkeypatch):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        stale = {"matcher": "*", "hooks": [{"command": f"x {DELIVERY_HOOK_MARKER} pretooluse"}]}
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [stale]}}))
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", settings)

        install_delivery_hooks()

        data = json.loads(settings.read_text())
        assert "PreToolUse" not in data["hooks"]
        assert "UserPromptSubmit" in data["hooks"]

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
            "UserPromptSubmit": [{"matcher": "*", "hooks": [{"command": "someone-elses-hook"}]}],
        }}))
        install_delivery_hooks()
        uninstall_delivery_hooks()

        data = json.loads(settings.read_text())
        assert data["hooks"]["Stop"] == [{"hooks": [{"command": "keep me"}]}]
        # the unrelated UserPromptSubmit hook survives; ours is gone
        groups = data["hooks"]["UserPromptSubmit"]
        assert any("someone-elses-hook" in h["command"] for g in groups for h in g["hooks"])
        assert not any(DELIVERY_HOOK_MARKER in h["command"] for g in groups for h in g["hooks"])

    def test_install_warn_dont_crash_on_bad_settings(self, tmp_path, monkeypatch, capsys):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{not json")
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", settings)

        install_delivery_hooks()  # must not raise

        assert "Warning" in capsys.readouterr().out

    def test_install_echoes_the_resolved_interpreter_path(self, tmp_path, monkeypatch, capsys):
        """bug-559: the hook command bakes in sys.executable, so a wrong interpreter (a dev
        checkout's editable venv) is frozen in and silently dies when that tree changes branch.
        The install MUST echo the resolved path — that is the only thing that makes a bad
        binding visible while the user is still standing there to notice it."""
        settings = tmp_path / ".claude" / "settings.json"
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", settings)
        monkeypatch.setattr("sys.executable", "/nowhere/dev-venv/python.exe")

        install_delivery_hooks()

        out = capsys.readouterr().out
        # The interpreter is the whole point — a bare "installed" line is what hid bug-559.
        assert "/nowhere/dev-venv/python.exe" in out
        assert "delivery-hook prompt" in out
        # And it must be the command that was actually written, not a re-render.
        written = json.loads(settings.read_text())["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        assert written["command"] in out

    def test_silent_self_heal_path_also_echoes(self, tmp_path, monkeypatch, capsys):
        """`setup-hook` / `update` re-freeze the interpreter via _refresh_delivery_hook_if_installed.
        That path is exactly where a bad interpreter gets re-written unattended, so it must report
        the command too — otherwise the self-heal can silently re-bind to the wrong Python."""
        settings = tmp_path / ".claude" / "settings.json"
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", settings)
        install_delivery_hooks()  # pre-existing install, so the refresh engages
        capsys.readouterr()
        monkeypatch.setattr("sys.executable", "/canonical/pipx/python.exe")

        commands._refresh_delivery_hook_if_installed()

        assert "/canonical/pipx/python.exe" in capsys.readouterr().out


class TestDeliveryCommandStreams:
    """`cmd_delivery` diagnostics go to stderr (bug-559) — a supervisor reads stderr, and on
    UserPromptSubmit stdout is treated as context to inject."""

    def test_usage_error_goes_to_stderr(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            cmd_delivery("bogus")

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Usage: terum-capture delivery <install|uninstall>" in captured.err
        assert captured.out == ""

    def test_unconfigured_install_goes_to_stderr(self, monkeypatch, capsys):
        monkeypatch.setattr(delivery_hooks, "load_config", lambda: None)

        with pytest.raises(SystemExit) as exc_info:
            cmd_delivery("install")

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Not configured. Run: terum-capture setup" in captured.err
        assert captured.out == ""


class TestFormatContext:
    def test_formats_summary_with_owner(self):
        out = _format_context({"results": [{"summary": "we use Redis for rate limits", "owner": "Teddy"}]})
        assert out.startswith(CONTEXT_MARKER)
        assert "Redis" in out and "(Teddy)" in out

    def test_falls_back_to_topic_without_owner(self):
        out = _format_context({"results": [{"topic": "rate limiting"}]})
        assert "- rate limiting" in out
        assert "(" not in out.splitlines()[-1]

    def test_multiline_summary_flattened_to_one_bullet_line(self):
        # Prod summaries are multi-line markdown; a bullet spanning lines breaks the strip
        # guard's block scan (found live, 2026-07-29 E2E). Every bullet must be ONE line.
        out = _format_context({"results": [{"summary": "**Topic**: nav\n\n**Summary**: article style\n- sub", "owner": "Teddy"}]})
        bullets = [l for l in out.splitlines()[1:]]
        assert len(bullets) == 1
        assert bullets[0].startswith("- ") and "article style" in bullets[0] and "(Teddy)" in bullets[0]

    def test_empty_is_none(self):
        assert _format_context({"results": []}) is None
        assert _format_context(None) is None
        assert _format_context({"results": [{"unrelated": "field"}]}) is None


class TestInstructionDosing:
    def test_first_and_every_nth_prompt(self, state_file):
        due = [_instruction_due("sess-a") for _ in range(INSTRUCTION_EVERY_N * 2)]
        expected = [i == 0 or (i + 1) % INSTRUCTION_EVERY_N == 0 for i in range(INSTRUCTION_EVERY_N * 2)]
        assert due == expected  # 1st, 5th, 10th (with N=5)

    def test_sessions_count_independently(self, state_file):
        assert _instruction_due("sess-a") is True
        assert _instruction_due("sess-b") is True  # a fresh session gets its own first-prompt dose

    def test_no_session_id_never_due(self, state_file):
        assert _instruction_due("") is False

    def test_unreadable_state_fails_open_to_not_due(self, state_file, monkeypatch):
        state_file.write_text('{"sess-a": 1}')
        monkeypatch.setattr(delivery_hooks.json, "loads", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _instruction_due("sess-a") is False  # fail-open = inject less, never crash

    def test_state_pruned_to_cap(self, state_file):
        for i in range(_STATE_MAX_SESSIONS + 5):
            _instruction_due(f"sess-{i}")
        state = json.loads(state_file.read_text())
        assert len(state) == _STATE_MAX_SESSIONS
        assert "sess-0" not in state  # oldest dropped


class TestPromptHook:
    def test_emits_context_and_first_prompt_instruction(self, state_file, monkeypatch, capsys):
        _set_stdin(monkeypatch, json.dumps({"prompt": "add rate limiting to the ingest route", "session_id": "s1"}))
        monkeypatch.setattr(delivery_hooks, "load_config", lambda: CONFIG)
        posted = {}

        class Resp:
            status_code = 200
            def json(self): return {"results": [{"summary": "use Upstash Redis", "owner": "Teddy"}]}

        def fake_post(url, json=None, headers=None, timeout=None):
            posted.update(url=url, body=json)
            return Resp()

        monkeypatch.setattr(delivery_hooks.httpx, "post", fake_post)

        run_prompt_hook()

        out = json.loads(capsys.readouterr().out)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "Upstash Redis" in ctx and "(Teddy)" in ctx
        assert SELF_CHECK_INSTRUCTION in ctx  # first prompt of the session -> instruction rides along
        assert REMINDER_MARKER in ctx
        assert posted["url"].endswith("/hooks/retrieve")
        assert posted["body"] == {"mode": "context", "text": "add rate limiting to the ingest route", "source": "hook"}

    def test_second_prompt_context_only(self, state_file, monkeypatch, capsys):
        monkeypatch.setattr(delivery_hooks, "load_config", lambda: CONFIG)

        class Resp:
            status_code = 200
            def json(self): return {"results": [{"summary": "note"}]}

        monkeypatch.setattr(delivery_hooks.httpx, "post", lambda *a, **k: Resp())

        _set_stdin(monkeypatch, json.dumps({"prompt": "long enough first prompt here", "session_id": "s2"}))
        run_prompt_hook()
        capsys.readouterr()

        _set_stdin(monkeypatch, json.dumps({"prompt": "long enough second prompt here", "session_id": "s2"}))
        run_prompt_hook()

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "note" in ctx
        assert REMINDER_MARKER not in ctx  # dosed: not due on prompt 2

    def test_short_prompt_skips_retrieval_but_still_doses(self, state_file, monkeypatch, capsys):
        _set_stdin(monkeypatch, json.dumps({"prompt": "yes", "session_id": "s3"}))
        monkeypatch.setattr(delivery_hooks, "load_config", lambda: CONFIG)

        def fail(*a, **k):
            raise AssertionError("must not POST for a sub-minimum prompt")

        monkeypatch.setattr(delivery_hooks.httpx, "post", fail)

        run_prompt_hook()

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert ctx == SELF_CHECK_INSTRUCTION  # instruction alone; no retrieval happened

    def test_no_config_prints_nothing(self, state_file, monkeypatch, capsys):
        _set_stdin(monkeypatch, json.dumps({"prompt": "a perfectly long prompt", "session_id": "s4"}))
        monkeypatch.setattr(delivery_hooks, "load_config", lambda: None)
        run_prompt_hook()
        assert capsys.readouterr().out == ""

    def test_network_error_still_emits_due_instruction(self, state_file, monkeypatch, capsys):
        _set_stdin(monkeypatch, json.dumps({"prompt": "a perfectly long prompt here", "session_id": "s5"}))
        monkeypatch.setattr(delivery_hooks, "load_config", lambda: CONFIG)

        def boom(*a, **k):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(delivery_hooks.httpx, "post", boom)

        run_prompt_hook()  # must not raise

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert ctx == SELF_CHECK_INSTRUCTION  # retrieval failed open; the dose still delivers

    def test_garbage_stdin_prints_nothing(self, state_file, monkeypatch, capsys):
        _set_stdin(monkeypatch, "{not json")
        monkeypatch.setattr(delivery_hooks, "load_config", lambda: CONFIG)
        run_prompt_hook()
        assert capsys.readouterr().out == ""
