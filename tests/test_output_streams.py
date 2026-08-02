"""The stdout/stderr contract (bug-559).

`die` is the single funnel for "we cannot proceed" messages. It exists because a diagnostic on
stdout is invisible to a supervisor: Claude Code reported our failing hook as "Failed with
non-blocking status code: No stderr output" while `Unknown command: delivery-hook` sat unread on
stdout, and every prompt errored for days with in-flow delivery never firing.

The sweep boundary is asserted here too: reporting commands keep their output on stdout even when
the news is bad, because that output IS the answer the user asked for.
"""
import pytest

from terum_capture import commands, updater
from terum_capture.output import die


class TestDie:
    def test_writes_to_stderr_and_exits_1(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            die("boom")

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.err == "boom\n"
        assert captured.out == ""  # the whole point — nothing on stdout

    def test_writes_every_line_in_order(self, capsys):
        with pytest.raises(SystemExit):
            die("first", "second")

        assert capsys.readouterr().err == "first\nsecond\n"

    def test_exit_code_is_overridable(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            die("nope", code=2)

        assert exc_info.value.code == 2


class TestReportingCommandsKeepStdout:
    """The deliberate exception. `status` is a REPORTING command: "invalid or revoked" is the
    answer to the question, not a usage error, and it belongs on stdout next to the Key:/API:
    lines it is part of. Routing it to stderr would tear one report across two streams. The
    non-zero exit is the machine-readable signal; the text stays where a human piping to a file
    expects it."""

    def test_status_reports_bad_state_on_stdout(self, monkeypatch, capsys):
        monkeypatch.setattr(
            commands, "load_config",
            lambda: {"api_key": "trm_abcdefgh", "api_url": "https://api.terum.ai/api"},
        )

        class Resp:
            status_code = 401

        monkeypatch.setattr(commands.httpx, "get", lambda *a, **k: Resp())

        with pytest.raises(SystemExit) as exc_info:
            commands.cmd_status()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Status: invalid or revoked (HTTP 401)" in captured.out
        assert captured.err == ""

    def test_status_reports_unreachable_on_stdout(self, monkeypatch, capsys):
        monkeypatch.setattr(
            commands, "load_config",
            lambda: {"api_key": "trm_abcdefgh", "api_url": "https://api.terum.ai/api"},
        )

        def boom(*a, **k):
            raise RuntimeError("dns died")

        monkeypatch.setattr(commands.httpx, "get", boom)

        with pytest.raises(SystemExit):
            commands.cmd_status()

        captured = capsys.readouterr()
        assert "Status: unreachable" in captured.out
        assert captured.err == ""


class TestSetupFailuresExitNonZero:
    """bug-561: every terminal failure in `cmd_setup` used a bare `return`, so it printed
    `Error: ...` to stdout and exited **0** — `setup && next-step` ran next-step after onboarding
    had failed, and two of these paths had already deleted the config. Both the exit code AND the
    stream are pinned, because fixing only the stream would leave the worse half of the bug."""

    @staticmethod
    def _resp(status):
        class Resp:
            status_code = status

            @staticmethod
            def json():
                return {"key": "trm_newkey"}
        return Resp()

    @pytest.fixture(autouse=True)
    def _no_existing_key(self, monkeypatch):
        monkeypatch.setattr(commands, "load_config", lambda: None)

    @pytest.mark.parametrize("status,fragment", [
        (409, "10 active keys"),
        (401, "Token expired or invalid"),
        (500, "Key creation failed (HTTP 500)"),
    ])
    def test_key_creation_failure_exits_1_on_stderr(self, monkeypatch, capsys, status, fragment):
        monkeypatch.setattr(commands.httpx, "post", lambda *a, **k: self._resp(status))

        with pytest.raises(SystemExit) as exc_info:
            commands.cmd_setup(token="tok")

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert fragment in captured.err
        assert captured.out == ""

    def test_unreachable_api_exits_1_on_stderr(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise RuntimeError("dns died")

        monkeypatch.setattr(commands.httpx, "post", boom)

        with pytest.raises(SystemExit) as exc_info:
            commands.cmd_setup(token="tok")

        assert exc_info.value.code == 1
        assert "Could not reach" in capsys.readouterr().err

    def test_roundtrip_verify_failure_deletes_config_and_exits_1(self, monkeypatch, capsys):
        """The worst of the seven: config already deleted, yet the shell used to see success."""
        deleted = {}
        monkeypatch.setattr(commands.httpx, "post", lambda *a, **k: self._resp(201))
        monkeypatch.setattr(commands.httpx, "get", lambda *a, **k: self._resp(403))
        monkeypatch.setattr(commands, "save_config", lambda *a, **k: None)
        monkeypatch.setattr(commands, "delete_config", lambda: deleted.setdefault("yes", True))

        with pytest.raises(SystemExit) as exc_info:
            commands.cmd_setup(token="tok")

        assert exc_info.value.code == 1
        assert deleted.get("yes") is True
        captured = capsys.readouterr()
        assert "Round-trip verification failed" in captured.err
        # die() raises SystemExit, which is NOT an Exception — so the surrounding `except
        # Exception` must not catch it and re-run delete_config / re-print the message.
        assert captured.err.count("Round-trip verification failed") == 1

    def test_failed_browser_auth_exits_1_without_a_second_message(self, monkeypatch, capsys):
        """_browser_auth already reported the specific reason (to stderr, via err()), so cmd_setup
        must exit non-zero WITHOUT adding a redundant line."""
        monkeypatch.setattr(commands, "_browser_auth", lambda api_url: None)

        with pytest.raises(SystemExit) as exc_info:
            commands.cmd_setup()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_declining_the_overwrite_prompt_is_NOT_a_failure(self, monkeypatch, capsys):
        """Answering N to "Continue?" is a user cancellation, not an error: it must stay exit 0.
        Guards against an over-eager sweep converting every `return` in cmd_setup to die()."""
        monkeypatch.setattr(
            commands, "load_config",
            lambda: {"api_key": "trm_existing", "api_url": "https://api.terum.ai/api"},
        )
        monkeypatch.setattr(commands.httpx, "get", lambda *a, **k: self._resp(200))
        monkeypatch.setattr("builtins.input", lambda *a: "n")
        monkeypatch.setattr(commands.httpx, "post", lambda *a, **k: pytest.fail("must not POST"))

        commands.cmd_setup(token="tok")  # must NOT raise SystemExit

        assert "already have a valid Terum key" in capsys.readouterr().out


class TestConfigureMcpUnknownClient:
    def test_reason_on_stderr_and_still_returns_failed_sentinel(self, capsys):
        """The caller turns "failed" into die(), so the exit code was already right — but the
        SPECIFIC reason was on stdout while the caller's generic message went to stderr, splitting
        one failure across two streams. err() fixes the stream without stealing the caller's
        control flow (this function's contract is to return a sentinel and never raise)."""
        assert commands._configure_mcp("trm_k", "https://api.terum.ai/api", client="vscode") == "failed"

        captured = capsys.readouterr()
        assert "unknown MCP client 'vscode'" in captured.err
        assert captured.out == ""


class TestBrowserAuthReasonsGoToStderr:
    """These are the reasons behind cmd_setup's messageless exit-1 auth path, so they are the only
    thing telling the user WHY onboarding failed. On stdout they were invisible to any supervisor."""

    @pytest.mark.parametrize("result,fragment", [
        (None, "Setup timed out"),
        ({"state": "wrong-state", "token": "t"}, "State mismatch"),
        ({"state": "expected", "token": ""}, "No token received"),
    ])
    def test_each_failure_reason_on_stderr(self, monkeypatch, capsys, result, fragment):
        from terum_capture import config as config_mod

        server = config_mod.CallbackServer()
        # Explicit, not inherited: this class attribute is what leaks between tests.
        monkeypatch.setattr(config_mod._CallbackHandler, "result", result)
        server._thread = type("T", (), {"join": lambda self, timeout=None: None})()
        server._httpd = type("H", (), {"shutdown": lambda self: None})()

        assert server.wait_for_callback("expected") is None
        captured = capsys.readouterr()
        assert fragment in captured.err
        assert captured.out == ""


class TestUpdaterErrorsGoToStderr:
    def test_missing_installer_reports_on_stderr(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise FileNotFoundError

        monkeypatch.setattr(updater.subprocess, "run", boom)

        with pytest.raises(SystemExit) as exc_info:
            updater.cmd_update()

        assert exc_info.value.code == 1
        assert "Is it installed and on your PATH?" in capsys.readouterr().err

    def test_nonzero_reinstall_reports_on_stderr(self, monkeypatch, capsys):
        class Result:
            returncode = 3

        monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: Result())

        with pytest.raises(SystemExit) as exc_info:
            updater.cmd_update()

        assert exc_info.value.code == 1
        assert "reinstall failed (exit 3)" in capsys.readouterr().err
