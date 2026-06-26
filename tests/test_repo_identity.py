"""Tests for git-remote-derived repo identity (bug-294).

The capture used to let the backend derive a project key from the raw `cwd`
basename, which fragmented one repo across machines/OSes/subdirs/worktrees and
even surfaced raw filesystem paths as project names. We instead send a stable,
location-independent `repo` identity (owner/repo from the git remote) computed
here in the CLI, where git is actually available.

Three layers under test:
  - _parse_owner_repo: pure URL/SCP parsing → "owner/repo" (or None)
  - derive_repo: the git fallback chain (origin → any remote → toplevel basename)
  - _do_upload: the `repo` field is attached to every event when known
"""
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import terum_capture.upload as upload


class TestParseOwnerRepo:
    """Pure remote-URL → owner/repo normalization, including credential stripping."""

    def test_scp_ssh_form(self):
        assert upload._parse_owner_repo("git@github.com:ryanliu-terum/Terum-MVP.git") == "ryanliu-terum/Terum-MVP"

    def test_https_form_with_dotgit(self):
        assert upload._parse_owner_repo("https://github.com/ryanliu-terum/Terum-MVP.git") == "ryanliu-terum/Terum-MVP"

    def test_https_form_without_dotgit(self):
        assert upload._parse_owner_repo("https://github.com/ryanliu-terum/Terum-MVP") == "ryanliu-terum/Terum-MVP"

    def test_ssh_scheme_form(self):
        assert upload._parse_owner_repo("ssh://git@github.com/ryanliu-terum/Terum-MVP.git") == "ryanliu-terum/Terum-MVP"

    def test_credentialed_https_strips_token(self):
        url = "https://x-access-token:ghp_SUPERSECRET@github.com/ryanliu-terum/Terum-MVP.git"
        result = upload._parse_owner_repo(url)
        assert result == "ryanliu-terum/Terum-MVP"
        assert "ghp_SUPERSECRET" not in (result or "")  # token must never survive into stored data

    def test_trailing_slash_tolerated(self):
        assert upload._parse_owner_repo("https://github.com/ryanliu-terum/Terum-MVP.git/") == "ryanliu-terum/Terum-MVP"

    def test_nested_path_preserved(self):
        # GitLab-style subgroups: keep the full project path, don't truncate to last 2.
        assert upload._parse_owner_repo("git@gitlab.com:group/subgroup/repo.git") == "group/subgroup/repo"

    def test_empty_is_none(self):
        assert upload._parse_owner_repo("") is None
        assert upload._parse_owner_repo("   ") is None

    def test_garbage_is_none(self):
        assert upload._parse_owner_repo("not a remote url") is None

    def test_raw_windows_path_is_none(self):
        # The exact value bug-294 wrongly stored — a path must never parse as a repo.
        assert upload._parse_owner_repo(r"C:\dev\Terum\MVP") is None

    def test_raw_posix_path_is_none(self):
        assert upload._parse_owner_repo("/home/teniroo/Projects/terum/Terum-MVP") is None


class TestDeriveRepo:
    """The fallback chain over the _git helper."""

    def test_prefers_origin_remote(self):
        with patch.object(upload, "_git") as g:
            g.side_effect = lambda cwd, args: "git@github.com:ryanliu-terum/Terum-MVP.git" \
                if args[:3] == ["config", "--get", "remote.origin.url"] else None
            assert upload.derive_repo("/anything") == "ryanliu-terum/Terum-MVP"

    def test_falls_back_to_other_remote_when_no_origin(self):
        def fake(cwd, args):
            if args == ["config", "--get", "remote.origin.url"]:
                return None
            if args == ["remote"]:
                return "upstream\nfork"
            if args == ["config", "--get", "remote.upstream.url"]:
                return "https://github.com/acme/widgets.git"
            return None
        with patch.object(upload, "_git", side_effect=fake):
            assert upload.derive_repo("/anything") == "acme/widgets"

    def test_falls_back_to_toplevel_basename_when_no_remote(self):
        def fake(cwd, args):
            if args[:1] == ["config"]:
                return None
            if args == ["remote"]:
                return ""
            if args == ["rev-parse", "--show-toplevel"]:
                return "/home/u/Projects/Terum-MVP"
            return None
        with patch.object(upload, "_git", side_effect=fake):
            assert upload.derive_repo("/home/u/Projects/Terum-MVP/node_modules") == "Terum-MVP"

    def test_not_a_git_repo_returns_none(self):
        with patch.object(upload, "_git", return_value=None):
            assert upload.derive_repo("/tmp/whatever") is None

    def test_empty_cwd_returns_none(self):
        assert upload.derive_repo(None) is None
        assert upload.derive_repo("") is None


class TestGitHelper:
    """_git shells out as `git -C <cwd> ...`, with a timeout, and swallows failures."""

    def test_invokes_git_with_dash_C_and_timeout(self):
        fake_run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="value\n", stderr=""))
        with patch.object(upload.subprocess, "run", fake_run):
            out = upload._git("/some/dir", ["config", "--get", "remote.origin.url"])
        assert out == "value"
        called = fake_run.call_args
        assert called.args[0] == ["git", "-C", "/some/dir", "config", "--get", "remote.origin.url"]
        assert called.kwargs.get("timeout")  # a timeout must be set so a hung git can't stall the hook

    def test_nonzero_exit_returns_none(self):
        with patch.object(upload.subprocess, "run",
                          return_value=SimpleNamespace(returncode=128, stdout="", stderr="not a repo")):
            assert upload._git("/x", ["rev-parse", "--show-toplevel"]) is None

    def test_git_missing_returns_none(self):
        with patch.object(upload.subprocess, "run", side_effect=FileNotFoundError("git")):
            assert upload._git("/x", ["remote"]) is None

    def test_git_timeout_returns_none(self):
        with patch.object(upload.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 5)):
            assert upload._git("/x", ["remote"]) is None


class TestRepoAttachedToEvents:
    """The derived repo rides on every event; absent when unknown."""

    def _run(self, *, repo_return):
        entries = [
            {"type": "user", "timestamp": "2026-06-26T00:00:00Z",
             "message": {"content": "A real question with enough length here"}},
            {"type": "assistant", "timestamp": "2026-06-26T00:00:01Z",
             "message": {"content": [{"type": "text", "text": "An answer with sufficient length."}]}},
        ]
        config = {"api_key": "trm_test", "api_url": "https://example.test"}
        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, "t.jsonl")
            with open(transcript, "w") as f:
                for e in entries:
                    f.write(json.dumps(e) + "\n")
            hook_input = json.dumps(
                {"transcript_path": transcript, "session_id": "s1", "cwd": "/home/u/Terum-MVP"})
            mock_post = MagicMock(return_value=MagicMock(status_code=200))
            with patch("sys.stdin", io.StringIO(hook_input)), \
                 patch.object(upload, "load_config", return_value=config), \
                 patch.object(upload, "derive_repo", return_value=repo_return), \
                 patch.object(upload.httpx, "post", mock_post), \
                 patch.object(upload, "TERUM_DIR", Path(tmp) / "terum"):
                upload._do_upload()
            if not mock_post.called:
                return None
            return mock_post.call_args.kwargs["json"]["events"]

    def test_repo_attached_when_known(self):
        events = self._run(repo_return="ryanliu-terum/Terum-MVP")
        assert events and all(ev["repo"] == "ryanliu-terum/Terum-MVP" for ev in events)

    def test_repo_omitted_when_unknown(self):
        events = self._run(repo_return=None)
        assert events and all("repo" not in ev for ev in events)
