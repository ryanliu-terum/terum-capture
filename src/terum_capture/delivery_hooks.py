"""Tier 3 delivery hook — guaranteed in-flow team context on every prompt.

ONE hook (UserPromptSubmit) makes delivery happen WITHOUT the agent choosing to call an MCP
tool: it sends the user's prompt to Terum's POST /api/hooks/retrieve (the non-MCP-protocol
sibling of the MCP tools — Terum-MVP #305) and injects the top team-knowledge results into
the session via `hookSpecificOutput.additionalContext`, BEFORE the model works.

The injected block also periodically carries a SELF-CHECK instruction telling the agent to
run `check_decision` on its own non-trivial mid-session decisions. That recovers the
approach-level conflict lane through the front door: the agent's self-phrased decision
sentence is the input shape the server's conflict floor is tuned for — unlike the mechanical
tool-arg statements ("modify auth.ts") the withdrawn PreToolUse variant could send, which
essentially never clear it (Terum-MVP .planning/2026-07-29-tier3-inflow-delivery-assessment.md;
the PreToolUse conflict hook was removed from this PR pending that redesign). The instruction
is DOSED — first prompt of a session, then every INSTRUCTION_EVERY_N-th — because a verbatim
every-turn reminder decays into wallpaper (measured on the intent-check hook precedent).

FAIL-OPEN BY CONSTRUCTION. Any error — no config, unreachable backend, timeout, bad payload,
unexpected shape, unwritable state file — degrades to injecting less (or nothing) and exits 0.
A delivery hook must NEVER break the user's session or block their prompt. This mirrors the
injection-pipeline principle: on failure, inject nothing. Deliberate, not an oversight.

ECHO-LOOP GUARD CONTRACT: every injected line starts with one of the [Terum ...] markers
below. upload.py strips marker blocks out of captured prompts before upload, so injected team
context is never re-captured and re-distilled as if THIS user decided it. Change the markers
and you must change upload.py's strip in the same commit.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from terum_capture.config import CONFIG_DIR, load_config
from terum_capture.output import die

# Under Claude Code's 30s UserPromptSubmit budget with margin; fail-open past it (inject nothing).
# 15.0 is decision-walk 2026-08-02 D1 (bug-592): prod mode="conflict" answers in 7.6-8.9s, so the
# old 8.0 budget meant the conflict lane timed out and NEVER fired end-to-end. httpx float timeout
# is per-operation (connect/read/write each), not wall-clock; the installed hook's 30s entry and
# Claude Code's own budget are the hard backstop, and a killed hook still fails open.
HOOK_HTTP_TIMEOUT = 15.0

# Substring that identifies our delivery-hook command in settings.json, for idempotent install/remove.
DELIVERY_HOOK_MARKER = "terum_capture delivery-hook"

# Prompts shorter than this skip retrieval — "yes"/"continue" shouldn't pay an embed+search
# round-trip of latency. (They still advance the instruction dose counter.)
MIN_PROMPT_CHARS = 20

_MAX_ITEMS = 5
_MAX_ITEM_CHARS = 220

# Echo-loop guard markers (see module docstring — upload.py strips blocks led by these).
CONTEXT_MARKER = "[Terum team context]"
REMINDER_MARKER = "[Terum reminder]"
DECISION_MARKER = "[Terum decision check]"

INSTRUCTION_EVERY_N = 5
# Single line by construction: upload.py's strip drops marker-LED lines, so an internal
# newline would leak the tail of the instruction into the captured prompt.
SELF_CHECK_INSTRUCTION = (
    f"{REMINDER_MARKER} As you work: before any architecture, library, schema, billing, or "
    "build-vs-buy decision you make this session — and before scaffolding any new service or "
    "module — state it in plain language and call the check_decision MCP tool with it, BEFORE "
    "writing any code. If it returns a standing decision that genuinely conflicts: STOP, do "
    "not write code — present the conflict (the decision, who made it, when, and the "
    "reasoning), then ask how to proceed. If no candidate genuinely conflicts, say nothing "
    "about the check. Call search_team_knowledge when you need what the team already knows, "
    "discussed, or decided — the repo alone won't have it."
)

# Conflict-lane preamble — NEUTRAL BY DESIGN. Live probes (2026-08-01) show true conflict
# prompts score 0.62-0.67 similarity, but ALIGNED prompts (agreeing with a decision) also
# surface their decision at ~0.55-0.58 — so a candidate is a MAYBE, never a verdict. The
# wording instructs the model to judge silently, never asserts a conflict exists. One line
# by construction (same strip rationale as SELF_CHECK_INSTRUCTION).
CONFLICT_PREAMBLE = (
    f"{DECISION_MARKER} A standing team decision may bear on this request — judge silently "
    "whether what the user is asking conflicts with it. If it genuinely conflicts: STOP "
    "before writing code, surface the decision (who made it, when, why), and ask how to "
    "proceed. If it aligns or is unrelated, proceed and do not mention this check."
)
_MAX_CONFLICT_ITEMS = 3

# Per-session prompt counter for instruction dosing. Insertion-ordered dict, pruned to the
# newest _STATE_MAX_SESSIONS entries so the file can't grow unbounded.
STATE_FILE = CONFIG_DIR / "delivery_state.json"
_STATE_MAX_SESSIONS = 50


def _routed_command(subcommand: str) -> str:
    """python-routed hook command, matching commands._hook_command's signed-interpreter rationale
    (Windows Smart App Control blocks the unsigned console-script .exe; sys.executable is signed)."""
    python = Path(sys.executable).as_posix()
    return f'"{python}" -m terum_capture {subcommand}'


def _read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _retrieve(mode: str, text: str) -> dict | None:
    """POST to /hooks/retrieve. Returns the parsed JSON body, or None on ANY failure (fail-open)."""
    text = (text or "").strip()
    if not text:
        return None
    config = load_config()
    if not config or not config.get("api_key") or not config.get("api_url"):
        return None
    try:
        resp = httpx.post(
            f"{config['api_url']}/hooks/retrieve",
            json={"mode": mode, "text": text, "source": "hook"},
            headers={"Authorization": f"Bearer {config['api_key']}"},
            timeout=HOOK_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
        return body if isinstance(body, dict) else None
    except Exception:
        return None


def _emit(event_name: str, additional_context: str | None) -> None:
    """Print the hook JSON that injects additional_context — or nothing at all (fail-open)."""
    if not additional_context:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": additional_context,
        }
    }))


def _format_context(output: dict | None) -> str | None:
    """Bullets from /hooks/retrieve context results. Field names pinned against the live
    SearchKnowledgeOutput shape (Terum-MVP lib/mcp/search.ts): results[].summary/topic/owner."""
    if not output:
        return None
    results = output.get("results")
    if not isinstance(results, list):
        return None  # bug-594: a truthy non-list (e.g. {"results": 1}) must fail open, not raise
    lines: list[str] = []
    for item in results[:_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        # Flatten to ONE line: prod summaries are multi-line markdown, and a bullet spanning
        # lines both garbles the block and breaks upload.py's strip (its block-scan ends at the
        # first non-"- " line). Found live in the 2026-07-29 E2E probe.
        text = " ".join(str(item.get("summary") or item.get("topic") or "").split())
        if not text:
            continue
        owner = str(item.get("owner") or "").strip()
        line = f"- {text[:_MAX_ITEM_CHARS]}"
        if owner:
            line += f" ({owner})"
        lines.append(line)
    if not lines:
        return None
    return (
        f"{CONTEXT_MARKER} Your team already discussed/decided these — use if helpful, "
        "ignore if not:\n" + "\n".join(lines)
    )


def _format_conflict(output: dict | None) -> str | None:
    """[Terum decision check] block from a mode="conflict" /hooks/retrieve response.

    Field names pinned against the live conflict shape: candidates[].decision_text/author/
    decided_at/similarity, plus a top-level `error`. Any error in the body — or no usable
    candidates — returns None (fail-open, inject nothing). Every emitted line starts with
    DECISION_MARKER (echo-loop guard contract; upload.py strips these lines in the same
    commit that introduced the marker).
    """
    if not output or output.get("error"):
        return None
    raw = output.get("candidates")
    if not isinstance(raw, list):
        return None  # bug-594: a truthy non-list (e.g. {"candidates": 1}) must fail open, not raise
    candidates = [c for c in raw if isinstance(c, dict)]
    if not candidates:
        return None

    def _sim(cand: dict) -> float:
        try:
            return float(cand.get("similarity") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    lines = [CONFLICT_PREAMBLE]
    for cand in sorted(candidates, key=_sim, reverse=True)[:_MAX_CONFLICT_ITEMS]:
        # Flatten to ONE line — same rationale as _format_context: a line not led by the
        # marker would survive upload.py's strip and be re-captured as the user's words.
        text = " ".join(str(cand.get("decision_text") or "").split())
        if not text:
            continue
        # Flattened like decision_text (bug-593): .strip() alone keeps INTERIOR newlines, and
        # an embedded newline in either field would put part of this entry on a physical line
        # not led by the marker — which upload.py's strip would then keep and upload as the
        # user's own words.
        author = " ".join(str(cand.get("author") or "").split()) or "unknown"
        decided = " ".join(str(cand.get("decided_at") or "").split())[:10] or "date unknown"
        lines.append(f"{DECISION_MARKER} Decision ({author}, {decided}): {text[:_MAX_ITEM_CHARS]}")
    if len(lines) == 1:
        return None  # candidates present but none had usable text
    return "\n".join(lines)


def _instruction_due(session_id: str) -> bool:
    """Advance this session's prompt counter; True on the 1st prompt and every
    INSTRUCTION_EVERY_N-th after. Any state failure -> False (fail-open = inject less)."""
    if not session_id:
        return False
    try:
        state: dict = {}
        if STATE_FILE.exists():
            raw = json.loads(STATE_FILE.read_text())
            if isinstance(raw, dict):
                state = raw
        prev = state.pop(session_id, None)  # pop+reinsert keeps active sessions newest
        count = prev + 1 if isinstance(prev, int) else 1
        state[session_id] = count
        if len(state) > _STATE_MAX_SESSIONS:
            for key in list(state)[: len(state) - _STATE_MAX_SESSIONS]:
                del state[key]
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state))
        return count == 1 or count % INSTRUCTION_EVERY_N == 0
    except Exception:
        return False


def run_prompt_hook() -> None:
    """UserPromptSubmit entry point (invoked as `... delivery-hook prompt`).

    The try/except makes the module's FAIL-OPEN guarantee structural rather than
    per-helper (bug-594): any unexpected defect in a helper degrades to injecting
    nothing, mirroring cmd_upload's guard around _do_upload.
    """
    try:
        config = load_config()
        if not config or not config.get("api_key") or not config.get("api_url"):
            return  # not set up -> nothing to retrieve AND the MCP tools aren't wired; stay silent
        payload = _read_stdin_json()
        prompt = str(payload.get("prompt") or payload.get("user_prompt") or "")
        session_id = str(payload.get("session_id") or "")

        parts: list[str] = []
        if len(prompt.strip()) >= MIN_PROMPT_CHARS:
            # Two PARALLEL, INDEPENDENT retrievals: the context and conflict lanes (decision-walk
            # 2026-08-02 D1, bug-592 — sequential lanes stacked their latencies, so the prompt
            # paid both round-trips back to back). Each still fails open on its own (_retrieve
            # returns None on any error, including a timeout), so a transient failure in one
            # never suppresses the other — ~2/9 live calls errored in the 2026-08-01 probes.
            # Injection order stays context-then-conflict regardless of which lane answers first.
            with ThreadPoolExecutor(max_workers=2) as pool:
                context_future = pool.submit(_retrieve, "context", prompt)
                conflict_future = pool.submit(_retrieve, "conflict", prompt)
                context = _format_context(context_future.result())
                conflict = _format_conflict(conflict_future.result())
            if context:
                parts.append(context)
            if conflict:
                parts.append(conflict)
        if _instruction_due(session_id):
            parts.append(SELF_CHECK_INSTRUCTION)
        _emit("UserPromptSubmit", "\n\n".join(parts) if parts else None)
    except Exception as exc:  # fail-open: a delivery hook must never break the user's session
        print(f"terum-capture: delivery hook degraded (injected nothing): {exc}", file=sys.stderr)


# --- install / uninstall in ~/.claude/settings.json -------------------------------------------

def _delivery_entry(subcommand: str, timeout: int) -> dict:
    return {"matcher": "*", "hooks": [{"type": "command", "command": _routed_command(subcommand), "timeout": timeout}]}


def _is_our_delivery_group(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    inner = entry.get("hooks")
    if not isinstance(inner, list):
        return False
    return any(isinstance(h, dict) and DELIVERY_HOOK_MARKER in str(h.get("command", "")) for h in inner)


# PreToolUse appears ONLY in the sweep list: the withdrawn draft installed one, so refresh and
# uninstall both clean it up, but install never wires it (prompt-lane-only per the assessment).
_SWEEP_EVENTS = ("UserPromptSubmit", "PreToolUse")


def install_delivery_hooks() -> None:
    """Idempotent read-modify-write of ~/.claude/settings.json adding the prompt delivery hook.
    Mirrors commands._configure_hook's discipline (never clobber, warn-don't-crash, dedupe)."""
    from terum_capture.commands import CLAUDE_SETTINGS
    try:
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        settings: dict = {}
        if CLAUDE_SETTINGS.exists():
            settings = json.loads(CLAUDE_SETTINGS.read_text())
        hooks = settings.setdefault("hooks", {})
        for event in _SWEEP_EVENTS:  # drop our old copies (refresh; incl. a stale draft PreToolUse)
            if event in hooks:
                hooks[event] = [e for e in hooks[event] if not _is_our_delivery_group(e)]
                if not hooks[event]:
                    del hooks[event]
        entry = _delivery_entry("delivery-hook prompt", 30)
        hooks.setdefault("UserPromptSubmit", []).append(entry)
        CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
        # Echo the RESOLVED command, not just "installed". _routed_command bakes in
        # sys.executable, so whichever interpreter ran the install is frozen into the hook
        # forever — and if that is a dev checkout's editable venv, the hook silently dies the
        # next time that tree changes branch (bug-559: 8 commits of drift, every prompt erroring,
        # in-flow delivery never firing). Printing the path is what makes a wrong binding visible
        # while the user is still standing here. Printed from inside install so the SILENT
        # self-heal path (setup-hook / update -> _refresh_delivery_hook_if_installed) reports it
        # too — that path re-freezes the interpreter, so it is exactly where a bad one hides.
        print(f"Delivery hook command: {entry['hooks'][0]['command']}")
    except Exception as exc:
        print(f"Warning: Could not install delivery hooks: {exc}")


def uninstall_delivery_hooks() -> None:
    from terum_capture.commands import CLAUDE_SETTINGS
    try:
        if not CLAUDE_SETTINGS.exists():
            return
        settings = json.loads(CLAUDE_SETTINGS.read_text())
        hooks = settings.get("hooks", {})
        for event in _SWEEP_EVENTS:
            if event in hooks:
                hooks[event] = [e for e in hooks[event] if not _is_our_delivery_group(e)]
                if not hooks[event]:
                    del hooks[event]
        if not hooks:
            settings.pop("hooks", None)
        CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    except Exception:
        pass


def cmd_delivery(action: str) -> None:
    """`terum-capture delivery <install|uninstall>` — wire/unwire the prompt delivery hook."""
    if action == "install":
        config = load_config()
        if not config or not config.get("api_key"):
            die("Not configured. Run: terum-capture setup")
        install_delivery_hooks()
        print("Delivery hook installed (UserPromptSubmit). "
              "Restart any open Claude Code sessions to load it.")
    elif action == "uninstall":
        uninstall_delivery_hooks()
        print("Delivery hooks removed.")
    else:
        die("Usage: terum-capture delivery <install|uninstall>")
