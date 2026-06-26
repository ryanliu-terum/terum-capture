"""Historical Claude Code session backfill (spec: 2026-06-25-claude-code-session-backfill).

Discovers prior transcripts under ``~/.claude/projects/<project>/<uuid>.jsonl`` and feeds
each through ``process_transcript`` — the exact same parse -> ingest path the live Stop
hook uses — so a freshly-installed user's knowledge graph is populated from day one
instead of starting empty.

Reuse, not reimplementation: parsing, event-building, the sidecar offset, ``derive_repo``,
and the POST all live in ``upload.py``. This module is only discovery + a throttled loop
with 429 back-off. Idempotency is free — ``conversationId`` is the session uuid (so a
session already live-captured dedups server-side) and the sidecar offset skips
already-sent sessions on a re-run.
"""
import json
import sys
import time
from pathlib import Path

from terum_capture.config import load_config
from terum_capture.upload import ProcessResult, process_transcript

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_WINDOW_DAYS = 30

# R5 (reconciliation #2): sleep >=1.0s between sessions (~60 req/min) keeps us comfortably
# under the backend's 120-req/60s limiter. 0.5s sits AT the limit, 0.3s exceeds it.
THROTTLE_SECONDS = 1.0

# On a 429 we back off and retry the SAME session (its sidecar was not advanced).
MAX_BACKOFF_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0


def discover_sessions(
    window_days: int | None = DEFAULT_WINDOW_DAYS,
    limit: int | None = None,
    *,
    projects_dir: Path | None = None,
    now: float | None = None,
) -> list[Path]:
    """Top-level session transcripts within the mtime window, newest first.

    The ``*/*.jsonl`` glob is depth-2 by design: it returns only top-level sessions and
    structurally excludes subagent/sidechain transcripts that live one level deeper at
    ``<project>/<uuid>/subagents/*.jsonl`` (a ~36x noise cut on a heavy machine). A
    ``window_days`` of ``None`` means no time filter (the ``--all`` escape hatch).
    """
    base = projects_dir or CLAUDE_PROJECTS
    if not base.is_dir():
        return []

    now = time.time() if now is None else now
    cutoff = None if window_days is None else now - window_days * 86400

    sessions: list[tuple[Path, float]] = []
    for path in base.glob("*/*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if cutoff is not None and mtime < cutoff:
            continue
        sessions.append((path, mtime))

    sessions.sort(key=lambda pm: pm[1], reverse=True)
    paths = [p for p, _ in sessions]
    if limit is not None:
        paths = paths[:limit]
    return paths


def _read_session_cwd(path: Path) -> str | None:
    """The cwd recorded in the transcript, from the first entry that carries one.

    Entries carry their own ``cwd``; line 1 may be a summary/index entry without one, so
    we scan until we find a non-empty string. ``derive_repo`` later turns this into a
    stable repo identity (or None for a moved/deleted repo — the cwd fallback is fine).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = entry.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _process_with_backoff(path: Path, config, *, sleep=time.sleep) -> ProcessResult:
    """Process one session, retrying on 429 with exponential back-off.

    A 429 means the rate limiter is saturated; ``process_transcript`` did NOT advance the
    sidecar, so retrying re-sends the whole session cleanly (server dedup is the backstop).
    After the retry budget is spent we return the rate_limited result — the caller counts
    it as not-yet-done so the next ``backfill`` run picks it up.
    """
    session_id = path.stem
    cwd = _read_session_cwd(path)
    backoff = INITIAL_BACKOFF_SECONDS
    result = ProcessResult("failed")
    for attempt in range(MAX_BACKOFF_RETRIES + 1):
        try:
            result = process_transcript(path, session_id, cwd, max_batch=None)
        except Exception as exc:  # never let one bad session abort the whole run
            print(f"terum-capture: session {session_id} failed: {exc}", file=sys.stderr)
            return ProcessResult("failed")
        if result.status != "rate_limited":
            return result
        if attempt >= MAX_BACKOFF_RETRIES:
            print(
                f"terum-capture: session {session_id} still rate-limited after "
                f"{MAX_BACKOFF_RETRIES} retries; will retry on the next backfill run",
                file=sys.stderr,
            )
            return result
        sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    return result


def cmd_backfill(
    window_days: int | None = DEFAULT_WINDOW_DAYS,
    limit: int | None = None,
    *,
    config=None,
    projects_dir: Path | None = None,
    sleep=time.sleep,
) -> None:
    """Discover in-window sessions and upload their unsent turns through the live pipeline.

    Honest reporting (no-silent-degradation): the tally distinguishes uploaded / already-
    captured / failed; any failures are surfaced with the retry instruction and never
    rolled into a false "done". Processing finishes server-side asynchronously, so the
    message says so rather than implying the knowledge is ready.
    """
    if config is None:
        config = load_config()
    if not config or not config.get("api_key", "").startswith("trm_"):
        print("Not configured. Run: terum-capture setup")
        return

    sessions = discover_sessions(window_days, limit, projects_dir=projects_dir)
    if not sessions:
        if window_days is None:
            print("No Claude Code sessions found.")
        else:
            print(f"No sessions found in the last {window_days} days.")
        return

    print(f"Found {len(sessions)} Claude Code session(s) to import...")
    uploaded = skipped = failed = 0
    last = len(sessions) - 1
    for i, path in enumerate(sessions):
        result = _process_with_backoff(path, config, sleep=sleep)
        if result.status == "uploaded":
            uploaded += 1
        elif result.status in ("skipped", "no_turns"):
            skipped += 1
        else:
            failed += 1
        # R5 throttle: pace under the limiter; no need to sleep after the final session.
        if i < last:
            sleep(THROTTLE_SECONDS)

    print(f"Imported {uploaded} sessions ({skipped} already captured).")
    print("Terum will finish processing them over the next day or so.")
    if failed:
        print(
            f"{failed} session(s) could not be uploaded. "
            f"Re-run 'terum-capture backfill' to retry them."
        )
