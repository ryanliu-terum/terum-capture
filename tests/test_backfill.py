"""Tests for historical Claude Code session backfill (spec §6).

Covers discovery (depth-2 glob excludes subagents; mtime window; sort + limit), cwd
extraction, end-to-end idempotency/resumability through the real process_transcript,
429 back-off, the R5 throttle, honest reporting, and the confirm-gated setup offer.
"""
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import terum_capture.backfill as backfill
import terum_capture.commands as commands
import terum_capture.upload as upload

CONFIG = {"api_key": "trm_test", "api_url": "https://example.test"}


def _resp(code):
    r = MagicMock()
    r.status_code = code
    return r


def _turn_pair(i):
    return [
        {"type": "user", "cwd": "/home/u/proj", "timestamp": f"2026-05-29T00:{i:02d}:00Z",
         "message": {"content": f"Question {i} about the system design here"}},
        {"type": "assistant", "timestamp": f"2026-05-29T00:{i:02d}:01Z",
         "message": {"content": [{"type": "text", "text": f"Answer {i} with sufficient detail."}]}},
    ]


def _write_session(base: Path, project: str, name: str, entries, *, age_days=0.0):
    proj = base / project
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{name}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    if age_days:
        ts = time.time() - age_days * 86400
        os.utime(path, (ts, ts))
    return path


# --------------------------------------------------------------------------- discovery


class TestDiscovery:
    def test_excludes_subagent_transcripts(self, tmp_path):
        base = tmp_path / "projects"
        top = _write_session(base, "projA", "sess-uuid", _turn_pair(1))
        # A subagent transcript one level deeper must NOT be returned by the depth-2 glob.
        sub = base / "projA" / "sess-uuid" / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "agent-1.jsonl").write_text("{}\n", encoding="utf-8")

        found = backfill.discover_sessions(projects_dir=base)
        assert found == [top]

    def test_mtime_window_filters_old_sessions(self, tmp_path):
        base = tmp_path / "projects"
        fresh = _write_session(base, "p", "fresh", _turn_pair(1), age_days=2)
        _write_session(base, "p", "stale", _turn_pair(2), age_days=45)

        found = backfill.discover_sessions(window_days=30, projects_dir=base)
        assert found == [fresh]

    def test_all_window_includes_old_sessions(self, tmp_path):
        base = tmp_path / "projects"
        _write_session(base, "p", "fresh", _turn_pair(1), age_days=2)
        _write_session(base, "p", "stale", _turn_pair(2), age_days=400)

        found = backfill.discover_sessions(window_days=None, projects_dir=base)
        assert len(found) == 2

    def test_sorted_newest_first_and_limit(self, tmp_path):
        base = tmp_path / "projects"
        _write_session(base, "p", "older", _turn_pair(1), age_days=10)
        newest = _write_session(base, "p", "newest", _turn_pair(2), age_days=1)
        _write_session(base, "p", "middle", _turn_pair(3), age_days=5)

        found = backfill.discover_sessions(projects_dir=base, limit=2)
        assert len(found) == 2
        assert found[0] == newest  # newest first

    def test_missing_projects_dir_returns_empty(self, tmp_path):
        assert backfill.discover_sessions(projects_dir=tmp_path / "nope") == []


class TestReadCwd:
    def test_reads_cwd_from_first_entry_that_has_one(self, tmp_path):
        # Line 1 is a summary/index entry with no cwd; line 2 carries it.
        path = _write_session(
            tmp_path, "p", "s",
            [{"type": "summary", "summary": "index"},
             {"type": "user", "cwd": "/home/u/repo", "message": {"content": "hi there friend"}}],
        )
        assert backfill._read_session_cwd(path) == "/home/u/repo"

    def test_returns_none_when_no_cwd(self, tmp_path):
        path = _write_session(tmp_path, "p", "s", [{"type": "summary", "summary": "x"}])
        assert backfill._read_session_cwd(path) is None


# ----------------------------------------------------------------- end-to-end backfill


def _run_backfill(base, terum_dir, *, post_codes, sleep=None, **kwargs):
    """Run cmd_backfill against a fake tree with the network + git mocked."""
    post = MagicMock(side_effect=[_resp(c) for c in post_codes])
    sleep = sleep or MagicMock()
    with patch.object(upload, "load_config", return_value=CONFIG), \
         patch.object(upload, "derive_repo", return_value=None), \
         patch.object(upload.httpx, "post", post), \
         patch.object(upload, "TERUM_DIR", terum_dir):
        backfill.cmd_backfill(config=CONFIG, projects_dir=base, sleep=sleep, **kwargs)
    return post, sleep


class TestBackfillEndToEnd:
    def test_uploads_then_idempotent_on_rerun(self, tmp_path, capsys):
        base = tmp_path / "projects"
        _write_session(base, "p", "s1", _turn_pair(1), age_days=1)
        _write_session(base, "p", "s2", _turn_pair(2), age_days=2)
        terum = tmp_path / "terum"

        post, _ = _run_backfill(base, terum, post_codes=(200, 200))
        assert post.call_count == 2  # one POST per session
        out = capsys.readouterr().out
        assert "Imported 2 sessions (0 already captured)." in out

        # Second run: both sidecars already at file_size -> skipped, zero new POSTs.
        post2, _ = _run_backfill(base, terum, post_codes=(200, 200))
        assert post2.call_count == 0
        out2 = capsys.readouterr().out
        assert "Imported 0 sessions (2 already captured)." in out2

    def test_resends_session_when_file_grows_past_sidecar(self, tmp_path):
        # Mirrors live/backfill cooperation: after a session is sent, new turns appended
        # later push the file past the sidecar offset, so the next run re-sends the tail.
        base = tmp_path / "projects"
        path = _write_session(base, "p", "s1", _turn_pair(1), age_days=1)
        terum = tmp_path / "terum"

        post, _ = _run_backfill(base, terum, post_codes=(200,))
        assert post.call_count == 1  # first send

        with open(path, "a", encoding="utf-8") as f:
            for e in _turn_pair(2):
                f.write(json.dumps(e) + "\n")

        post2, _ = _run_backfill(base, terum, post_codes=(200,))
        assert post2.call_count == 1  # only the appended tail re-sent

    def test_throttles_between_sessions_not_after_last(self, tmp_path):
        base = tmp_path / "projects"
        _write_session(base, "p", "s1", _turn_pair(1), age_days=1)
        _write_session(base, "p", "s2", _turn_pair(2), age_days=2)
        terum = tmp_path / "terum"
        sleep = MagicMock()

        _run_backfill(base, terum, post_codes=(200, 200), sleep=sleep)
        throttle_calls = [c for c in sleep.call_args_list if c.args == (backfill.THROTTLE_SECONDS,)]
        assert len(throttle_calls) == 1  # 2 sessions -> exactly one inter-session sleep
        assert backfill.THROTTLE_SECONDS >= 1.0  # R5: >=1s keeps us under 120 req/60s

    def test_429_then_success_retries_same_session(self, tmp_path):
        base = tmp_path / "projects"
        path = _write_session(base, "p", "s1", _turn_pair(1), age_days=1)
        terum = tmp_path / "terum"
        sleep = MagicMock()

        # First POST 429, retry POST 200.
        post, _ = _run_backfill(base, terum, post_codes=(429, 200), sleep=sleep)
        assert post.call_count == 2
        # The retried session ultimately advanced its sidecar.
        assert (terum / "sent_s1").exists()
        # Back-off slept at least once with the initial back-off interval.
        assert any(c.args == (backfill.INITIAL_BACKOFF_SECONDS,) for c in sleep.call_args_list)

    def test_persistent_429_reported_as_failed_no_advance(self, tmp_path, capsys):
        base = tmp_path / "projects"
        _write_session(base, "p", "s1", _turn_pair(1), age_days=1)
        terum = tmp_path / "terum"
        sleep = MagicMock()

        codes = tuple([429] * (backfill.MAX_BACKOFF_RETRIES + 1))
        post, _ = _run_backfill(base, terum, post_codes=codes, sleep=sleep)
        assert post.call_count == backfill.MAX_BACKOFF_RETRIES + 1
        assert not (terum / "sent_s1").exists()  # never advanced -> next run retries
        out = capsys.readouterr().out
        assert "1 session(s) could not be uploaded" in out
        assert "Imported 0 sessions" in out

    def test_server_error_counted_as_failed(self, tmp_path, capsys):
        base = tmp_path / "projects"
        _write_session(base, "p", "s1", _turn_pair(1), age_days=1)
        terum = tmp_path / "terum"

        _run_backfill(base, terum, post_codes=(500,))
        out = capsys.readouterr().out
        assert "1 session(s) could not be uploaded" in out

    def test_empty_window_message(self, tmp_path, capsys):
        base = tmp_path / "projects"
        _write_session(base, "p", "stale", _turn_pair(1), age_days=90)
        terum = tmp_path / "terum"

        _run_backfill(base, terum, post_codes=())
        out = capsys.readouterr().out
        assert "No sessions found in the last 30 days." in out

    def test_unconfigured_exits_with_message(self, tmp_path, capsys):
        backfill.cmd_backfill(config={"api_url": "x"}, projects_dir=tmp_path)
        out = capsys.readouterr().out
        assert "Run: terum-capture setup" in out

    def test_deleted_repo_cwd_does_not_crash(self, tmp_path, capsys):
        # A session whose cwd repo was deleted: derive_repo returns None (cwd fallback);
        # the run must still upload, never crash.
        base = tmp_path / "projects"
        _write_session(base, "p", "s1", _turn_pair(1), age_days=1)
        terum = tmp_path / "terum"
        post, _ = _run_backfill(base, terum, post_codes=(200,))
        assert post.call_count == 1
        events = post.call_args.kwargs["json"]["events"]
        assert all("repo" not in ev for ev in events)  # None -> field omitted


# ------------------------------------------------------------------- setup Δ4 offer


class TestSetupOffer:
    def test_non_interactive_prints_one_liner_no_prompt(self, capsys):
        with patch.object(backfill, "discover_sessions") as disc, \
             patch.object(backfill, "cmd_backfill") as run, \
             patch("builtins.input") as inp:
            commands._maybe_offer_backfill(interactive=False)
        inp.assert_not_called()
        run.assert_not_called()
        disc.assert_not_called()
        assert "terum-capture backfill" in capsys.readouterr().out

    def test_interactive_yes_runs_backfill(self):
        with patch.object(backfill, "discover_sessions", return_value=[Path("a"), Path("b")]), \
             patch.object(backfill, "cmd_backfill") as run, \
             patch("builtins.input", return_value=""):  # default yes
            commands._maybe_offer_backfill(interactive=True)
        run.assert_called_once()

    def test_interactive_no_declines(self, capsys):
        with patch.object(backfill, "discover_sessions", return_value=[Path("a")]), \
             patch.object(backfill, "cmd_backfill") as run, \
             patch("builtins.input", return_value="n"):
            commands._maybe_offer_backfill(interactive=True)
        run.assert_not_called()
        assert "anytime to import" in capsys.readouterr().out

    def test_interactive_no_sessions_skips_prompt(self):
        with patch.object(backfill, "discover_sessions", return_value=[]), \
             patch.object(backfill, "cmd_backfill") as run, \
             patch("builtins.input") as inp:
            commands._maybe_offer_backfill(interactive=True)
        inp.assert_not_called()
        run.assert_not_called()
