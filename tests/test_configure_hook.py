"""Tests for Stop-hook install/uninstall in settings.json.

These guard:
  - the matcher-group shape Claude Code requires for Stop hooks ({"hooks": [{...}]});
  - migration of older forms — the legacy flat shape ({"type", "command", "timeout"})
    and the legacy bare `terum-capture upload` shim command — to the current form;
  - routing the hook through the signed Python interpreter (`python -m terum_capture
    upload`) instead of the unsigned console-script .exe that Windows Smart App
    Control / WDAC block on enforcing machines (which silently killed capture);
  - project- vs global-scope target resolution and the git-ignore guard that keeps a
    project-local hook/note out of version control.
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
def settings_path(tmp_path):
    return tmp_path / "settings.json"


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


class TestScopeTargets:
    def test_project_scope_resolves_git_ignored_local_files(self, tmp_path):
        settings, claude_md = commands._scope_targets(False, base=tmp_path)
        assert settings == tmp_path / ".claude" / "settings.local.json"
        assert claude_md == tmp_path / "CLAUDE.local.md"

    def test_global_scope_resolves_home_files(self):
        settings, claude_md = commands._scope_targets(True)
        assert settings == commands.CLAUDE_SETTINGS
        assert claude_md == commands.CLAUDE_MD
        # Global scope keeps the committed/shared filenames, not the .local variants.
        assert settings.name == "settings.json"
        assert claude_md.name == "CLAUDE.md"


class TestConfigureHook:
    def test_fresh_install_writes_python_routed_group_shape(self, settings_path):
        commands._configure_hook(settings_path)
        assert _read(settings_path)["hooks"]["Stop"] == [_group()]

    def test_creates_parent_directory(self, tmp_path):
        # Project scope points at <cwd>/.claude/settings.local.json — the .claude dir
        # may not exist yet; _configure_hook must create it.
        nested = tmp_path / ".claude" / "settings.local.json"
        commands._configure_hook(nested)
        assert nested.exists()
        assert _read(nested)["hooks"]["Stop"] == [_group()]

    def test_migrates_legacy_flat_entry_to_python_form(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [LEGACY_FLAT]}})
        commands._configure_hook(settings_path)
        # Migrated to the group shape AND the python-routed command, not appended.
        assert _read(settings_path)["hooks"]["Stop"] == [_group()]

    def test_migrates_legacy_group_command_to_python_form(self, settings_path):
        # Right shape already, but the old bare-shim command — refresh it in place.
        _write(settings_path, {"hooks": {"Stop": [LEGACY_GROUP]}})
        commands._configure_hook(settings_path)
        assert _read(settings_path)["hooks"]["Stop"] == [_group()]

    def test_idempotent_when_already_correct(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [_group()]}})
        commands._configure_hook(settings_path)
        assert _read(settings_path)["hooks"]["Stop"] == [_group()]

    def test_collapses_duplicate_entries(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [LEGACY_FLAT, LEGACY_FLAT]}})
        commands._configure_hook(settings_path)
        assert _read(settings_path)["hooks"]["Stop"] == [_group()]

    def test_preserves_unrelated_stop_hook(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [OTHER]}})
        commands._configure_hook(settings_path)
        assert _read(settings_path)["hooks"]["Stop"] == [OTHER, _group()]

    def test_preserves_other_settings_keys(self, settings_path):
        _write(settings_path, {"model": "opus", "hooks": {"Stop": []}})
        commands._configure_hook(settings_path)
        out = _read(settings_path)
        assert out["model"] == "opus"
        assert out["hooks"]["Stop"] == [_group()]


class TestAppendClaudeMd:
    def test_appends_block_creating_parent_dir(self, tmp_path):
        md = tmp_path / "nested" / "CLAUDE.local.md"
        commands._append_claude_md(md)
        assert "## Terum Knowledge Capture" in md.read_text()

    def test_idempotent(self, tmp_path):
        md = tmp_path / "CLAUDE.local.md"
        commands._append_claude_md(md)
        commands._append_claude_md(md)
        assert md.read_text().count("## Terum Knowledge Capture") == 1

    def test_preserves_existing_content(self, tmp_path):
        md = tmp_path / "CLAUDE.local.md"
        md.write_text("# My project notes\n")
        commands._append_claude_md(md)
        content = md.read_text()
        assert "# My project notes" in content
        assert "## Terum Knowledge Capture" in content


class TestEnsureGitignore:
    def test_adds_entries_inside_git_repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert commands._ensure_gitignore(tmp_path) is True
        content = (tmp_path / ".gitignore").read_text()
        assert ".claude/settings.local.json" in content
        assert "CLAUDE.local.md" in content

    def test_skips_when_not_a_git_repo(self, tmp_path):
        # No .git anywhere — never litter a non-repo directory with a .gitignore.
        assert commands._ensure_gitignore(tmp_path) is False
        assert not (tmp_path / ".gitignore").exists()

    def test_idempotent(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert commands._ensure_gitignore(tmp_path) is True
        assert commands._ensure_gitignore(tmp_path) is False  # nothing new to add
        # Entries appear exactly once.
        content = (tmp_path / ".gitignore").read_text()
        assert content.count("CLAUDE.local.md") == 1

    def test_preserves_existing_gitignore(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".gitignore").write_text("node_modules/")  # no trailing newline
        commands._ensure_gitignore(tmp_path)
        content = (tmp_path / ".gitignore").read_text()
        assert "node_modules/" in content
        assert ".claude/settings.local.json" in content
        # The pre-existing entry must survive on its own line, not get glued to ours.
        assert "node_modules/.claude" not in content

    def test_detects_repo_from_a_subdirectory(self, tmp_path):
        # setup run from a subdir of a repo still ignores the files it writes there.
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "packages" / "app"
        sub.mkdir(parents=True)
        assert commands._ensure_gitignore(sub) is True
        assert (sub / ".gitignore").exists()

    def test_treats_git_file_as_a_repo(self, tmp_path):
        # Worktrees and submodules use a `.git` FILE, not a directory.
        (tmp_path / ".git").write_text("gitdir: /somewhere/else\n")
        assert commands._ensure_gitignore(tmp_path) is True

    def test_does_not_false_skip_on_a_substring_mention(self, tmp_path):
        # A comment / different subpath merely MENTIONING the filename must not be mistaken
        # for an effective ignore rule — otherwise the file stays committable.
        (tmp_path / ".git").mkdir()
        (tmp_path / ".gitignore").write_text(
            "# TODO: ignore CLAUDE.local.md later\ndocs/CLAUDE.local.md\n"
        )
        assert commands._ensure_gitignore(tmp_path) is True
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        # A real, effective (unanchored) rule is now present as its own line.
        assert "CLAUDE.local.md" in lines
        assert ".claude/settings.local.json" in lines

    def test_recognizes_anchored_existing_rule(self, tmp_path):
        # `/CLAUDE.local.md` and the bare entry already effectively ignore the files.
        (tmp_path / ".git").mkdir()
        (tmp_path / ".gitignore").write_text("/CLAUDE.local.md\n.claude/settings.local.json\n")
        assert commands._ensure_gitignore(tmp_path) is False


class TestGlobalHookPresent:
    @pytest.fixture
    def global_settings(self, tmp_path, monkeypatch):
        path = tmp_path / "global-settings.json"
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", path)
        return path

    def test_false_when_no_global_settings(self, global_settings):
        assert commands._global_hook_present() is False

    def test_false_when_only_unrelated_hook(self, global_settings):
        _write(global_settings, {"hooks": {"Stop": [OTHER]}})
        assert commands._global_hook_present() is False

    def test_true_when_global_terum_hook_installed(self, global_settings):
        _write(global_settings, {"hooks": {"Stop": [_group()]}})
        assert commands._global_hook_present() is True

    def test_true_for_legacy_global_hook(self, global_settings):
        _write(global_settings, {"hooks": {"Stop": [LEGACY_FLAT]}})
        assert commands._global_hook_present() is True

    @pytest.mark.parametrize("blob", ["null", "[]", '"x"', "123", '{"hooks": null}', '{"hooks": [1, 2]}'])
    def test_never_crashes_on_malformed_settings(self, global_settings, blob):
        # A hand-edited settings.json whose top level or `hooks` is not an object must not
        # raise out of setup — it just means "no terum hook here".
        global_settings.write_text(blob)
        assert commands._global_hook_present() is False


class TestSettingsHasOurHook:
    def test_missing_file_is_false(self, tmp_path):
        assert commands._settings_has_our_hook(tmp_path / "nope.json") is False

    def test_detects_hook_at_arbitrary_path(self, tmp_path):
        path = tmp_path / "settings.local.json"
        _write(path, {"hooks": {"Stop": [_group()]}})
        assert commands._settings_has_our_hook(path) is True


class TestRemoveHook:
    def test_removes_python_form(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [_group()]}})
        assert commands._remove_hook(settings_path) is True
        assert "hooks" not in _read(settings_path)

    def test_removes_legacy_flat_shape_keeping_others(self, settings_path):
        _write(settings_path, {"hooks": {"Stop": [LEGACY_FLAT, OTHER]}})
        assert commands._remove_hook(settings_path) is True
        assert _read(settings_path)["hooks"]["Stop"] == [OTHER]

    def test_returns_false_when_no_terum_hook_present(self, settings_path):
        # Drives the logout message: "No Terum hook was found at …".
        _write(settings_path, {"hooks": {"Stop": [OTHER]}})
        assert commands._remove_hook(settings_path) is False
        assert _read(settings_path)["hooks"]["Stop"] == [OTHER]

    def test_noop_when_settings_absent(self, settings_path):
        # No file written; should not raise or create one.
        assert commands._remove_hook(settings_path) is False
        assert not settings_path.exists()
