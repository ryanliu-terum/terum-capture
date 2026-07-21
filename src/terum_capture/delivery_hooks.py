"""Tier 3 (DRAFT): Claude Code delivery hooks — guaranteed in-flow team context.

Two hooks make delivery happen WITHOUT the agent choosing to call an MCP tool:

  - UserPromptSubmit -> run_prompt_hook()      : injects relevant team knowledge for the
    user's prompt, BEFORE the model works (pre-work enrichment). Anchored to the prompt.
  - PreToolUse       -> run_pretooluse_hook()  : surfaces standing-decision conflicts for the
    action the agent is about to take, BEFORE the tool runs (approach-level guardrail).

Both read Claude Code's hook payload on stdin, call Terum's POST /api/hooks/retrieve (the
non-MCP-protocol sibling of the MCP tools), and print a hook-JSON response on stdout using
`hookSpecificOutput.additionalContext` (verified against the Claude Code hooks contract).

FAIL-OPEN BY CONSTRUCTION. Any error — no config, unreachable backend, timeout, bad payload,
unexpected shape — prints nothing and exits 0. A delivery hook must NEVER break the user's
session or block their prompt/tool call. This mirrors the injection-pipeline principle: on
failure, inject nothing. That is a deliberate design choice, not an oversight.

DRAFT: the /hooks/retrieve response SHAPE assumed by the formatters below is provisional and
must be pinned against the finalized endpoint (Terum-MVP PR #305) before this is de-drafted.
"""
import json
import sys
from pathlib import Path

import httpx

from terum_capture.config import load_config

# Under Claude Code's 30s UserPromptSubmit budget with margin; fail-open past it (inject nothing).
HOOK_HTTP_TIMEOUT = 8.0

# Substring that identifies our delivery-hook command in settings.json, for idempotent install/remove.
DELIVERY_HOOK_MARKER = "terum_capture delivery-hook"

_MAX_ITEMS = 5
_MAX_ITEM_CHARS = 220


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
            json={"mode": mode, "text": text},
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


def _bullets(items: list, *fields: str) -> list[str]:
    """Best-effort: for each dict item, take the first non-empty field in `fields`, truncated."""
    out = []
    for item in items[:_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        value = next((str(item[f]) for f in fields if item.get(f)), None)
        if value:
            out.append(f"- {value[:_MAX_ITEM_CHARS]}")
    return out


def _format_context(output: dict | None) -> str | None:
    if not output:
        return None
    lines = _bullets(output.get("results") or [], "summary", "topic")
    if not lines:
        return None
    return (
        "Relevant team context from Terum (your team already discussed/decided these — "
        "use if helpful, ignore if not):\n" + "\n".join(lines)
    )


def _format_conflict(output: dict | None) -> str | None:
    if not output:
        return None
    lines = _bullets(output.get("candidates") or [], "statement", "decision", "text", "summary")
    if not lines:
        return None
    return (
        "Terum: the action you're about to take may touch a standing team decision. If it "
        "contradicts one of these, flag it to the user before proceeding:\n" + "\n".join(lines)
    )


def _statement_from_tool(tool_name: object, tool_input: object) -> str:
    """Render a pending tool call into a plain-language statement to conflict-check. Mechanical
    formatting only (the conflict judgment is server-side embeddings + the model). Unknown tools
    return "" -> no retrieval (fail-open)."""
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "Bash":
        return str(tool_input.get("command") or "")
    if tool_name in ("Edit", "Write", "MultiEdit"):
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        return f"modify {path}" if path else ""
    return ""


def run_prompt_hook() -> None:
    """UserPromptSubmit entry point (invoked as `... delivery-hook prompt`)."""
    payload = _read_stdin_json()
    prompt = payload.get("user_prompt") or payload.get("prompt") or ""
    _emit("UserPromptSubmit", _format_context(_retrieve("context", prompt)))


def run_pretooluse_hook() -> None:
    """PreToolUse entry point (invoked as `... delivery-hook pretooluse`)."""
    payload = _read_stdin_json()
    statement = _statement_from_tool(payload.get("tool_name"), payload.get("tool_input"))
    _emit("PreToolUse", _format_conflict(_retrieve("conflict", statement)))


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


def install_delivery_hooks() -> None:
    """Idempotent read-modify-write of ~/.claude/settings.json adding the two delivery hooks.
    Mirrors commands._configure_hook's discipline (never clobber, warn-don't-crash, dedupe)."""
    from terum_capture.commands import CLAUDE_SETTINGS
    events = {
        "UserPromptSubmit": _delivery_entry("delivery-hook prompt", 30),
        "PreToolUse": _delivery_entry("delivery-hook pretooluse", 15),
    }
    try:
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        settings: dict = {}
        if CLAUDE_SETTINGS.exists():
            settings = json.loads(CLAUDE_SETTINGS.read_text())
        hooks = settings.setdefault("hooks", {})
        for event, canonical in events.items():
            group = hooks.setdefault(event, [])
            kept = [e for e in group if not _is_our_delivery_group(e)]  # drop our old copies (refresh)
            kept.append(canonical)
            hooks[event] = kept
        CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    except Exception as exc:
        print(f"Warning: Could not install delivery hooks: {exc}")


def uninstall_delivery_hooks() -> None:
    from terum_capture.commands import CLAUDE_SETTINGS
    try:
        if not CLAUDE_SETTINGS.exists():
            return
        settings = json.loads(CLAUDE_SETTINGS.read_text())
        hooks = settings.get("hooks", {})
        for event in ("UserPromptSubmit", "PreToolUse"):
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
    """`terum-capture delivery <install|uninstall>` — wire/unwire the two delivery hooks."""
    if action == "install":
        config = load_config()
        if not config or not config.get("api_key"):
            print("Not configured. Run: terum-capture setup")
            sys.exit(1)
        install_delivery_hooks()
        print("Delivery hooks installed (UserPromptSubmit + PreToolUse). "
              "Restart any open Claude Code sessions to load them.")
    elif action == "uninstall":
        uninstall_delivery_hooks()
        print("Delivery hooks removed.")
    else:
        print("Usage: terum-capture delivery <install|uninstall>")
        sys.exit(1)
