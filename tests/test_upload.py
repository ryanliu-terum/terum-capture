"""Tests for transcript parsing and turn extraction."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def make_entry(etype, content=None, **kwargs):
    entry = {"type": etype, "timestamp": "2026-05-29T00:21:27.252Z"}
    if etype == "user" and content is not None:
        entry["message"] = {"content": content}
    elif etype == "assistant" and content is not None:
        entry["message"] = {"content": content}
    elif etype == "ai-title":
        entry["title"] = content
    entry.update(kwargs)
    return entry


def write_transcript(entries):
    """Write entries to a temp JSONL file, return path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    for entry in entries:
        f.write(json.dumps(entry) + "\n")
    f.close()
    return f.name


class TestTranscriptParsing:
    def test_extracts_user_text_and_assistant_text_blocks(self):
        entries = [
            make_entry("user", "Fix the auth bug"),
            make_entry("assistant", [
                {"type": "text", "text": "Looking at the code..."},
                {"type": "tool_use", "name": "Read", "input": {"path": "auth.ts"}},
                {"type": "text", "text": "Fixed the TTL."},
            ]),
        ]
        turns = _parse_entries(entries)
        assert len(turns) == 1
        assert turns[0][0] == "Fix the auth bug"
        assert "Looking at the code..." in turns[0][1]
        assert "Fixed the TTL." in turns[0][1]
        assert "tool_use" not in turns[0][1]
        assert "Read" not in turns[0][1]

    def test_skips_thinking_blocks(self):
        entries = [
            make_entry("user", "Debug this"),
            make_entry("assistant", [
                {"type": "thinking", "thinking": "Let me think..."},
                {"type": "text", "text": "The issue is in auth.ts"},
            ]),
        ]
        turns = _parse_entries(entries)
        assert len(turns) == 1
        assert "Let me think" not in turns[0][1]
        assert "auth.ts" in turns[0][1]

    def test_skips_system_wrapper_user_messages(self):
        entries = [
            make_entry("user", "<system-reminder>Hook output</system-reminder>"),
            make_entry("user", "Real user message"),
            make_entry("assistant", [{"type": "text", "text": "Got it."}]),
        ]
        turns = _parse_entries(entries)
        assert len(turns) == 1
        assert turns[0][0] == "Real user message"

    def test_skips_tool_result_user_messages(self):
        entries = [
            make_entry("user", [{"type": "tool_result", "content": "file data..."}]),
            make_entry("user", "Now fix the bug"),
            make_entry("assistant", [{"type": "text", "text": "Fixed."}]),
        ]
        turns = _parse_entries(entries)
        assert len(turns) == 1
        assert turns[0][0] == "Now fix the bug"

    def test_captures_ai_title(self):
        entries = [
            make_entry("ai-title", "Auth bug debugging session"),
            make_entry("user", "Fix auth"),
            make_entry("assistant", [{"type": "text", "text": "Done."}]),
        ]
        title, turns = _parse_with_title(entries)
        assert title == "Auth bug debugging session"
        assert len(turns) == 1

    def test_skips_non_conversation_entry_types(self):
        entries = [
            make_entry("mode", "code"),
            make_entry("permission-mode", "auto"),
            make_entry("system", "loaded context"),
            make_entry("user", "How do I configure the auth middleware?"),
            make_entry("assistant", [{"type": "text", "text": "You need to set the JWT secret in your env vars."}]),
        ]
        turns = _parse_entries(entries)
        assert len(turns) == 1

    def test_filters_trivial_turns(self):
        entries = [
            make_entry("user", "ok"),
            make_entry("assistant", [{"type": "text", "text": "k"}]),
            make_entry("user", "Fix the authentication middleware bug"),
            make_entry("assistant", [{"type": "text", "text": "Updated the TTL."}]),
        ]
        turns = _parse_entries(entries)
        # "ok" + "k" = 3 chars, below 10 threshold
        assert len(turns) == 1
        assert turns[0][0] == "Fix the authentication middleware bug"

    def test_empty_transcript(self):
        turns = _parse_entries([])
        assert turns == []

    def test_tool_only_transcript(self):
        entries = [
            make_entry("user", "Fix the authentication middleware bug in auth.ts"),
            make_entry("assistant", [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ]),
        ]
        turns = _parse_entries(entries)
        # Assistant had no text blocks, so user prompt becomes orphan turn
        assert len(turns) == 1
        assert "authentication middleware" in turns[0][0]
        assert turns[0][1] == ""

    def test_multiple_turns_grouped_correctly(self):
        entries = [
            make_entry("user", "First question about auth"),
            make_entry("assistant", [{"type": "text", "text": "Auth answer here."}]),
            make_entry("user", "Second question about database"),
            make_entry("assistant", [{"type": "text", "text": "Database answer here."}]),
        ]
        turns = _parse_entries(entries)
        assert len(turns) == 2
        assert "auth" in turns[0][0].lower()
        assert "database" in turns[1][0].lower()

    def test_malformed_jsonl_line_skipped(self):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        f.write(json.dumps(make_entry("user", "Valid message")) + "\n")
        f.write("not valid json\n")
        f.write(json.dumps(make_entry("assistant", [{"type": "text", "text": "Response."}])) + "\n")
        f.close()

        entries = []
        with open(f.name, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        os.unlink(f.name)

        turns = _parse_entries(entries)
        assert len(turns) == 1


def _parse_entries(entries):
    """Extract turns from entries using the same logic as upload.py."""
    title = None
    turns = []
    current_prompt = None
    current_prompt_ts = None

    for entry in entries:
        etype = entry.get("type")

        if etype == "ai-title" and title is None:
            title = entry.get("title") or entry.get("message", {}).get("content")

        elif etype == "user":
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                continue
            if not isinstance(content, str) or content.startswith("<"):
                continue
            if len(content.strip()) < 3:
                continue
            if current_prompt is not None:
                turns.append((current_prompt, "", current_prompt_ts))
            current_prompt = content.strip()
            current_prompt_ts = entry.get("timestamp")

        elif etype == "assistant":
            msg = entry.get("message", {})
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        texts.append(text)
            if not texts:
                continue
            response_text = "\n\n".join(texts)
            ts = entry.get("timestamp") or current_prompt_ts
            if current_prompt is not None:
                turns.append((current_prompt, response_text, ts))
                current_prompt = None
                current_prompt_ts = None
            else:
                turns.append(("", response_text, ts))

    if current_prompt is not None:
        turns.append((current_prompt, "", current_prompt_ts))

    turns = [
        (p, r, t) for p, r, t in turns
        if len(p) + len(r) >= 10
    ]
    return turns


def _parse_with_title(entries):
    """Same as _parse_entries but also returns title."""
    title = None
    turns = []
    current_prompt = None
    current_prompt_ts = None

    for entry in entries:
        etype = entry.get("type")
        if etype == "ai-title" and title is None:
            title = entry.get("title") or entry.get("message", {}).get("content")
        elif etype == "user":
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                continue
            if not isinstance(content, str) or content.startswith("<"):
                continue
            if len(content.strip()) < 3:
                continue
            if current_prompt is not None:
                turns.append((current_prompt, "", current_prompt_ts))
            current_prompt = content.strip()
            current_prompt_ts = entry.get("timestamp")
        elif etype == "assistant":
            msg = entry.get("message", {})
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        texts.append(text)
            if not texts:
                continue
            response_text = "\n\n".join(texts)
            ts = entry.get("timestamp") or current_prompt_ts
            if current_prompt is not None:
                turns.append((current_prompt, response_text, ts))
                current_prompt = None
                current_prompt_ts = None
            else:
                turns.append(("", response_text, ts))

    if current_prompt is not None:
        turns.append((current_prompt, "", current_prompt_ts))

    turns = [(p, r, t) for p, r, t in turns if len(p) + len(r) >= 10]
    return title, turns
