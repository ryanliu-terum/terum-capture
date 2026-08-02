import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx

from terum_capture import __version__
from terum_capture.config import load_config, save_config, delete_config, CallbackServer
from terum_capture.output import die, err

DEFAULT_API_URL = "https://api.terum.ai/api"
DASHBOARD_URL = "https://app.terum.ai"
# Global-scope targets, used by `setup --global` / `logout --global`. The default
# (project) scope resolves per-directory targets in _scope_targets().
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

# Files a project-scoped setup keeps out of version control (personal capture config).
GITIGNORE_ENTRIES = (".claude/settings.local.json", "CLAUDE.local.md")


def _scope_targets(use_global: bool, base: Path | None = None) -> tuple[Path, Path]:
    """Resolve (settings_path, claude_md_path) for the chosen install scope.

    Default (project) scope writes personal, git-ignored files in the directory `setup`
    is run from — `.claude/settings.local.json` + `CLAUDE.local.md` — so only that repo's
    Claude Code sessions are captured and nothing is committed. `--global` scope restores
    the machine-wide targets in `~/.claude` (every project is captured).
    """
    if use_global:
        return CLAUDE_SETTINGS, CLAUDE_MD
    base = base or Path.cwd()
    return base / ".claude" / "settings.local.json", base / "CLAUDE.local.md"


def _display_path(path: Path) -> str:
    """A short, friendly form of `path`: ./… under cwd, ~/… under home, else absolute."""
    for root, prefix in ((Path.cwd(), "./"), (Path.home(), "~/")):
        try:
            return prefix + str(path.relative_to(root))
        except ValueError:
            continue
    return str(path)


def _home_path(path: Path) -> str:
    """`~/…` when under home, else the absolute path — stable regardless of cwd.

    Used for the project picker and multi-project summary, where a `./…` form (which
    _display_path prefers) would be confusing for repos that aren't the current directory.
    """
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _relative_time(mtime: float | None, now: float) -> str:
    """Coarse 'Nm/h/d ago' label for the picker; '' when the time is unknown."""
    if not mtime:
        return ""
    secs = max(0.0, now - mtime)
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _parse_selection(raw: str, count: int) -> tuple[str, list[int]]:
    """Parse a picker answer into (kind, 1-based indices).

    kind is one of: 'global' (g), 'all' (a), 'pick' (a valid subset, incl. the empty-Enter
    default of the first row), 'none' (empty answer with nothing to pick), or 'invalid'
    (tokens were given but none resolved — caller re-prompts). Accepts comma/space lists and
    'x-y' ranges, e.g. '1,3' or '1-3 5'. Out-of-range/garbage tokens are dropped.
    """
    s = raw.strip().lower()
    if s == "":
        return ("pick", [1]) if count else ("none", [])
    if s in ("g", "global"):
        return ("global", [])
    if s in ("a", "all"):
        return ("all", list(range(1, count + 1)))

    picked: list[int] = []
    saw_token = False
    for tok in s.replace(",", " ").split():
        saw_token = True
        if "-" in tok:
            lo, _, hi = tok.partition("-")
            if lo.isdigit() and hi.isdigit():
                for n in range(int(lo), int(hi) + 1):
                    if 1 <= n <= count and n not in picked:
                        picked.append(n)
        elif tok.isdigit():
            n = int(tok)
            if 1 <= n <= count and n not in picked:
                picked.append(n)
    if picked:
        return ("pick", picked)
    return ("invalid", []) if saw_token else ("none", [])


def _in_git_repo(base: Path) -> bool:
    """True if `base` (or any ancestor) is a git work tree — a `.git` dir OR file."""
    return any((d / ".git").exists() for d in (base, *base.parents))


def _ensure_gitignore(base: Path) -> bool:
    """Add the project-scoped capture files to `<base>/.gitignore` (idempotent).

    Only touches `.gitignore` inside a git work tree, so a non-repo directory never gets a
    stray file. Presence is tested by exact line match (bare or `/`-anchored form), NOT a
    naive substring scan — otherwise a filename merely mentioned in a comment or a different
    subpath would false-skip the rule and leave the file committable. Returns True only when
    it actually wrote new entries. Honors the "git-ignored" promise so a project-local
    hook/note can't be accidentally committed.
    """
    if not _in_git_repo(base):
        return False
    try:
        gitignore = base / ".gitignore"
        existing = gitignore.read_text() if gitignore.exists() else ""
        present = {ln.strip() for ln in existing.splitlines()}
        missing = [e for e in GITIGNORE_ENTRIES if e not in present and "/" + e not in present]
        if not missing:
            return False
        with open(gitignore, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            if existing:
                f.write("\n")
            f.write("# Terum capture (personal — do not commit)\n")
            f.write("\n".join(missing) + "\n")
        return True
    except OSError:
        return False

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
# Invisible in rendered markdown; bounds the managed span so upserts can replace the block
# without touching anything the user wrote below it. Legacy blocks (pre-marker) are replaced
# conservatively up to the next "## " heading or EOF.
MCP_USAGE_END_MARKER = "<!-- /terum-mcp-usage -->"

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
- When a question is about what's already been decided, discussed, or figured out, by you OR by
  a teammate (e.g. "what did we decide about X", "are we still doing Y", the status of ongoing
  work, a prior finding), call `search_team_knowledge` first. Much of this lives ONLY in the
  team's shared record, not in your repo, so don't answer from a code or file search alone;
  check team knowledge before concluding something isn't written down or doesn't exist.
- Call `get_standing_decisions` to catch up on the team's recent decisions before proposing
  something new.

This is separate from Terum's automatic session capture, which needs no tool calls: capture
records your work for the team, while these knowledge tools are yours to call to pull the
team's context into your work.
<!-- /terum-mcp-usage -->
"""

MAX_PICKER_ROWS = 25


def _prompt_project_scope(cwd: Path) -> tuple[bool, list[Path]]:
    """Interactive scope step: pick which project(s) to capture, or go global.

    Returns (use_global, bases). Lists the machine's known Claude Code projects (newest
    first), always with the current directory as the recommended first row, and accepts any
    subset ('1,3' / '1-3'), 'a' for all listed, or 'g' for global. Re-prompts on garbage.
    """
    from terum_capture.backfill import discover_projects

    now = time.time()

    # Current directory is always row 1 (the default), even with no prior sessions.
    rows: list[dict] = [{"path": cwd, "mtime": None, "sessions": 0, "current": True}]
    seen = {str(cwd)}
    for e in discover_projects():
        key = str(e["path"])
        if key in seen:
            if key == str(cwd):  # fold cwd's history into the current row
                rows[0]["mtime"] = e["mtime"]
                rows[0]["sessions"] = e["sessions"]
            continue
        seen.add(key)
        rows.append({**e, "current": False})

    shown = rows[:MAX_PICKER_ROWS]
    hidden = len(rows) - len(shown)

    print("\nWhich projects should Terum capture?")
    print("A per-project hook is installed in each (git-ignored — nothing is committed).\n")
    for i, r in enumerate(shown, 1):
        label = (r["path"].name or str(r["path"]))[:24]
        if r.get("current"):
            meta = "current directory"
        else:
            meta = " · ".join(
                x for x in (_relative_time(r.get("mtime"), now), f"{r['sessions']} sessions") if x
            )
        default = "  (default)" if i == 1 else ""
        print(f"   {i}) {label:<24}  {_home_path(r['path'])}   [{meta}]{default}")
    if hidden > 0:
        print(f"   … and {hidden} more (use --project <path>, or 'g' for every project)")

    while True:
        try:
            raw = input(
                "\nEnter numbers (e.g. 1,3 or 1-3), 'a' = all listed, 'g' = every project [1]: "
            )
        except EOFError:
            # Piped/closed stdin at the prompt — take the default (current directory) rather
            # than crashing with a traceback after the key was already created.
            return False, [cwd]
        kind, idxs = _parse_selection(raw, len(shown))
        if kind == "global":
            return True, []
        if kind in ("pick", "all"):
            return False, [shown[i - 1]["path"] for i in idxs]
        if kind == "none":
            return False, [cwd]
        print("Didn't recognize that — enter numbers like 1,3 or 1-3, or 'a'/'g'.")


def _install_scope(use_global: bool, bases: list[Path] | None) -> list[dict]:
    """Write the hook + note (+ .gitignore for project scope) for the chosen scope.

    Returns one dict per installed target for the caller's summary. Project bases are
    de-duplicated, and a base that isn't an existing directory is skipped with a warning —
    so a typo'd --project path never silently creates a stray `.claude/` tree.
    """
    if use_global:
        s, m = _scope_targets(True)
        _configure_hook(s)
        _append_claude_md(m)
        return [{"global": True, "settings": s, "note": m, "gitignored": False}]

    installed: list[dict] = []
    seen: set[str] = set()
    for base in bases or []:
        # Absolutize a relative --project (e.g. '.', 'sub') BEFORE any git/gitignore work:
        # _in_git_repo walks base.parents, and a relative path's parents stop at cwd, so a
        # repo-subdir install would otherwise skip .gitignore and leave files committable.
        base = Path(os.path.abspath(base.expanduser()))
        key = str(base)
        if key in seen:
            continue
        seen.add(key)
        if not base.is_dir():
            print(f"Warning: {base} is not a directory — skipping.")
            continue
        s, m = _scope_targets(False, base)
        _configure_hook(s)
        _append_claude_md(m)
        gi = _ensure_gitignore(base)
        installed.append({"global": False, "base": base, "settings": s, "note": m, "gitignored": gi})
    return installed


def cmd_setup(
    api_url: str | None = None,
    token: str | None = None,
    use_global: bool = False,
    projects: list[str] | None = None,
    mcp: bool | None = None,
    delivery: bool | None = None,
):
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
                    return  # user DECLINED — a cancellation, not a failure. Exit 0 is correct.
        except Exception:
            pass

    if not token:
        token = _browser_auth(api_url)
        if not token:
            # Auth failed: _browser_auth already reported the specific reason to stderr, so exit
            # without a second message — but DO exit non-zero (bug-561: this used to exit 0).
            sys.exit(1)

    hostname = socket.gethostname() or "unknown"
    try:
        resp = httpx.post(
            f"{api_url}/keys",
            json={"name": hostname},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
    except Exception as exc:
        die(f"Error: Could not reach {api_url}: {exc}")

    if resp.status_code == 409:
        die("Error: You have 10 active keys. Revoke one first.")
    if resp.status_code == 401:
        die("Error: Token expired or invalid. Run setup again.")
    if resp.status_code != 201:
        die(f"Error: Key creation failed (HTTP {resp.status_code}).")

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
            die("Error: Round-trip verification failed. Config deleted.")
    except Exception:
        delete_config()
        die("Error: Round-trip verification failed. Config deleted.")

    # Resolve the capture scope. Explicit flags win and keep non-interactive installs
    # unattended; otherwise an interactive terminal gets the project picker; a piped/CI
    # install with no flags falls back to the current directory.
    bases: list[Path] | None
    if use_global:
        bases = None
    elif projects:
        bases = [Path(p) for p in projects]
    elif sys.stdin.isatty() and not token_supplied:
        use_global, bases = _prompt_project_scope(Path.cwd())
    else:
        bases = [Path.cwd()]

    installed = _install_scope(use_global, bases)

    _maybe_configure_mcp_interactive(api_key, api_url, mcp)
    _maybe_install_delivery_interactive(delivery)

    prefix = api_key[:8]
    print(f"\nTerum connected! Key: {prefix}...")

    if use_global:
        print("\nClaude Code hook installed globally — sessions in every project will be captured.")
        print(f"A summary instruction was added to {_display_path(_scope_targets(True)[1])}.")
    elif not installed:
        print("\nNo project hook was installed. Run 'terum-capture setup' from a project,")
        print("or 'terum-capture setup --global' to capture every project.")
    else:
        if len(installed) == 1:
            print(f"\nClaude Code hook installed for this project:\n  {_home_path(installed[0]['base'])}")
            print("Run 'terum-capture setup --global' to capture every project instead.")
        else:
            print(f"\nClaude Code hook installed for {len(installed)} projects:")
            for t in installed:
                print(f"  • {_home_path(t['base'])}")
        print("A summary instruction was added to each project's CLAUDE.local.md.")
        if any(t["gitignored"] for t in installed):
            print("The capture files were added to .gitignore so they stay out of version control.")
        if _global_hook_present():
            print(
                "\nNote: a global Terum hook is also installed (~/.claude/settings.json), so every\n"
                "project is still captured. Run 'terum-capture logout --global' to remove it."
            )

    print("\nNo further setup needed. Start a new Claude Code session to begin capturing.")

    _maybe_offer_backfill(interactive=not token_supplied and sys.stdin.isatty())


def cmd_setup_hook():
    """Refresh every MANAGED artifact to this installed package's canonical form.

    Unlike ``setup``, this mints no API key and prompts for nothing. It rewrites the terum
    Stop-hook entry (command + timeout) in whichever scopes already have one — global and/or
    this project — refreshes the MCP-usage block wording in ~/.claude/CLAUDE.md IF the block
    exists, and re-canonicalizes the delivery hook IF it is installed. Refresh-only
    throughout: it never adds a hook, the nudge block, or the delivery hook to a scope that
    didn't opt in — ``update`` runs this, and a maintenance command must never flip
    defaults or widen capture's scope. Safe to run anytime to repair drift.
    """
    refreshed = _refresh_installed_hooks()
    _upsert_mcp_usage_claude_md(add_if_missing=False)
    _refresh_delivery_hook_if_installed()
    if not refreshed:
        # Not a failure (``update`` calls this on every upgrade, including on a machine that
        # has not run `setup` yet) — so exit 0 and point at the command that installs.
        print(
            f"terum-capture {__version__}: no Terum hook found to refresh here. "
            "Run 'terum-capture setup' to install one."
        )
        return
    print(
        f"terum-capture {__version__}: Stop hook refreshed (timeout {HOOK_TIMEOUT}s) in "
        + ", ".join(_display_path(p) for p in refreshed)
        + ". Restart any open Claude Code sessions to load it."
    )


def _refresh_delivery_hook_if_installed():
    """Re-canonicalize the delivery hook entry IF the user opted in (refresh-only).

    Lets ``update`` ship delivery-hook command/timeout changes the same way it ships
    Stop-hook changes, without ever installing the hook on a machine that never ran
    ``delivery install``. Warn-don't-crash (unreadable settings -> leave untouched)."""
    from terum_capture.delivery_hooks import _is_our_delivery_group, install_delivery_hooks
    try:
        if not CLAUDE_SETTINGS.exists():
            return
        settings = json.loads(CLAUDE_SETTINGS.read_text())
        groups = settings.get("hooks", {}).get("UserPromptSubmit", [])
        if any(_is_our_delivery_group(g) for g in groups):
            install_delivery_hooks()
    except Exception as exc:
        print(f"Warning: Could not refresh the delivery hook: {exc}")


def _maybe_install_delivery_interactive(choice: bool | None) -> None:
    """The opt-in delivery prompt at the end of `cmd_setup` (default-YES on a TTY).

    Same tri-state contract as _maybe_configure_mcp_interactive: None -> ask on a TTY,
    skip silently otherwise; True -> forced yes (--delivery); False -> forced skip
    (--no-delivery). Consent lives HERE, at setup time, deliberately: `update` never
    installs the hook (a maintenance command must not flip a machine's defaults)."""
    if choice is False:
        return

    if choice is None:
        if not sys.stdin.isatty():
            return
        try:
            answer = input(
                "Also inject relevant team knowledge into each Claude Code prompt "
                "(in-flow delivery; fail-open, removable with 'terum-capture delivery "
                "uninstall')? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if answer in ("n", "no"):
            return

    from terum_capture.delivery_hooks import install_delivery_hooks
    install_delivery_hooks()
    print("Delivery hook installed — each prompt now arrives with relevant team context.")


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


def _settings_has_our_hook(settings_path: Path) -> bool:
    """True if `settings_path` has a terum Stop hook. Robust to any malformed/absent JSON.

    The whole read+navigate is guarded (incl. AttributeError) so a hand-edited settings
    file whose top level or `hooks` value is not an object can never crash the caller.
    """
    try:
        settings = json.loads(settings_path.read_text())
        stop = settings.get("hooks", {}).get("Stop", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return False
    return isinstance(stop, list) and any(_is_our_stop_entry(e) for e in stop)


def _global_hook_present() -> bool:
    """True if a terum Stop hook is installed machine-wide in ~/.claude/settings.json.

    Used to warn a project-scoped `setup` that a pre-existing global hook is still
    capturing every project (it fires alongside the new project-local one), so the user
    knows to `logout --global` if they want capture to actually be project-specific.
    """
    return _settings_has_our_hook(CLAUDE_SETTINGS)


def _configure_hook(settings_path: Path):
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings: dict = {}
        if settings_path.exists():
            settings = json.loads(settings_path.read_text())

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
            settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    except Exception as exc:
        print(f"Warning: Could not configure hook: {exc}")


def _refresh_installed_hooks() -> list[Path]:
    """Re-apply the canonical Stop entry everywhere a terum hook is ALREADY installed.

    Refresh-only, and that restriction is what project scoping makes load-bearing.
    `setup-hook` and the daily self-heal are drift-REPAIR paths: before capture had a scope
    they could rewrite ~/.claude/settings.json unconditionally, because global was the only
    place a hook could live. Doing that now would hand a machine-wide hook to someone who
    deliberately chose a per-project one — `terum-capture update` would silently re-arm
    capture for every project on the box. So each scope is refreshed only if it already has
    our hook, and neither is ever created here; installing is `setup`'s job alone.

    Checks both scopes because either can hold the hook: global, plus the project the
    command is being run from (for the self-heal that is the session's own project, since
    Claude Code runs a Stop hook with the project as cwd). Returns the paths refreshed.
    """
    refreshed: list[Path] = []
    for path in (CLAUDE_SETTINGS, _scope_targets(False)[0]):
        if path not in refreshed and _settings_has_our_hook(path):
            _configure_hook(path)
            refreshed.append(path)
    return refreshed


def _append_claude_md(claude_md_path: Path):
    try:
        claude_md_path.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if claude_md_path.exists():
            existing = claude_md_path.read_text()

        if "## Terum Knowledge Capture" in existing:
            return

        with open(claude_md_path, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(CLAUDE_MD_BLOCK)
    except Exception as exc:
        print(f"Warning: Could not update CLAUDE.md: {exc}")


def _upsert_mcp_usage_claude_md(add_if_missing: bool = True):
    """Install or refresh the MANAGED MCP-usage block in ~/.claude/CLAUDE.md.

    The block spans MCP_USAGE_HEADER .. MCP_USAGE_END_MARKER and is replaced wholesale with
    this package's current wording, so shipped nudge improvements reach existing installs on
    `update` (via setup-hook) instead of fossilizing per-machine. Everything outside the span
    is preserved. A legacy block without the end marker is replaced up to the next "## "
    heading or EOF — the conservative bound. `add_if_missing=False` is the refresh-only mode
    (setup-hook/update): a machine that never wired MCP is never given the block as a side
    effect. Warn-don't-crash, mirroring _append_claude_md."""
    try:
        CLAUDE_MD.parent.mkdir(parents=True, exist_ok=True)
        existing = CLAUDE_MD.read_text() if CLAUDE_MD.exists() else ""
        lines = existing.split("\n")
        start = next((i for i, l in enumerate(lines) if l.strip() == MCP_USAGE_HEADER), None)

        if start is None:
            if not add_if_missing:
                return
            with open(CLAUDE_MD, "a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(MCP_USAGE_BLOCK)
            return

        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].strip() == MCP_USAGE_END_MARKER), None)
        if end is None:  # legacy block: no marker -> stop before the next section heading
            nxt = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), None)
            end = (nxt - 1) if nxt is not None else (len(lines) - 1)

        block_lines = MCP_USAGE_BLOCK.strip("\n").split("\n")
        tail = lines[end + 1:]
        spacer = [""] if tail and tail[0].strip() else []
        new_lines = lines[:start] + block_lines + spacer + tail
        out = "\n".join(new_lines)
        if not out.endswith("\n"):
            out += "\n"
        CLAUDE_MD.write_text(out)
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
        _upsert_mcp_usage_claude_md()
        print("MCP connected — your agent can now pull team decisions & run conflict checks.")
    elif result == "already":
        _upsert_mcp_usage_claude_md()
        print("MCP already configured — left it as-is.")
    else:
        print("Could not auto-configure MCP. Run 'terum-capture mcp install' later.")


def cmd_mcp_install(client: str = "claude"):
    config = load_config()
    if not config or not config.get("api_key"):
        die("Not configured. Run: terum-capture setup")

    result = _configure_mcp(config["api_key"], config.get("api_url", DEFAULT_API_URL), client=client)

    label = "Cursor" if client == "cursor" else "Claude Code"
    # The usage guidance lives in ~/.claude/CLAUDE.md — Claude Code only (Cursor has no such file).
    if client == "claude" and result in ("installed", "already"):
        _upsert_mcp_usage_claude_md()
    if result == "installed":
        print(f"MCP connected for {label}.")
    elif result == "already":
        print(f"MCP already configured for {label} — left it as-is.")
    else:
        die(f"Could not configure MCP for {label}.")


def _configure_mcp(api_key: str, api_url: str, client: str = "claude") -> str:
    """Wire an MCP server named "terum" pointing at {api_url}/mcp, authed with api_key.
    Returns one of: "installed" | "already" | "failed". Never raises."""
    mcp_url = f"{api_url.rstrip('/')}/mcp"

    if client == "claude":
        return _configure_mcp_claude(api_key, mcp_url)
    if client == "cursor":
        return _configure_mcp_cursor(api_key, mcp_url)

    # err(), not die(): this function's contract is to return a "failed" sentinel and never raise,
    # so the caller owns the exit. Without this the specific reason went to stdout while the
    # caller's generic "Could not configure MCP" went to stderr — one failure split across two
    # streams. Unreachable via the CLI (cli.py validates the client first); defensive for
    # programmatic callers.
    err(f"Error: unknown MCP client '{client}'.")
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


def _remove_hook(settings_path: Path) -> bool:
    """Remove the terum Stop hook from `settings_path`. Returns True if one was removed."""
    try:
        if not settings_path.exists():
            return False
        settings = json.loads(settings_path.read_text())
        hooks = settings.get("hooks", {})
        stop_hooks = hooks.get("Stop", [])
        kept = [h for h in stop_hooks if not _is_our_stop_entry(h)]
        removed = len(kept) != len(stop_hooks)
        hooks["Stop"] = kept
        if not hooks["Stop"]:
            del hooks["Stop"]
        if not hooks:
            del settings["hooks"]
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        return removed
    except Exception:
        return False


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


def cmd_logout(use_global: bool = False, project: str | None = None):
    config = load_config()
    if not config:
        print("Not configured — nothing to do.")
        return

    print("Warning: This removes your local config but does NOT revoke the API key.")
    print("The key will remain active until revoked from the dashboard.")
    answer = input("Continue? [y/N] ").strip().lower()
    if answer != "y":
        return

    # The API key config is global, so it is always removed. The Stop hook is scope-
    # specific: by default we uninstall this project's hook (--project <path> targets a
    # different one); --global removes the machine-wide one. Hooks in other projects are
    # left untouched — this only ever removes one project's hook per run.
    base = Path(project).expanduser() if project else None
    settings_path, _ = _scope_targets(use_global, base)
    delete_config()
    removed = _remove_hook(settings_path)
    if removed:
        print(f"Config removed and the hook uninstalled from {_display_path(settings_path)}.")
    else:
        print(f"Config removed. No Terum hook was found at {_display_path(settings_path)}.")

    # A leftover hook in the OTHER scope keeps firing (now a harmless 'unconfigured' no-op
    # since the config is gone). Point the user at the command that actually removes it —
    # this closes the `setup --global` then bare `logout` footgun the message otherwise hides.
    other_path, _ = _scope_targets(not use_global)
    if _settings_has_our_hook(other_path):
        if use_global:
            print(
                f"A project-local hook is still installed at {_display_path(other_path)} — "
                "run 'terum-capture logout' from that project to remove it."
            )
        else:
            print(
                "A global Terum hook is still installed (~/.claude/settings.json) — "
                "run 'terum-capture logout --global' to remove it."
            )
