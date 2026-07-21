import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path

import httpx

from terum_capture import __version__
from terum_capture.config import load_config, save_config, delete_config, CallbackServer

DEFAULT_API_URL = "https://api.terum.ai/api"
DASHBOARD_URL = "https://app.terum.ai"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"
CLAUDE_JSON = Path.home() / ".claude.json"
CURSOR_MCP = Path.home() / ".cursor" / "mcp.json"
MCP_SERVER_NAME = "terum"

# 60s (not Claude Code's 15s default): a big session's first upload does a
# whole-file read + git before the POST; 15s could kill it mid-work, and a
# marker is only written on success, so the retry re-pays the same cost and the
# session never captures (bug-416). _configure_hook refreshes installed users to
# this value on their next `setup`.
HOOK_TIMEOUT = 60

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

# Idempotency key for the MCP-usage guidance below. Delivery through the Terum MCP server is
# PULL-ONLY — nothing is injected automatically, so the tools only help if the agent actually
# reaches for them. Without this instruction the model rarely calls an optional MCP tool, so
# "in-flow team context" silently never fires. Appended only when MCP is actually wired for
# Claude Code (see _configure_mcp call sites); Cursor has no ~/.claude/CLAUDE.md.
MCP_USAGE_HEADER = "## Terum Team Knowledge (MCP)"

MCP_USAGE_BLOCK = """
## Terum Team Knowledge (MCP)

Your team's shared knowledge and standing decisions are reachable through the Terum MCP
server, via three tools: `search_team_knowledge`, `check_decision`, `get_standing_decisions`.
Delivery is pull-only — nothing is injected automatically, so these only help the team if you
actually call them at the right moments:

- Before proposing or making a non-trivial technical decision — an architecture choice, a
  library / API / schema / auth decision, or a destructive or hard-to-reverse action — call
  `check_decision` with the specific action you're about to take. It surfaces conflicts with
  the team's standing decisions and is silent when nothing conflicts.
- When you start work in an unfamiliar area, or before answering a question that assumes team
  context, call `search_team_knowledge` first to find what the team already decided or discussed.
- Call `get_standing_decisions` to catch up on the team's recent decisions before proposing
  something new.

This is separate from Terum's automatic session capture, which needs no tool calls: capture
records your work for the team, while these knowledge tools are yours to call to pull the
team's context into your work.
"""


def cmd_setup(api_url: str | None = None, token: str | None = None, mcp: bool | None = None):
    api_url = api_url or DEFAULT_API_URL
    # Whether a token was passed non-interactively (--token). Captured before
    # _browser_auth reassigns `token`, so the backfill auto-prompt can stay TTY-gated.
    token_supplied = token is not None

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

    _maybe_configure_mcp_interactive(api_key, api_url, mcp)

    prefix = api_key[:8]
    print(f"\nTerum connected! Key: {prefix}...")
    print("\nClaude Code hook configured — your sessions will be captured automatically.")
    print("A summary instruction was added to ~/.claude/CLAUDE.md.")
    print("\nNo further setup needed. Start a new Claude Code session to begin capturing.")

    _maybe_offer_backfill(interactive=not token_supplied and sys.stdin.isatty())


def cmd_setup_hook():
    """Refresh ONLY the Stop-hook config to this installed package's canonical form.

    Unlike ``setup``, this mints no API key and prompts for nothing — it just rewrites
    the terum hook entry in ~/.claude/settings.json to the current command + timeout.
    Used by ``update`` (after the reinstall) and safe to run anytime to repair hook drift.
    """
    _configure_hook()
    print(
        f"terum-capture {__version__}: Stop hook refreshed (timeout {HOOK_TIMEOUT}s). "
        "Restart any open Claude Code sessions to load it."
    )


def _maybe_offer_backfill(interactive: bool):
    """Offer to import past sessions after connecting (spec Δ4).

    Interactive setup confirms (default-yes) before importing; non-interactive setup
    (--token / no TTY) just prints the one-liner so an automated install never blocks
    on input and never silently runs a large upload the operator didn't ask for.
    """
    from terum_capture.backfill import cmd_backfill, discover_sessions

    if not interactive:
        print("\nRun 'terum-capture backfill' anytime to import your past Claude Code sessions.")
        return

    n = len(discover_sessions())  # default 30-day window, discovery only — no POSTs
    if n == 0:
        print("\nRun 'terum-capture backfill' anytime to import your past Claude Code sessions.")
        return

    answer = input(
        f"\nFound {n} Claude Code sessions from the last 30 days. Import them now? [Y/n] "
    ).strip().lower()
    if answer in ("", "y", "yes"):
        cmd_backfill()
    else:
        print("Run 'terum-capture backfill' anytime to import your past sessions.")


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


def _append_mcp_usage_claude_md():
    """Append MCP-usage guidance to ~/.claude/CLAUDE.md so the agent knows WHEN to call the
    Terum MCP tools. Idempotent (keyed on MCP_USAGE_HEADER), append-only, and warn-don't-crash —
    mirrors _append_claude_md exactly. Claude Code only; called after MCP is wired for `claude`."""
    try:
        CLAUDE_MD.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if CLAUDE_MD.exists():
            existing = CLAUDE_MD.read_text()

        if MCP_USAGE_HEADER in existing:
            return

        with open(CLAUDE_MD, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(MCP_USAGE_BLOCK)
    except Exception as exc:
        print(f"Warning: Could not update CLAUDE.md with MCP guidance: {exc}")


def _maybe_configure_mcp_interactive(api_key: str, api_url: str, choice: bool | None) -> None:
    """Tier 1: the opt-in MCP prompt at the end of `cmd_setup`.

    `choice` is a tri-state (wired in from CLI flags, see cli.py):
      None  -> interactive: ask only if running on a TTY, skip silently otherwise.
      True  -> forced yes (headless opt-in via --mcp): install without prompting.
      False -> forced skip: do nothing.
    Never raises — a headless EOFError/KeyboardInterrupt on input() is treated as skip.
    """
    if choice is False:
        return

    if choice is None:
        if not sys.stdin.isatty():
            return
        try:
            answer = input(
                "Also connect Claude Code to your team's shared decisions & "
                "conflict-checks (read-only)? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if answer in ("n", "no"):
            return

    result = _configure_mcp(api_key, api_url, client="claude")
    if result == "installed":
        _append_mcp_usage_claude_md()
        print("MCP connected — your agent can now pull team decisions & run conflict checks.")
    elif result == "already":
        _append_mcp_usage_claude_md()
        print("MCP already configured — left it as-is.")
    else:
        print("Could not auto-configure MCP. Run 'terum-capture mcp install' later.")


def cmd_mcp_install(client: str = "claude"):
    config = load_config()
    if not config or not config.get("api_key"):
        print("Not configured. Run: terum-capture setup")
        sys.exit(1)

    result = _configure_mcp(config["api_key"], config.get("api_url", DEFAULT_API_URL), client=client)

    label = "Cursor" if client == "cursor" else "Claude Code"
    # The usage guidance lives in ~/.claude/CLAUDE.md — Claude Code only (Cursor has no such file).
    if client == "claude" and result in ("installed", "already"):
        _append_mcp_usage_claude_md()
    if result == "installed":
        print(f"MCP connected for {label}.")
    elif result == "already":
        print(f"MCP already configured for {label} — left it as-is.")
    else:
        print(f"Could not configure MCP for {label}.")
        sys.exit(1)


def _configure_mcp(api_key: str, api_url: str, client: str = "claude") -> str:
    """Wire an MCP server named "terum" pointing at {api_url}/mcp, authed with api_key.
    Returns one of: "installed" | "already" | "failed". Never raises."""
    mcp_url = f"{api_url.rstrip('/')}/mcp"

    if client == "claude":
        return _configure_mcp_claude(api_key, mcp_url)
    if client == "cursor":
        return _configure_mcp_cursor(api_key, mcp_url)

    print(f"Error: unknown MCP client '{client}'.")
    return "failed"


def _configure_mcp_claude(api_key: str, mcp_url: str) -> str:
    existing_config, parseable = _read_json_config(CLAUDE_JSON)
    existing_mcp_servers = existing_config.get("mcpServers") if isinstance(existing_config, dict) else None
    if parseable and isinstance(existing_mcp_servers, dict) and MCP_SERVER_NAME in existing_mcp_servers:
        return "already"

    if shutil.which("claude"):
        try:
            result = subprocess.run(
                ["claude", "mcp", "add", "--transport", "http", MCP_SERVER_NAME, mcp_url,
                 "--header", f"Authorization: Bearer {api_key}", "--scope", "user"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                return "installed"
        except Exception:
            pass

    entry = {"type": "http", "url": mcp_url, "headers": {"Authorization": f"Bearer {api_key}"}}
    return _write_mcp_entry(CLAUDE_JSON, entry)


def _configure_mcp_cursor(api_key: str, mcp_url: str) -> str:
    entry = {"url": mcp_url, "headers": {"Authorization": f"Bearer {api_key}"}}
    return _write_mcp_entry(CURSOR_MCP, entry)


def _read_json_config(path: Path) -> tuple[dict, bool]:
    """Returns (config_dict, parseable). parseable=False means the file exists but is
    not valid JSON — callers must not overwrite it in that case."""
    if not path.exists():
        return {}, True
    try:
        return json.loads(path.read_text()), True
    except Exception:
        return {}, False


def _write_mcp_entry(path: Path, entry: dict) -> str:
    try:
        did_exist = path.exists()
        config, parseable = _read_json_config(path)
        if not parseable:
            raise ValueError(f"{path} exists but is not valid JSON")

        mcp_servers = config.get("mcpServers")
        if not isinstance(mcp_servers, dict):
            mcp_servers = {}
            config["mcpServers"] = mcp_servers
        if MCP_SERVER_NAME in mcp_servers:
            return "already"

        mcp_servers[MCP_SERVER_NAME] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n")
        if not did_exist and sys.platform != "win32":
            os.chmod(path, 0o600)
        return "installed"
    except Exception as exc:
        print(f"Warning: Could not configure MCP: {exc}")
        return "failed"


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
    if not config or not config.get("api_key") or not config.get("api_url"):
        # api_url is required below (config['api_url'] for the /keys/me probe); a
        # config missing it is "not configured", not a crash.
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
            print(f"Version: {__version__}")
            print(f"Name: {data.get('name', 'unknown')}")
            if data.get("last_used_at"):
                print(f"Last used: {data['last_used_at']}")
            from terum_capture.maintenance import read_update_available
            pending = read_update_available()
            if pending:
                print(
                    f"Update available: {pending} — run 'terum-capture update' "
                    f"(you have {__version__})."
                )
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
