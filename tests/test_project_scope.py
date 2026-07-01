"""Tests for the multi-project setup scope: discovery, selection parsing, and install.

Covers the pieces that make `setup` let a user choose WHICH project(s) to capture:
  - backfill.discover_projects (resolves each ~/.claude/projects/<enc>/ dir to its real cwd);
  - commands._parse_selection (the picker answer grammar);
  - commands._install_scope (writes hooks to N project dirs, or global);
  - commands._prompt_project_scope (the interactive picker end-to-end).
"""
import json
import os
from pathlib import Path

import pytest

import terum_capture.backfill as backfill
import terum_capture.commands as commands


def _make_project(projects_dir: Path, encoded: str, cwd: Path, mtime: float, n: int = 1) -> None:
    """Create ~/.claude/projects/<encoded>/ with n transcripts whose entries record `cwd`."""
    d = projects_dir / encoded
    d.mkdir(parents=True)
    for i in range(n):
        f = d / f"sess{i}.jsonl"
        f.write_text(json.dumps({"type": "user", "cwd": str(cwd), "message": {"content": "hi"}}) + "\n")
        os.utime(f, (mtime, mtime))


class TestDiscoverProjects:
    def test_resolves_cwd_and_orders_by_recency(self, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        repo_a = tmp_path / "repoA"; repo_a.mkdir()
        repo_b = tmp_path / "repoB"; repo_b.mkdir()
        _make_project(projects, "-enc-a", repo_a, mtime=100.0, n=2)
        _make_project(projects, "-enc-b", repo_b, mtime=200.0, n=1)

        res = backfill.discover_projects(projects_dir=projects)
        assert [e["path"] for e in res] == [repo_b, repo_a]  # newest first
        assert res[0]["sessions"] == 1
        assert res[1]["sessions"] == 2

    def test_drops_projects_whose_cwd_no_longer_exists(self, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        gone = tmp_path / "deleted-repo"  # never created on disk
        _make_project(projects, "-enc", gone, mtime=100.0)
        assert backfill.discover_projects(projects_dir=projects) == []

    def test_dedupes_same_cwd_from_multiple_encoded_dirs(self, tmp_path):
        projects = tmp_path / "projects"
        projects.mkdir()
        repo = tmp_path / "repo"; repo.mkdir()
        _make_project(projects, "-enc-1", repo, mtime=100.0, n=1)
        _make_project(projects, "-enc-2", repo, mtime=300.0, n=2)
        res = backfill.discover_projects(projects_dir=projects)
        assert len(res) == 1
        assert res[0]["path"] == repo
        assert res[0]["sessions"] == 3  # folded
        assert res[0]["mtime"] == 300.0  # newest wins

    def test_falls_back_to_older_transcript_when_newest_lacks_cwd(self, tmp_path):
        # The newest transcript is summary-only (no cwd); an older one carries it. The
        # project must still be discovered — not silently dropped.
        projects = tmp_path / "projects"
        projects.mkdir()
        repo = tmp_path / "repo"; repo.mkdir()
        d = projects / "-enc"; d.mkdir()
        old = d / "old.jsonl"
        old.write_text(json.dumps({"type": "user", "cwd": str(repo), "message": {"content": "hi"}}) + "\n")
        os.utime(old, (100.0, 100.0))
        new = d / "new.jsonl"
        new.write_text(json.dumps({"type": "summary", "summary": "x"}) + "\n")  # no cwd
        os.utime(new, (200.0, 200.0))

        res = backfill.discover_projects(projects_dir=projects)
        assert len(res) == 1
        assert res[0]["path"] == repo
        assert res[0]["mtime"] == 200.0  # recency tracks the newest transcript

    def test_empty_when_projects_dir_absent(self, tmp_path):
        assert backfill.discover_projects(projects_dir=tmp_path / "nope") == []

    def test_ignores_dirs_without_transcripts(self, tmp_path):
        projects = tmp_path / "projects"
        (projects / "-empty").mkdir(parents=True)
        assert backfill.discover_projects(projects_dir=projects) == []


class TestParseSelection:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", ("pick", [1])),
            ("g", ("global", [])),
            ("global", ("global", [])),
            ("a", ("all", [1, 2, 3])),
            ("all", ("all", [1, 2, 3])),
            ("1,3", ("pick", [1, 3])),
            ("1-3", ("pick", [1, 2, 3])),
            ("2 3", ("pick", [2, 3])),
            ("1,1,2", ("pick", [1, 2])),   # dedupe, preserve order
            ("9", ("invalid", [])),        # out of range, but a token was given
            ("xyz", ("invalid", [])),      # garbage
            ("0", ("invalid", [])),        # 1-based; 0 is out of range
        ],
    )
    def test_grammar(self, raw, expected):
        assert commands._parse_selection(raw, 3) == expected

    def test_empty_with_no_rows_is_none(self):
        assert commands._parse_selection("", 0) == ("none", [])


class TestInstallScope:
    def test_global_writes_home_targets(self, tmp_path, monkeypatch):
        gs = tmp_path / "settings.json"
        gm = tmp_path / "CLAUDE.md"
        monkeypatch.setattr(commands, "CLAUDE_SETTINGS", gs)
        monkeypatch.setattr(commands, "CLAUDE_MD", gm)
        out = commands._install_scope(True, None)
        assert out[0]["global"] is True
        assert gs.exists() and gm.exists()

    def test_multiple_projects_deduped(self, tmp_path):
        a = tmp_path / "a"; a.mkdir(); (a / ".git").mkdir()
        b = tmp_path / "b"; b.mkdir()  # not a git repo
        out = commands._install_scope(False, [a, b, a])  # `a` duplicated
        assert len(out) == 2
        assert (a / ".claude" / "settings.local.json").exists()
        assert (b / ".claude" / "settings.local.json").exists()
        assert (a / "CLAUDE.local.md").exists()
        # gitignore only inside the git repo.
        assert (a / ".gitignore").exists()
        assert not (b / ".gitignore").exists()
        assert [t["gitignored"] for t in out] == [True, False]

    def test_skips_nonexistent_base_without_creating_a_tree(self, tmp_path, capsys):
        ghost = tmp_path / "ghost"  # does not exist
        out = commands._install_scope(False, [ghost])
        assert out == []
        assert not (ghost / ".claude").exists()  # no stray tree from a typo'd path
        assert "not a directory" in capsys.readouterr().out

    def test_relative_project_from_repo_subdir_still_gitignores(self, tmp_path, monkeypatch):
        # `setup --project .` from a monorepo subdir must still write .gitignore (the repo's
        # .git is an ancestor, not at cwd) — otherwise the personal files stay committable.
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        sub = repo / "packages" / "app"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)

        out = commands._install_scope(False, [Path(".")])
        assert len(out) == 1
        assert out[0]["base"].is_absolute()  # relative arg was absolutized
        assert (sub / ".claude" / "settings.local.json").exists()
        assert (sub / ".gitignore").exists()
        assert out[0]["gitignored"] is True


class TestPromptProjectScope:
    def test_number_selects_a_discovered_project(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"; proj.mkdir()
        other = tmp_path / "other"; other.mkdir()
        monkeypatch.setattr(
            "terum_capture.backfill.discover_projects",
            lambda **k: [{"path": other, "mtime": 100.0, "sessions": 3}],
        )
        monkeypatch.setattr("builtins.input", lambda *_: "2")
        use_global, bases = commands._prompt_project_scope(proj)
        # Row 1 is always cwd; row 2 is the discovered `other`.
        assert use_global is False
        assert bases == [other]

    def test_enter_defaults_to_current_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("terum_capture.backfill.discover_projects", lambda **k: [])
        monkeypatch.setattr("builtins.input", lambda *_: "")
        use_global, bases = commands._prompt_project_scope(tmp_path)
        assert use_global is False
        assert bases == [tmp_path]

    def test_all_selects_every_listed_row(self, tmp_path, monkeypatch):
        other = tmp_path / "other"; other.mkdir()
        monkeypatch.setattr(
            "terum_capture.backfill.discover_projects",
            lambda **k: [{"path": other, "mtime": 100.0, "sessions": 3}],
        )
        monkeypatch.setattr("builtins.input", lambda *_: "a")
        use_global, bases = commands._prompt_project_scope(tmp_path)
        assert set(bases) == {tmp_path, other}

    def test_g_returns_global(self, tmp_path, monkeypatch):
        monkeypatch.setattr("terum_capture.backfill.discover_projects", lambda **k: [])
        monkeypatch.setattr("builtins.input", lambda *_: "g")
        use_global, bases = commands._prompt_project_scope(tmp_path)
        assert use_global is True

    def test_reprompts_until_valid(self, tmp_path, monkeypatch):
        monkeypatch.setattr("terum_capture.backfill.discover_projects", lambda **k: [])
        answers = iter(["nonsense", "1"])
        monkeypatch.setattr("builtins.input", lambda *_: next(answers))
        use_global, bases = commands._prompt_project_scope(tmp_path)
        assert bases == [tmp_path]

    def test_eof_falls_back_to_current_dir(self, tmp_path, monkeypatch):
        # Ctrl-D / closed stdin at the prompt must not crash after the key was created.
        monkeypatch.setattr("terum_capture.backfill.discover_projects", lambda **k: [])

        def _eof(*_):
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        use_global, bases = commands._prompt_project_scope(tmp_path)
        assert use_global is False
        assert bases == [tmp_path]

    def test_current_dir_not_duplicated_when_also_discovered(self, tmp_path, monkeypatch):
        # If cwd already has sessions, it must appear once (row 1), not twice.
        monkeypatch.setattr(
            "terum_capture.backfill.discover_projects",
            lambda **k: [{"path": tmp_path, "mtime": 500.0, "sessions": 7}],
        )
        monkeypatch.setattr("builtins.input", lambda *_: "a")
        use_global, bases = commands._prompt_project_scope(tmp_path)
        assert bases == [tmp_path]  # deduped to a single row
