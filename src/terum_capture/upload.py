import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from terum_capture.config import load_config

TERUM_DIR = Path.home() / ".terum"
MAX_EVENTS_PER_BATCH = 50
# The ingest route accepts <=10k events per request; backfill (max_batch=None) chunks
# to this ceiling. The live hook's 50-cap never reaches it, so a chunk == one POST there.
SERVER_MAX_EVENTS = 10000
HTTP_TIMEOUT = 10.0
GIT_TIMEOUT = 5.0

# git remote URL forms we normalize to a canonical "owner/repo":
#   SCP:  [user@]host:owner/repo            e.g. git@github.com:owner/repo
#   URL:  scheme://[userinfo@]host[:port]/owner/repo
_SCP_RE = re.compile(r"^[^/@]+@[^/:]+:(?P<path>.+)$")
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/]+/(?P<path>.+)$")


def _parse_owner_repo(url: str | None) -> str | None:
    """Normalize a git remote URL to a canonical "owner/repo" path.

    For URL forms the path is taken AFTER the host, so any embedded credential
    (e.g. https://x-access-token:TOKEN@host/owner/repo) is dropped — a token must
    never reach stored data. Returns None for anything that isn't a remote URL,
    including raw filesystem paths, so this can never re-introduce bug-294's
    cwd-as-repo behavior.
    """
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]

    m = _URL_RE.match(url) if "://" in url else _SCP_RE.match(url)
    if not m:
        return None
    path = m.group("path").strip("/")
    # A real owner/repo always has a separator; a bare token/segment does not.
    if "/" not in path:
        return None
    return path or None


def _git(cwd: str, args: list[str]) -> str | None:
    """Run `git -C <cwd> <args>`; return trimmed stdout, or None on any failure.

    A timeout bounds a hung git so it can never stall the Stop hook; any spawn
    error (git missing), non-zero exit (not a repo), or timeout degrades to None.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    return out or None


def derive_repo(cwd: str | None) -> str | None:
    """Stable, location-independent repo identity from the git remote at cwd.

    Fallback chain: origin remote -> first other remote -> repo-root basename ->
    None. Git resolves all of these from any subdirectory or worktree of the
    repo, so the same repo yields the same identity regardless of OS, checkout
    path, or which subdir Claude Code ran in.
    """
    if not cwd:
        return None
    url = _git(cwd, ["config", "--get", "remote.origin.url"])
    if not url:
        remotes = _git(cwd, ["remote"])
        if remotes:
            first = remotes.splitlines()[0].strip()
            if first:
                url = _git(cwd, ["config", "--get", f"remote.{first}.url"])
    repo = _parse_owner_repo(url) if url else None
    if repo:
        return repo
    toplevel = _git(cwd, ["rev-parse", "--show-toplevel"])
    if toplevel:
        base = toplevel.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return base or None
    return None


@dataclass
class ProcessResult:
    """Outcome of processing one transcript — drives the backfill tally + the hook flow.

    status:
      "uploaded"     — every event POSTed with a 2xx; sidecar advanced.
      "skipped"      — nothing new (file_size <= last sidecar offset); no POST.
      "no_turns"     — new bytes but no qualifying turns; sidecar advanced.
      "rate_limited" — a 429; sidecar NOT advanced (caller backs off + retries).
      "failed"       — non-2xx/non-429 or a transport error; sidecar NOT advanced.
      "unconfigured" — no usable api_key.
      "invalid"      — missing/unreadable transcript args.
    """

    status: str
    events: int = 0
    status_code: int | None = None


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
    # The live hook keeps its historical 50-turn cap; the shared core does the work.
    process_transcript(
        hook_input.get("transcript_path"),
        hook_input.get("session_id"),
        hook_input.get("cwd"),
        max_batch=MAX_EVENTS_PER_BATCH,
    )


def _read_offset(sidecar: Path) -> int:
    """The 'already sent up to here' byte offset, or 0 (fresh / unreadable / corrupt)."""
    if not sidecar.exists():
        return 0
    try:
        return int(sidecar.read_text().strip())
    except (ValueError, OSError):
        return 0


def process_transcript(
    transcript_path: str | None,
    session_id: str | None,
    cwd: str | None,
    *,
    max_batch: int | None = None,
) -> ProcessResult:
    """Offset-read -> parse turns -> build events -> POST -> advance sidecar, for ONE transcript.

    The single parsing brain shared by the live Stop hook (``max_batch=50``, the historical
    cap) and backfill (``max_batch=None`` — send every unsent turn, since a whole historical
    session routinely exceeds 50 turn-pairs and the cap would silently drop the overflow,
    spec Δ1). ``conversationId`` is the session uuid, so a session already captured live
    dedups server-side. The sidecar offset advances ONLY after every event POSTs with a 2xx
    (state-persistence-after-success), so a crash or 429 mid-session re-processes it next run.
    """
    if not transcript_path or not session_id or not os.path.isfile(transcript_path):
        return ProcessResult("invalid")

    config = load_config()
    if (
        not config
        or not config.get("api_key", "").startswith("trm_")
        or not config.get("api_url")
    ):
        # api_url is required: _post_events reads config['api_url'] directly, so a
        # config missing it must short-circuit here rather than KeyError mid-upload.
        return ProcessResult("unconfigured")

    sidecar = TERUM_DIR / f"sent_{session_id}"
    last_offset = _read_offset(sidecar)

    file_size = os.path.getsize(transcript_path)
    if file_size <= last_offset:
        _cleanup_old_sidecars()
        return ProcessResult("skipped")

    tokens = _scan_session_tokens(transcript_path)
    entries = _read_entries(transcript_path, last_offset)
    new_offset = file_size

    title, turns = _parse_turns(entries)

    if not turns:
        _update_sidecar(sidecar, new_offset)
        _cleanup_old_sidecars()
        return ProcessResult("no_turns")

    # Stable repo identity (bug-294): one git resolution per session, sent on every
    # event so the backend keys/names projects by repo instead of the raw cwd basename.
    repo = derive_repo(cwd)
    selected = turns if max_batch is None else turns[:max_batch]
    events = _build_events(selected, session_id, title, cwd, repo, tokens)

    result = _post_events(config, events, sidecar, new_offset)
    _cleanup_old_sidecars()
    return result


def _scan_session_tokens(transcript_path: str) -> tuple[int, int, int, int]:
    """Sum session-level token usage across the full transcript (cheap integer scan)."""
    token_input = 0
    token_cache_creation = 0
    token_cache_read = 0
    token_output = 0
    with open(transcript_path, "r", encoding="utf-8") as f:
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
    return token_input, token_cache_creation, token_cache_read, token_output


def _read_entries(transcript_path: str, last_offset: int) -> list:
    """Read+parse JSONL entries from last_offset (a BYTE offset) to EOF, skipping garbage.

    Opened in binary mode so the persisted ``os.path.getsize()`` offset and ``seek()``
    agree byte-for-byte. Text-mode seeking to an arbitrary byte offset is only well-defined
    at 0 or a prior ``tell()`` cookie — on Windows, CRLF translation makes a raw getsize()
    offset land unpredictably, so binary mode is the only byte-accurate resume. Each line is
    utf-8 decoded per-line (mirrors _scan_session_tokens); a non-utf-8 or non-JSON line is
    skipped rather than crashing the read, preserving the JSON-per-line skip-on-garbage
    behavior. Offset 0 reads the whole file (no seek), unchanged from the prior text read.
    """
    entries = []
    with open(transcript_path, "rb") as f:
        if last_offset > 0:
            f.seek(last_offset)
        for raw in f:
            try:
                line = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _parse_turns(entries: list) -> tuple[str | None, list[tuple[str, str, str | None]]]:
    """Pair user/assistant entries into (prompt, response, timestamp) turns.

    Unchanged from the original inline _do_upload logic: skips list-content + system
    (`<…>`) + sub-3-char user messages, joins assistant text blocks, carries each turn's
    real timestamp, and drops turns under 10 combined chars. Backfill reuses this verbatim
    (no new shortcut parsing), so adversarial/old transcripts filter identically to live.
    """
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

    turns = [(p, r, t) for p, r, t in turns if len(p) + len(r) >= 10]
    return title, turns


def _build_events(turns, session_id, title, cwd, repo, tokens) -> list[dict]:
    """Serialize turns into the `claude-code` event shape the ingest route expects."""
    token_input, token_cache_creation, token_cache_read, token_output = tokens
    has_tokens = token_input or token_cache_creation or token_cache_read or token_output
    events = []
    for prompt, response, ts in turns:
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
        if repo:
            event["repo"] = repo
        if has_tokens:
            event["tokenInput"] = token_input
            event["tokenCacheCreation"] = token_cache_creation
            event["tokenCacheRead"] = token_cache_read
            event["tokenOutput"] = token_output
        events.append(event)
    return events


def _post_events(config, events, sidecar, new_offset) -> ProcessResult:
    """POST events (chunked to the server's per-request cap) and advance the sidecar.

    The sidecar advances ONLY after every chunk returns 2xx (state-persistence-after-
    success). A 429 is reported distinctly so the backfill caller can back off and retry
    the whole session; any other failure leaves the offset untouched too, so a re-run
    re-sends and server dedup (conversation_id + capturedAt) collapses the overlap.
    """
    url = f"{config['api_url']}/ingest/llm-history"
    headers = {"Authorization": f"Bearer {config['api_key']}"}
    sent = 0
    for start in range(0, len(events), SERVER_MAX_EVENTS):
        chunk = events[start:start + SERVER_MAX_EVENTS]
        try:
            resp = httpx.post(url, json={"events": chunk}, headers=headers, timeout=HTTP_TIMEOUT)
        except Exception as exc:
            print(f"terum-capture: POST failed: {exc}", file=sys.stderr)
            return ProcessResult("failed", events=sent)
        if resp.status_code == 429:
            return ProcessResult("rate_limited", events=sent, status_code=429)
        if resp.status_code not in (200, 201):
            print(f"terum-capture: server returned {resp.status_code}", file=sys.stderr)
            return ProcessResult("failed", events=sent, status_code=resp.status_code)
        sent += len(chunk)
    _update_sidecar(sidecar, new_offset)
    return ProcessResult("uploaded", events=sent, status_code=200)


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
