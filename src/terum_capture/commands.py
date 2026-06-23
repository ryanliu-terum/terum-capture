import json
import os
import secrets
import socket
import sys
import webbrowser
from pathlib import Path

import httpx

from terum_capture.config import load_config, save_config, delete_config, CallbackServer

DEFAULT_API_URL = "https://api.terum.ai/api"
DASHBOARD_URL = "https://app.terum.ai"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"

HOOK_TIMEOUT = 15

# Substrings that identify our Stop hook across versions, so we can detect,
# migrate, and remove either form:
#   - "terum_capture upload" — the current python-routed command (`-m terum_capture upload`)
#   - "terum-capture upload" — the legacy bare console-script shim
# (The pipx venv path contains "terum-capture" but never "terum-capture upload",
# so it can't false-match.)
HOOK_UPLOAD_MARKERS = ("terum_capture upload", "terum-capture upload")


def _hook_command() -> str:
    """The Stop-hook command, routed through the signed Python interpreter.

    At `terum-capture setup` time this process IS running inside the interpreter
    that has the package installed (the pipx venv python, or the user's python).
    That interpreter is code-signed (e.g. PSF Authenticode), so Windows Smart App
    Control / WDAC permit it — unlike the unsigned pip/pipx console-script `.exe`
    shim, which they block on enforcing machines ("An Application Control policy
    has blocked this file"), silently killing capture every session.

    `Path.as_posix()` yields a forward-slash, drive-letter path that the Git Bash
    which runs Claude Code hooks on Windows executes correctly; it's a no-op on
    POSIX. The path is quoted to survive spaces (e.g. "C:/Users/Jane Doe/...").
    """
    python = Path(sys.executable).as_posix()
    return f'"{python}" -m terum_capture upload'


def _hook_entry() -> dict:
    return {"type": "command", "command": _hook_command(), "timeout": HOOK_TIMEOUT}


CLAUDE_MD_BLOCK = """
## Terum Knowledge Capture

After completing a task that involved technical decisions, problem-solving, debugging,
or building something non-trivial, end your response with a brief summary:
what you did, what files you changed, and any decisions you made.
One paragraph, plain text — no markdown formatting, no bullet lists, no code blocks.

This summary is captured automatically — you do not need to call any tools.
If you forget the summary, the conversation is still captured; the summary just
makes the captured data richer.
"""


def cmd_setup(api_url: str | None = None, token: str | None = None):
    api_url = api_url or DEFAULT_API_URL

    config = load_config()
    if config and config.get("api_key", "").startswith("trm_"):
        try:
            resp = httpx.get(
                f"{config['api_url']}/keys/me",
                headers={"Authorization": f"Bearer {config['api_key']}"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                prefix = config["api_key"][:8]
                print(f"You already have a valid Terum key ({prefix}...).")
                print("Creating a new key will not revoke the existing one.")
                answer = input("Continue? [y/N] ").strip().lower()
                if answer != "y":
                    return
        except Exception:
            pass

    if not token:
        token = _browser_auth(api_url)
        if not token:
            return

    hostname = socket.gethostname() or "unknown"
    try:
        resp = httpx.post(
            f"{api_url}/keys",
            json={"name": hostname},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
    except Exception as exc:
        print(f"Error: Could not reach {api_url}: {exc}")
        return

    if resp.status_code == 409:
        print("Error: You have 10 active keys. Revoke one first.")
        return
    if resp.status_code == 401:
        print("Error: Token expired or invalid. Run setup again.")
        return
    if resp.status_code != 201:
        print(f"Error: Key creation failed (HTTP {resp.status_code}).")
        return

    data = resp.json()
    api_key = data["key"]
    save_config(api_key, api_url)

    try:
        verify = httpx.get(
            f"{api_url}/keys/me",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
        if verify.status_code != 200:
            delete_config()
            print("Error: Round-trip verification failed. Config deleted.")
            return
    except Exception:
        delete_config()
        print("Error: Round-trip verification failed. Config deleted.")
        return

    _configure_hook()
    _append_claude_md()

    prefix = api_key[:8]
    print(f"\nTerum connected! Key: {prefix}...")
    print("\nClaude Code hook configured — your sessions will be captured automatically.")
    print("A summary instruction was added to ~/.claude/CLAUDE.md.")
    print("\nNo further setup needed. Start a new Claude Code session to begin capturing.")


def _browser_auth(api_url: str) -> str | None:
    state = secrets.token_urlsafe(32)
    server = CallbackServer()
    port = server.start()
    if port is None:
        return None

    url = f"{DASHBOARD_URL}/auth/mcp-setup?port={port}&state={state}"
    print(f"Opening browser for authentication...")
    if not webbrowser.open(url):
        print(f"Could not open browser. Visit this URL:\n  {url}")

    result = server.wait_for_callback(expected_state=state)
    if result is None:
        return None
    return result.get("token")


def _is_our_command(command: object) -> bool:
    return isinstance(command, str) and any(m in command for m in HOOK_UPLOAD_MARKERS)


def _is_our_stop_entry(entry: dict) -> bool:
    """True if a Stop-hooks list entry is the terum-capture upload hook.

    Matches the current python-routed command (`"<python>" -m terum_capture upload`)
    AND the legacy bare-shim command (`terum-capture upload`), so we can detect,
    migrate, and remove either. Handles both the current matcher-group shape
    ({"hooks": [{"command": ...}]}) and the legacy flat shape ({"command": ...}).
    """
    if not isinstance(entry, dict):
        return False
    inner = entry.get("hooks")
    if isinstance(inner, list):
        return any(isinstance(h, dict) and _is_our_command(h.get("command")) for h in inner)
    return _is_our_command(entry.get("command"))


def _configure_hook():
    try:
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        settings: dict = {}
        if CLAUDE_SETTINGS.exists():
            settings = json.loads(CLAUDE_SETTINGS.read_text())

        hooks = settings.setdefault("hooks", {})
        stop_hooks = hooks.setdefault("Stop", [])

        # Claude Code expects each Stop entry to be a matcher group wrapping a
        # "hooks" array. Keep one copy normalized to the canonical group shape +
        # current python-routed command (migrating the legacy flat shape and the
        # legacy bare-shim command in place), and drop accidental duplicates.
        canonical = {"hooks": [_hook_entry()]}
        new_stop_hooks = []
        already_present = False
        changed = False
        for entry in stop_hooks:
            if _is_our_stop_entry(entry):
                if already_present:
                    changed = True  # drop duplicate
                    continue
                already_present = True
                if entry != canonical:
                    entry = canonical  # migrate shape and/or refresh command
                    changed = True
            new_stop_hooks.append(entry)

        if not already_present:
            new_stop_hooks.append(canonical)
            changed = True

        if changed:
            hooks["Stop"] = new_stop_hooks
            CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    except Exception as exc:
        print(f"Warning: Could not configure hook: {exc}")


def _append_claude_md():
    try:
        CLAUDE_MD.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if CLAUDE_MD.exists():
            existing = CLAUDE_MD.read_text()

        if "## Terum Knowledge Capture" in existing:
            return

        with open(CLAUDE_MD, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(CLAUDE_MD_BLOCK)
    except Exception as exc:
        print(f"Warning: Could not update CLAUDE.md: {exc}")


def _remove_hook():
    try:
        if not CLAUDE_SETTINGS.exists():
            return
        settings = json.loads(CLAUDE_SETTINGS.read_text())
        hooks = settings.get("hooks", {})
        stop_hooks = hooks.get("Stop", [])
        hooks["Stop"] = [h for h in stop_hooks if not _is_our_stop_entry(h)]
        if not hooks["Stop"]:
            del hooks["Stop"]
        if not hooks:
            del settings["hooks"]
        CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    except Exception:
        pass


def cmd_status():
    config = load_config()
    if not config or not config.get("api_key"):
        print("Not configured. Run: terum-capture setup")
        sys.exit(1)

    prefix = config["api_key"][:8]
    print(f"Key: {prefix}...")
    print(f"API: {config.get('api_url', 'not set')}")

    try:
        resp = httpx.get(
            f"{config['api_url']}/keys/me",
            headers={"Authorization": f"Bearer {config['api_key']}"},
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            print(f"Status: connected")
            print(f"Name: {data.get('name', 'unknown')}")
            if data.get("last_used_at"):
                print(f"Last used: {data['last_used_at']}")
        else:
            print(f"Status: invalid or revoked (HTTP {resp.status_code})")
            sys.exit(1)
    except Exception as exc:
        print(f"Status: unreachable ({exc})")
        sys.exit(1)


def cmd_logout():
    config = load_config()
    if not config:
        print("Not configured — nothing to do.")
        return

    print("Warning: This removes your local config but does NOT revoke the API key.")
    print("The key will remain active until revoked from the dashboard.")
    answer = input("Continue? [y/N] ").strip().lower()
    if answer != "y":
        return

    delete_config()
    _remove_hook()
    print("Config removed and hook uninstalled.")
