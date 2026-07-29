"""Tests for the session activity digest (capture blind-spot study, 2026-07-28).

The digest folds the tool activity _parse_turns drops — Edit/Write file paths and
is_error Bash failures — into one bounded synthetic turn. These tests drive the
pure extractor plus its wiring through process_transcript's shared parsing brain.
Adversarial cases target the study's own false-positive trap: successful commands
whose OUTPUT contains the word "error" must NOT be flagged.
"""
import json

import terum_capture.upload as upload


def _tool_use(name, tool_id, **inp):
    return {
        "type": "assistant",
        "timestamp": "2026-07-28T10:00:00.000Z",
        "message": {"content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]},
    }


def _tool_result(tool_id, text, is_error=False, ts="2026-07-28T10:00:05.000Z"):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": is_error,
                    "content": [{"type": "text", "text": text}],
                }
            ]
        },
    }


def test_edited_files_and_failed_bash_captured():
    entries = [
        _tool_use("Edit", "t1", file_path="C:\\dev\\Terum\\MVP\\lib\\memory\\compactor.ts"),
        _tool_use("Write", "t2", file_path="/home/u/proj/app/api/route.ts"),
        _tool_use("Bash", "t3", command="npm test\nsecond line ignored"),
        _tool_result("t3", "FAIL src/x.test.ts\nExit code 1", is_error=True),
    ]
    digest, ts = upload._extract_activity_digest(entries)
    assert "lib/memory/compactor.ts" in digest
    assert "app/api/route.ts" in digest
    assert "`npm test`" in digest and "second line" not in digest
    assert "FAIL src/x.test.ts" in digest
    assert ts == "2026-07-28T10:00:05.000Z"


def test_successful_command_with_error_in_output_not_flagged():
    # The study's false-positive trap: a grep whose OUTPUT contains "error" but
    # which succeeded (is_error False) must not appear as a failure.
    entries = [
        _tool_use("Bash", "t1", command="grep -rn error lib/"),
        _tool_result("t1", "lib/a.ts:12: return { error: null }", is_error=False),
    ]
    assert upload._extract_activity_digest(entries) is None


def test_non_bash_errors_and_read_tools_ignored():
    entries = [
        _tool_use("Read", "t1", file_path="/x/y/z.ts"),
        _tool_use("Grep", "t2", pattern="foo"),
        _tool_result("t2", "not found", is_error=True),
    ]
    assert upload._extract_activity_digest(entries) is None


def test_caps_respected():
    entries = []
    for i in range(30):
        entries.append(_tool_use("Edit", f"e{i}", file_path=f"/repo/src/file{i}.ts"))
    for i in range(15):
        entries.append(_tool_use("Bash", f"b{i}", command=f"cmd{i} --flag"))
        entries.append(_tool_result(f"b{i}", f"Error: boom {i}", is_error=True))
    digest, _ = upload._extract_activity_digest(entries)
    assert len(digest) <= upload.DIGEST_MAX_CHARS
    assert "(+10 more)" in digest
    assert digest.count("`cmd") <= upload.DIGEST_MAX_FAILURES


def test_dedupes_repeated_edits_of_same_file():
    entries = [
        _tool_use("Edit", "t1", file_path="/repo/src/a.ts"),
        _tool_use("Edit", "t2", file_path="/repo/src/a.ts"),
    ]
    digest, _ = upload._extract_activity_digest(entries)
    assert digest.count("src/a.ts") == 1


def test_no_activity_returns_none():
    entries = [
        {"type": "user", "message": {"content": "plain prompt"}, "timestamp": "2026-07-28T10:00:00.000Z"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "plain answer"}]}},
    ]
    assert upload._extract_activity_digest(entries) is None


def test_bump_timestamp_advances_one_second():
    assert upload._bump_timestamp("2026-07-28T10:00:59.500Z") == "2026-07-28T10:01:00.500Z"
    assert upload._bump_timestamp(None) is None
    assert upload._bump_timestamp("garbage") == "garbage"


def test_digest_turn_flows_through_parse_and_append(tmp_path, monkeypatch):
    """End-to-end through process_transcript: the digest arrives as the LAST event,
    response-only, with a bumped timestamp — and prose turns are unchanged."""
    entries = [
        {"type": "user", "timestamp": "2026-07-28T10:00:00.000Z", "message": {"content": "please fix the bug in auth"}},
        {
            "type": "assistant",
            "timestamp": "2026-07-28T10:00:10.000Z",
            "message": {"content": [{"type": "text", "text": "Fixed it by widening the TTL check."}]},
        },
        _tool_use("Edit", "t1", file_path="/repo/lib/auth.ts"),
        _tool_result("t1", "ok", is_error=False, ts="2026-07-28T10:00:20.000Z"),
        _tool_use("Bash", "t2", command="npm run typecheck"),
        _tool_result("t2", "error TS2345: nope", is_error=True, ts="2026-07-28T10:00:30.000Z"),
    ]
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")

    posted = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002 - mirrors httpx signature
        posted["events"] = json["events"]

        class R:
            status_code = 200

            def raise_for_status(self):
                return None

        return R()

    monkeypatch.setattr(upload, "TERUM_DIR", tmp_path)
    monkeypatch.setattr(upload, "load_config", lambda: {"api_key": "trm_x", "api_url": "https://api.example"})
    monkeypatch.setattr(upload.httpx, "post", fake_post)

    result = upload.process_transcript(str(transcript), "sess-1", str(tmp_path))
    assert result.status == "uploaded"
    events = posted["events"]
    last = events[-1]
    assert last["prompt"] == ""
    assert "[Session activity digest" in last["response"]
    assert "lib/auth.ts" in last["response"]
    assert "error TS2345" in last["response"]
    assert last["capturedAt"] == "2026-07-28T10:00:31.000Z"
    assert events[0]["prompt"] == "please fix the bug in auth"
