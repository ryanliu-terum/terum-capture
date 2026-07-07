"""Regression tests for bug-416: durable, crash-safe resume of the upload path.

The bug: a big session's FIRST upload does expensive pre-POST work (whole-file
reads + git spawns) and only records progress AFTER a 2xx. If the process is
killed before the marker is written, the retry starts from offset 0 and re-pays
the same expensive, kill-prone work forever — the session never captures.

The Option-B ("full durability") fix, re-ported onto the shared process_transcript:
  1. The resolved repo is cached in the sidecar BEFORE the POST, so a killed retry
     does not re-run git (the offset-0 loss loop no longer re-pays the expensive work).
  2. Entries are read ONCE and token totals summed from them (was: whole-file token
     scan + a second read), and a token baseline is persisted so an incremental resume
     stays correct without re-scanning the whole file.
  3. A failed/killed upload never advances the offset and re-sends every un-confirmed
     turn on retry (anti-data-loss).
  4. Legacy bare-integer sidecars (pre-fix installs) still resume correctly.

These drive the real process_transcript, mocking only its I/O edges, and share ONE
TERUM_DIR + transcript across runs so a "kill then retry" is faithfully modeled.
"""
import contextlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import terum_capture.upload as upload


class _Harness:
    """A persistent transcript + sidecar dir that survives across multiple runs."""

    def __init__(self, tmp, session_id="sess-416", cwd="/home/u/proj"):
        self.session_id = session_id
        self.cwd = cwd
        self.transcript = os.path.join(tmp, "transcript.jsonl")
        self.terum_dir = Path(tmp) / "terum"

    @property
    def sidecar(self):
        return self.terum_dir / f"sent_{self.session_id}"

    def write(self, entries):
        with open(self.transcript, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def append(self, entries):
        with open(self.transcript, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def run(self, *, ok=True, derive_mock=None, max_batch=50):
        """Run process_transcript once. ok=False simulates a kill before the marker
        (the POST connection dies). Returns (ProcessResult, posted_events)."""
        config = {"api_key": "trm_test", "api_url": "https://example.test"}
        if ok:
            post = MagicMock(return_value=MagicMock(status_code=200))
        else:
            post = MagicMock(side_effect=RuntimeError("connection killed before marker"))

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(upload, "load_config", return_value=config))
            stack.enter_context(patch.object(upload.httpx, "post", post))
            stack.enter_context(patch.object(upload, "TERUM_DIR", self.terum_dir))
            if derive_mock is not None:
                stack.enter_context(patch.object(upload, "derive_repo", derive_mock))
            result = upload.process_transcript(
                self.transcript, self.session_id, self.cwd, max_batch=max_batch
            )

        events = []
        for call in post.call_args_list:
            events.extend(call.kwargs["json"]["events"])
        return result, events


def _turn(user, text, **usage):
    u = {"type": "user", "timestamp": "2026-07-06T00:00:00Z", "message": {"content": user}}
    a = {
        "type": "assistant",
        "timestamp": "2026-07-06T00:00:01Z",
        "message": {"content": [{"type": "text", "text": text}]},
    }
    if usage:
        a["message"]["usage"] = usage
    return [u, a]


class TestDurableResume:
    def test_killed_first_upload_caches_repo_so_retry_does_not_rerun_git(self):
        # The core bug-416 reproducer: pre-fix, the retry re-derives the repo (re-pays
        # git) because no progress was persisted before the kill.
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp)
            h.write(_turn("A real question with enough length here", "An answer with sufficient length."))
            derive = MagicMock(return_value="owner/repo")

            r1, _ = h.run(ok=False, derive_mock=derive)  # killed before the marker
            assert r1.status == "failed"
            assert derive.call_count == 1

            r2, events = h.run(ok=True, derive_mock=derive)  # retry
            assert r2.status == "uploaded"
            assert derive.call_count == 1, "retry re-derived repo — offset-0 loop still re-pays git"
            assert events and all(ev["repo"] == "owner/repo" for ev in events)

    def test_failed_first_upload_does_not_advance_and_resends_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp)
            none = MagicMock(return_value=None)  # deterministic: no git spawns
            h.write(
                _turn("First real question with length", "First answer with sufficient length.")
                + _turn("Second real question with length", "Second answer with sufficient length.")
            )
            r1, e1 = h.run(ok=False, derive_mock=none)  # killed before the marker
            assert r1.status == "failed"
            assert len(e1) == 2  # it attempted both turns

            r2, e2 = h.run(ok=True, derive_mock=none)  # retry
            assert r2.status == "uploaded"
            assert len(e2) == 2  # nothing confirmed sent -> both re-sent, no skip
            prompts = {ev["prompt"] for ev in e2}
            assert "First real question with length" in prompts
            assert "Second real question with length" in prompts

    def test_incremental_resume_keeps_cumulative_tokens_correct(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp)
            derive = MagicMock(return_value="owner/repo")
            h.write(_turn("First real question with length", "First answer here with length.",
                          input_tokens=10, output_tokens=7))
            r1, e1 = h.run(ok=True, derive_mock=derive)
            assert r1.status == "uploaded"
            assert e1 and all(ev["tokenInput"] == 10 and ev["tokenOutput"] == 7 for ev in e1)
            assert derive.call_count == 1

            # Append a second turn; cumulative totals must match a whole-file rescan.
            h.append(_turn("Second real question with length", "Second answer here with length.",
                           input_tokens=30, output_tokens=9))
            r2, e2 = h.run(ok=True, derive_mock=derive)
            assert r2.status == "uploaded" and len(e2) == 1  # only the new turn is sent
            ev = e2[0]
            assert ev["prompt"] == "Second real question with length"
            assert ev["tokenInput"] == 40   # 10 baseline + 30 delta
            assert ev["tokenOutput"] == 16  # 7 + 9
            assert derive.call_count == 1, "repo re-derived on incremental upload despite being cached"

    def test_legacy_integer_sidecar_resumes_and_recomputes_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(tmp)
            h.write(_turn("First real question with length", "First answer here with length.",
                          input_tokens=10, output_tokens=7))
            # A session tracked by the OLD bare-integer sidecar format.
            h.terum_dir.mkdir(parents=True, exist_ok=True)
            h.sidecar.write_text(str(os.path.getsize(h.transcript)))

            h.append(_turn("Second real question with length", "Second answer here with length.",
                           input_tokens=30, output_tokens=9))
            r, e = h.run(ok=True, derive_mock=MagicMock(return_value="owner/repo"))
            assert r.status == "uploaded" and len(e) == 1
            assert e[0]["prompt"] == "Second real question with length"
            # No baseline was persisted by the legacy format, so tokens come from a
            # one-time whole-file rescan: 10 + 30 / 7 + 9.
            assert e[0]["tokenInput"] == 40
            assert e[0]["tokenOutput"] == 16


class TestSidecarSerialization:
    def test_read_sidecar_handles_legacy_new_and_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sent_x"

            # Missing file -> offset 0, safe full reprocess.
            assert upload._read_sidecar(p) == {"offset": 0, "repo": None, "tokens": None}

            # Legacy bare integer.
            p.write_text("12345")
            assert upload._read_sidecar(p) == {"offset": 12345, "repo": None, "tokens": None}

            # Corrupt / empty -> reset to 0 (never a false "already sent").
            p.write_text("not-a-number")
            assert upload._read_sidecar(p)["offset"] == 0
            p.write_text("")
            assert upload._read_sidecar(p)["offset"] == 0

            # New JSON format round-trips (tokens read back as a 4-tuple).
            upload._write_sidecar(p, 42, "owner/repo", (1, 2, 3, 4))
            st = upload._read_sidecar(p)
            assert st["offset"] == 42
            assert st["repo"] == "owner/repo"
            assert st["tokens"] == (1, 2, 3, 4)

    def test_write_sidecar_omits_repo_and_tokens_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sent_y"
            upload._write_sidecar(p, 7, None, None)
            assert json.loads(p.read_text()) == {"offset": 7}
            assert upload._read_sidecar(p) == {"offset": 7, "repo": None, "tokens": None}
