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
