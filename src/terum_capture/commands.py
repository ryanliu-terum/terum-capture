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

HOOK_ENTRY = {
    "type": "command",
    "command": "terum-capture upload",
    "timeout": 15,
}

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


def _configure_hook():
    try:
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        settings: dict = {}
        if CLAUDE_SETTINGS.exists():
            settings = json.loads(CLAUDE_SETTINGS.read_text())

        hooks = settings.setdefault("hooks", {})
        stop_hooks = hooks.setdefault("Stop", [])

        for hook in stop_hooks:
            if isinstance(hook, dict) and hook.get("command") == "terum-capture upload":
                return

        stop_hooks.append(HOOK_ENTRY)
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
        hooks["Stop"] = [
            h for h in stop_hooks
            if not (isinstance(h, dict) and h.get("command") == "terum-capture upload")
        ]
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
