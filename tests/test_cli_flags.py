"""Guards the CLI arg wiring for the project-vs-global install scope.

`setup`/`logout` import their command functions lazily inside `main()`, so patching the
attribute on `terum_capture.commands` is what the dispatch actually resolves at call time.
"""
import terum_capture.cli as cli
import terum_capture.commands as commands


def _run(monkeypatch, argv: list[str], fn_name: str) -> dict:
    captured: dict = {}

    def fake(*args, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(commands, fn_name, fake)
    monkeypatch.setattr("sys.argv", ["terum-capture", *argv])
    cli.main()
    return captured


class TestSetupScope:
    def test_defaults_to_project_scope(self, monkeypatch):
        captured = _run(monkeypatch, ["setup"], "cmd_setup")
        assert captured["use_global"] is False

    def test_global_flag_opts_into_machine_wide(self, monkeypatch):
        captured = _run(monkeypatch, ["setup", "--global"], "cmd_setup")
        assert captured["use_global"] is True

    def test_global_flag_composes_with_other_args(self, monkeypatch):
        captured = _run(
            monkeypatch, ["setup", "--url", "http://x/api", "--global", "--token", "t"], "cmd_setup"
        )
        assert captured["use_global"] is True
        assert captured["api_url"] == "http://x/api"
        assert captured["token"] == "t"

    def test_no_project_flag_passes_none(self, monkeypatch):
        captured = _run(monkeypatch, ["setup"], "cmd_setup")
        assert captured["projects"] is None

    def test_project_flag_is_repeatable(self, monkeypatch):
        captured = _run(
            monkeypatch, ["setup", "--project", "/a", "--project", "/b"], "cmd_setup"
        )
        assert captured["projects"] == ["/a", "/b"]


class TestLogoutScope:
    def test_defaults_to_project_scope(self, monkeypatch):
        captured = _run(monkeypatch, ["logout"], "cmd_logout")
        assert captured["use_global"] is False

    def test_global_flag_removes_machine_wide_hook(self, monkeypatch):
        captured = _run(monkeypatch, ["logout", "--global"], "cmd_logout")
        assert captured["use_global"] is True

    def test_project_flag_targets_a_specific_repo(self, monkeypatch):
        captured = _run(monkeypatch, ["logout", "--project", "/x"], "cmd_logout")
        assert captured["project"] == "/x"
        assert captured["use_global"] is False
