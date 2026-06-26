"""Tests for the extracted shared core process_transcript (spec Δ1 + §6).

Covers: golden event-shape parity, the batch-cap fix (max_batch=None sends every turn,
max_batch=50 still caps), the ProcessResult status taxonomy, sidecar advance-after-2xx,
the >server-cap chunking, and utf-8 robustness.
"""
import contextlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import terum_capture.upload as upload

# The reference parser the original test suite reimplements inline — parity target.
from tests.test_upload import _parse_entries


def _resp(code):
    r = MagicMock()
    r.status_code = code
    return r


def _write_transcript(tmp, entries, name="t.jsonl"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


@contextlib.contextmanager
def _harness(*, post_codes=(200,), config=None, repo=None):
    """Drive process_transcript with I/O edges mocked; yields (post_mock, terum_dir)."""
    if config is None:
        config = {"api_key": "trm_test", "api_url": "https://example.test"}
    post_mock = MagicMock(side_effect=[_resp(c) for c in post_codes])
    with tempfile.TemporaryDirectory() as terum:
        terum_dir = Path(terum) / "terum"
        with patch.object(upload, "load_config", return_value=config), \
             patch.object(upload, "derive_repo", return_value=repo), \
             patch.object(upload.httpx, "post", post_mock), \
             patch.object(upload, "TERUM_DIR", terum_dir):
            yield post_mock, terum_dir


def _posted_events(post_mock):
    """Flatten every POSTed chunk's events into one list (across chunked requests)."""
    events = []
    for call in post_mock.call_args_list:
        events.extend(call.kwargs["json"]["events"])
    return events


def _turn_pair(i):
    return [
        {"type": "user", "timestamp": f"2026-05-29T00:{i:02d}:00Z",
         "message": {"content": f"Question number {i} about the system design"}},
        {"type": "assistant", "timestamp": f"2026-05-29T00:{i:02d}:01Z",
         "message": {"content": [{"type": "text", "text": f"Answer number {i} with detail."}]}},
    ]


class TestGoldenParity:
    def test_event_shape_matches_expected(self):
        entries = [
            {"type": "ai-title", "title": "Design session", "timestamp": "2026-05-29T00:00:00Z"},
            {"type": "user", "timestamp": "2026-05-29T00:00:01Z",
             "message": {"content": "Explain the retrieval pipeline in detail"}},
            {"type": "assistant", "timestamp": "2026-05-29T00:00:02Z",
             "message": {"content": [{"type": "text", "text": "The pipeline distills then embeds."}]}},
        ]
        with _harness() as (post, _):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_transcript(tmp, entries)
                result = upload.process_transcript(path, "sess-1", "/home/u/proj", max_batch=50)
        assert result.status == "uploaded"
        events = _posted_events(post)
        assert len(events) == 1
        ev = events[0]
        assert ev["site"] == "claude-code"
        assert ev["conversationId"] == "sess-1"
        assert ev["prompt"] == "Explain the retrieval pipeline in detail"
        assert "distills then embeds" in ev["response"]
        assert ev["capturedAt"] == "2026-05-29T00:00:02Z"  # the assistant entry's real ts
        assert ev["conversationTitle"] == "Design session"
        assert ev["cwd"] == "/home/u/proj"

    def test_parse_turns_matches_reference_parser(self):
        # The extracted _parse_turns must filter identically to the original inline logic.
        entries = [
            {"type": "user", "message": {"content": "First real question about auth"}, "timestamp": "t1"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Auth answer here."}]}, "timestamp": "t2"},
            {"type": "user", "message": {"content": "ok"}, "timestamp": "t3"},  # trivial
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "k"}]}, "timestamp": "t4"},
            {"type": "user", "message": {"content": ["tool_result blob"]}, "timestamp": "t5"},  # list -> skip
            {"type": "user", "message": {"content": "<system-reminder>x</system-reminder>"}, "timestamp": "t6"},  # system -> skip
        ]
        _, turns = upload._parse_turns(entries)
        assert turns == _parse_entries(entries)


class TestBatchCapFix:
    def test_max_batch_none_sends_all_turns(self):
        entries = []
        for i in range(60):
            entries.extend(_turn_pair(i))
        with _harness() as (post, _):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_transcript(tmp, entries)
                result = upload.process_transcript(path, "sess-big", "/c", max_batch=None)
        assert result.status == "uploaded"
        assert len(_posted_events(post)) == 60  # not silently truncated to 50

    def test_hook_max_batch_caps_at_50(self):
        entries = []
        for i in range(60):
            entries.extend(_turn_pair(i))
        with _harness() as (post, _):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_transcript(tmp, entries)
                result = upload.process_transcript(path, "sess-hook", "/c", max_batch=50)
        assert result.status == "uploaded"
        assert len(_posted_events(post)) == 50  # live-hook behavior preserved


class TestChunking:
    def test_over_server_cap_splits_into_multiple_posts(self):
        # Shrink the cap so the test stays fast but exercises the chunk loop + tally.
        entries = []
        for i in range(5):
            entries.extend(_turn_pair(i))
        with patch.object(upload, "SERVER_MAX_EVENTS", 2):
            with _harness(post_codes=(200, 200, 200)) as (post, td):
                with tempfile.TemporaryDirectory() as tmp:
                    path = _write_transcript(tmp, entries)
                    result = upload.process_transcript(path, "sess-chunk", "/c", max_batch=None)
                    # sidecar advanced only after ALL chunks succeeded (assert inside harness)
                    sidecar_exists = (td / "sent_sess-chunk").exists()
        assert result.status == "uploaded"
        assert result.events == 5
        assert post.call_count == 3  # ceil(5/2)
        assert sidecar_exists

    def test_failed_chunk_does_not_advance_sidecar(self):
        entries = []
        for i in range(5):
            entries.extend(_turn_pair(i))
        with patch.object(upload, "SERVER_MAX_EVENTS", 2):
            with _harness(post_codes=(200, 500)) as (post, td):
                with tempfile.TemporaryDirectory() as tmp:
                    path = _write_transcript(tmp, entries)
                    result = upload.process_transcript(path, "sess-fail", "/c", max_batch=None)
                    sidecar_exists = (td / "sent_sess-fail").exists()
        assert result.status == "failed"
        assert not sidecar_exists  # offset untouched -> safe re-run


class TestStatusTaxonomy:
    def _single(self):
        return [
            {"type": "user", "timestamp": "2026-05-29T00:00:00Z",
             "message": {"content": "A real question with enough length here"}},
            {"type": "assistant", "timestamp": "2026-05-29T00:00:01Z",
             "message": {"content": [{"type": "text", "text": "An answer with sufficient length."}]}},
        ]

    def test_uploaded_advances_sidecar(self):
        with _harness(post_codes=(200,)) as (post, td):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_transcript(tmp, self._single())
                result = upload.process_transcript(path, "s", "/c", max_batch=None)
                size = os.path.getsize(path)
                offset = (td / "sent_s").read_text().strip()
        assert result.status == "uploaded"
        assert offset == str(size)

    def test_429_returns_rate_limited_and_no_advance(self):
        with _harness(post_codes=(429,)) as (post, td):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_transcript(tmp, self._single())
                result = upload.process_transcript(path, "s", "/c", max_batch=None)
                sidecar_exists = (td / "sent_s").exists()
        assert result.status == "rate_limited"
        assert result.status_code == 429
        assert not sidecar_exists

    def test_skipped_when_nothing_new(self):
        with _harness(post_codes=(200, 200)) as (post, td):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_transcript(tmp, self._single())
                first = upload.process_transcript(path, "s", "/c", max_batch=None)
                second = upload.process_transcript(path, "s", "/c", max_batch=None)
        assert first.status == "uploaded"
        assert second.status == "skipped"
        assert post.call_count == 1  # second run made no POST

    def test_no_turns_advances_sidecar_without_post(self):
        # Only a system message -> no qualifying turns, but bytes were consumed.
        entries = [{"type": "user", "timestamp": "t", "message": {"content": "<system-reminder>x</system-reminder>"}}]
        with _harness() as (post, td):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_transcript(tmp, entries)
                result = upload.process_transcript(path, "s", "/c", max_batch=None)
                sidecar_exists = (td / "sent_s").exists()
        assert result.status == "no_turns"
        assert post.call_count == 0
        assert sidecar_exists

    def test_invalid_when_transcript_missing(self):
        with _harness() as (post, td):
            result = upload.process_transcript("/no/such/file.jsonl", "s", "/c", max_batch=None)
        assert result.status == "invalid"
        assert post.call_count == 0

    def test_unconfigured_when_no_key(self):
        with _harness(config={"api_url": "https://x"}) as (post, td):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_transcript(tmp, self._single())
                result = upload.process_transcript(path, "s", "/c", max_batch=None)
        assert result.status == "unconfigured"
        assert post.call_count == 0


class TestUnicodeRobustness:
    def test_unicode_transcript_does_not_crash(self):
        entries = [
            {"type": "user", "timestamp": "t1", "message": {"content": "Explain the café ☕ pipeline 日本語"}},
            {"type": "assistant", "timestamp": "t2", "message": {"content": [{"type": "text", "text": "It runs — naïvely — fine 🚀."}]}},
        ]
        with _harness() as (post, td):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_transcript(tmp, entries)
                result = upload.process_transcript(path, "s", "/c", max_batch=None)
        assert result.status == "uploaded"
        ev = _posted_events(post)[0]
        assert "café" in ev["prompt"]
        assert "🚀" in ev["response"]


class TestRepoReuse:
    def test_repo_field_attached_when_derive_repo_returns_value(self):
        entries = [
            {"type": "user", "timestamp": "t1", "message": {"content": "A real question with enough length"}},
            {"type": "assistant", "timestamp": "t2", "message": {"content": [{"type": "text", "text": "A sufficiently long answer."}]}},
        ]
        with _harness(repo="ryanliu-terum/Terum-MVP") as (post, td):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_transcript(tmp, entries)
                upload.process_transcript(path, "s", "/home/u/Terum-MVP", max_batch=None)
        assert all(ev["repo"] == "ryanliu-terum/Terum-MVP" for ev in _posted_events(post))

    def test_repo_field_omitted_when_derive_repo_none(self):
        # A moved/deleted repo -> derive_repo None -> no repo field (cwd still carried).
        entries = [
            {"type": "user", "timestamp": "t1", "message": {"content": "A real question with enough length"}},
            {"type": "assistant", "timestamp": "t2", "message": {"content": [{"type": "text", "text": "A sufficiently long answer."}]}},
        ]
        with _harness(repo=None) as (post, td):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_transcript(tmp, entries)
                upload.process_transcript(path, "s", "/gone", max_batch=None)
        events = _posted_events(post)
        assert events and all("repo" not in ev for ev in events)
