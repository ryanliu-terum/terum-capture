import json
import os
import sys
import time
from pathlib import Path

import httpx

from terum_capture.config import load_config

TERUM_DIR = Path.home() / ".terum"
MAX_EVENTS_PER_BATCH = 50
HTTP_TIMEOUT = 10.0


def cmd_upload():
    """Called by the Stop hook. Reads hook input from stdin, parses transcript, POSTs new turns."""
    try:
        _do_upload()
    except Exception as exc:
        print(f"terum-capture: upload failed: {exc}", file=sys.stderr)
    sys.exit(0)


def _do_upload():
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return

    transcript_path = hook_input.get("transcript_path")
    session_id = hook_input.get("session_id")
    cwd = hook_input.get("cwd")
    if not transcript_path or not session_id or not os.path.isfile(transcript_path):
        return

    config = load_config()
    if not config or not config.get("api_key", "").startswith("trm_"):
        return

    sidecar = TERUM_DIR / f"sent_{session_id}"
    last_offset = 0
    if sidecar.exists():
        try:
            last_offset = int(sidecar.read_text().strip())
        except (ValueError, OSError):
            last_offset = 0

    file_size = os.path.getsize(transcript_path)
    if file_size <= last_offset:
        _cleanup_old_sidecars()
        return

    # Scan full transcript for session-level token usage (cheap integer sum)
    token_input = 0
    token_cache_creation = 0
    token_cache_read = 0
    token_output = 0
    with open(transcript_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "assistant":
                usage = entry.get("message", {}).get("usage", {})
                if usage:
                    token_input += usage.get("input_tokens", 0)
                    token_cache_creation += usage.get("cache_creation_input_tokens", 0)
                    token_cache_read += usage.get("cache_read_input_tokens", 0)
                    token_output += usage.get("output_tokens", 0)

    # Read only new lines for text extraction
    entries = []
    with open(transcript_path, "r") as f:
        if last_offset > 0:
            f.seek(last_offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    new_offset = file_size

    title = None
    turns: list[tuple[str, str, str | None]] = []
    current_prompt: str | None = None
    current_prompt_ts: str | None = None

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

    if not turns:
        _update_sidecar(sidecar, new_offset)
        _cleanup_old_sidecars()
        return

    events = []
    for prompt, response, ts in turns[:MAX_EVENTS_PER_BATCH]:
        event: dict = {
            "site": "claude-code",
            "conversationId": session_id,
            "prompt": prompt or "",
            "response": response or "",
            "capturedAt": ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if title:
            event["conversationTitle"] = title
        if cwd:
            event["cwd"] = cwd
        has_tokens = token_input or token_cache_creation or token_cache_read or token_output
        if has_tokens:
            event["tokenInput"] = token_input
            event["tokenCacheCreation"] = token_cache_creation
            event["tokenCacheRead"] = token_cache_read
            event["tokenOutput"] = token_output
        events.append(event)

    try:
        resp = httpx.post(
            f"{config['api_url']}/ingest/llm-history",
            json={"events": events},
            headers={"Authorization": f"Bearer {config['api_key']}"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code in (200, 201):
            _update_sidecar(sidecar, new_offset)
        else:
            print(f"terum-capture: server returned {resp.status_code}", file=sys.stderr)
    except Exception as exc:
        print(f"terum-capture: POST failed: {exc}", file=sys.stderr)

    _cleanup_old_sidecars()


def _update_sidecar(path: Path, offset: int):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(offset))
    except OSError:
        pass


def _cleanup_old_sidecars():
    """Remove sidecar files older than 7 days."""
    try:
        cutoff = time.time() - 7 * 86400
        for f in TERUM_DIR.glob("sent_*"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass
