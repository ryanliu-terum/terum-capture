"""Tests for the event payload built by the real _do_upload path.

Unlike test_upload.py (which reimplements the parser) these drive the actual
_do_upload function, mocking only its I/O edges: stdin (the Stop-hook input),
load_config, httpx.post, and TERUM_DIR (sidecar location). This is the layer
where prompt/response are serialized for the wire — the only place that can
catch the null-vs-empty-string and session-token regressions.
"""
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import terum_capture.upload as upload


def _run_do_upload(entries, *, session_id="session-xyz", cwd="/home/u/proj", config=None):
    """Drive the real _do_upload over a temp transcript; return the POSTed events.

    Returns the `events` array from the captured POST body, or None if no POST
    happened (e.g. no qualifying turns).
    """
    if config is None:
        config = {"api_key": "trm_test", "api_url": "https://example.test"}

    with tempfile.TemporaryDirectory() as tmp:
        transcript = os.path.join(tmp, "transcript.jsonl")
        with open(transcript, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        hook_input = json.dumps(
            {"transcript_path": transcript, "session_id": session_id, "cwd": cwd}
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post = MagicMock(return_value=mock_resp)

        with patch("sys.stdin", io.StringIO(hook_input)), \
             patch.object(upload, "load_config", return_value=config), \
             patch.object(upload.httpx, "post", mock_post), \
             patch.object(upload, "TERUM_DIR", Path(tmp) / "terum"):
            upload._do_upload()

        if not mock_post.called:
            return None
        return mock_post.call_args.kwargs["json"]["events"]


class TestOrphanTurnSerialization:
    """The empty side of an orphan turn must serialize as "" — never null.

    The ingest Zod schema declares prompt/response as z.string().optional(),
    which accepts undefined but REJECTS null. A null makes the whole batch 400,
    the sidecar offset never advances, and that session re-fails forever.
    """

    def test_orphan_assistant_sends_empty_string_prompt(self):
        # Assistant reply with no preceding user prompt -> parser yields ("", response).
        entries = [
            {
                "type": "assistant",
                "timestamp": "2026-05-29T00:00:00Z",
                "message": {
                    "content": [
                        {"type": "text", "text": "A standalone answer with enough length."}
                    ]
                },
            }
        ]
        events = _run_do_upload(entries)
        assert events is not None and len(events) == 1
        ev = events[0]
        assert ev["prompt"] == ""  # would be None under the `prompt or None` bug
        assert ev["prompt"] is not None
        assert isinstance(ev["prompt"], str)
        assert "standalone answer" in ev["response"]

    def test_orphan_user_sends_empty_string_response(self):
        # Trailing user prompt with no assistant reply -> parser yields (prompt, "").
        entries = [
            {
                "type": "user",
                "timestamp": "2026-05-29T00:00:00Z",
                "message": {"content": "A trailing question with no assistant reply yet"},
            }
        ]
        events = _run_do_upload(entries)
        assert events is not None and len(events) == 1
        ev = events[0]
        assert ev["response"] == ""  # would be None under the `response or None` bug
        assert ev["response"] is not None
        assert isinstance(ev["response"], str)
        assert "trailing question" in ev["prompt"]


class TestSessionTokenUsage:
    """Session-level token totals from transcript `usage` are attached to events."""

    def test_four_token_fields_sent_separately(self):
        entries = [
            {
                "type": "user",
                "timestamp": "2026-05-29T00:00:00Z",
                "message": {"content": "Explain the retrieval pipeline in detail"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-29T00:00:01Z",
                "message": {
                    "content": [{"type": "text", "text": "The retrieval pipeline works thus."}],
                    "usage": {
                        "input_tokens": 100,
                        "cache_creation_input_tokens": 20,
                        "cache_read_input_tokens": 5,
                        "output_tokens": 50,
                    },
                },
            },
        ]
        events = _run_do_upload(entries)
        assert events is not None and len(events) == 1
        ev = events[0]
        assert ev["tokenInput"] == 100
        assert ev["tokenCacheCreation"] == 20
        assert ev["tokenCacheRead"] == 5
        assert ev["tokenOutput"] == 50
        assert "sessionInputTokens" not in ev
        assert "sessionOutputTokens" not in ev

    def test_tokens_summed_across_turns_and_attached_to_every_event(self):
        entries = [
            {"type": "user", "timestamp": "2026-05-29T00:00:00Z",
             "message": {"content": "First real question here"}},
            {"type": "assistant", "timestamp": "2026-05-29T00:00:01Z",
             "message": {
                 "content": [{"type": "text", "text": "First answer with sufficient length."}],
                 "usage": {"input_tokens": 10, "output_tokens": 7}}},
            {"type": "user", "timestamp": "2026-05-29T00:00:02Z",
             "message": {"content": "Second real question here"}},
            {"type": "assistant", "timestamp": "2026-05-29T00:00:03Z",
             "message": {
                 "content": [{"type": "text", "text": "Second answer with sufficient length."}],
                 "usage": {"input_tokens": 30, "output_tokens": 9}}},
        ]
        events = _run_do_upload(entries)
        assert events is not None and len(events) == 2
        for ev in events:
            assert ev["tokenInput"] == 40  # 10 + 30
            assert ev["tokenOutput"] == 16  # 7 + 9
            assert ev["tokenCacheCreation"] == 0
            assert ev["tokenCacheRead"] == 0

    def test_no_usage_omits_token_fields(self):
        entries = [
            {"type": "user", "timestamp": "2026-05-29T00:00:00Z",
             "message": {"content": "A question with no usage data attached"}},
            {"type": "assistant", "timestamp": "2026-05-29T00:00:01Z",
             "message": {"content": [{"type": "text", "text": "An answer with no usage data."}]}},
        ]
        events = _run_do_upload(entries)
        assert events is not None and len(events) == 1
        ev = events[0]
        assert "tokenInput" not in ev
        assert "tokenOutput" not in ev
        assert "tokenCacheCreation" not in ev
        assert "tokenCacheRead" not in ev
