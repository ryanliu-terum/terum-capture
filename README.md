# terum-capture

Capture your [Claude Code](https://claude.com/claude-code) CLI sessions into [Terum](https://terum.ai)'s knowledge pipeline.

`terum-capture` installs a Claude Code **Stop hook** that, after each turn, reads the session transcript, extracts the new prompts and responses, and uploads them to your Terum account — where they're compacted into structured notes and made searchable. Setup is a one-time browser login; capture is automatic from then on.

## Install

**Requirements:** Python 3.10+ and [`pipx`](https://pipx.pypa.io/) (`brew install pipx` on macOS).

```bash
pipx install git+https://github.com/ryanliu-terum/terum-capture
terum-capture setup
```

Or the one-liner (checks Python, installs pipx if needed, then installs):

```bash
curl -fsSL https://raw.githubusercontent.com/ryanliu-terum/terum-capture/main/install.sh | bash
```

`pipx` is used (rather than a plain `pip install` into a venv) because the Stop hook calls the bare `terum-capture` command — it needs to be on your `PATH` globally.

After installing, run `terum-capture setup`, then **start a new Claude Code session** — existing sessions won't have the hook loaded yet.

## Commands

| Command | What it does |
|---------|--------------|
| `terum-capture setup` | Browser login → creates an API key, installs the Stop hook in `~/.claude/settings.json`, and appends a short summary instruction to `~/.claude/CLAUDE.md`. Interactive setup then offers to import your past sessions, and to connect Claude Code's MCP to Terum (see below). |
| `terum-capture backfill` | Import your **existing** Claude Code sessions (last 30 days by default) into Terum, so a fresh install isn't starting from an empty graph. Re-runnable and crash-safe — already-sent sessions are skipped. |
| `terum-capture status` | Show your key prefix, API URL, installed version, and whether the key is still valid (plus a note if a newer version is available). |
| `terum-capture update` | Reinstall the latest CLI from GitHub and refresh the Stop hook. Run this to pick up fixes — the hook also self-heals its config daily, and `status` tells you when an update is available. |
| `terum-capture setup-hook` | Re-write just the Stop-hook entry in `~/.claude/settings.json` (no login, no new key). Rarely needed by hand — `update` runs it for you; use it to repair hook drift. |
| `terum-capture logout` | Remove local config and uninstall the hook. **Does not revoke the key** — revoke that from the dashboard. |
| `terum-capture upload` | Invoked automatically by the Stop hook (reads hook input from stdin). You don't run this manually. |
| `terum-capture mcp install` | Connect an already-set-up machine to Terum's MCP server (see below). Accepts `--client claude` (default) or `--client cursor`. |
| `terum-capture delivery install` | Opt in to the in-flow delivery hook: on every Claude Code prompt, relevant team knowledge is injected before the model works (plus a periodic reminder to conflict-check its own decisions). Requires the MCP-connected setup. `delivery uninstall` removes it. Fail-open: if Terum is unreachable, nothing is injected and your session is unaffected. |

`setup` accepts `--url <api>` (defaults to `https://api.terum.ai/api`) and `--token <jwt>` to skip the browser for headless/CI installs (non-interactive setup skips the backfill and MCP prompts). It also accepts `--mcp` (install MCP directly without prompting, even on a non-interactive run) and `--no-mcp` (skip MCP entirely); by default it asks with a `[Y/n]` prompt when run interactively and skips silently otherwise. The same pattern applies to in-flow delivery: `--delivery` / `--no-delivery`, with an interactive default-Yes `[Y/n]` prompt. `update` never installs delivery on a machine that didn't opt in — it only refreshes what's already there.

`backfill` accepts `--days N` (window, default 30), `--all` (no time window — import everything), and `--limit N` (cap the number of sessions). It discovers transcripts under `~/.claude/projects/`, paces uploads under the server rate limit, backs off on throttling, and reports how many were imported vs. already captured. Uploaded sessions finish processing server-side asynchronously over the next day or so.

## Connect your agent to team knowledge (MCP)

Terum runs a remote MCP server that lets your agent pull your team's shared decisions and run conflict checks — read-only, separate from the write-up capture flow above. It reuses the same `trm_` API key `setup` already mints, so there's no second sign-in.

- **During `setup`:** after the hook is installed, you're asked `Also connect Claude Code to your team's shared decisions & conflict-checks (read-only)? [Y/n]`. Say yes (or just hit enter) and it's wired automatically.
- **Later, or for Cursor:** run `terum-capture mcp install` (Claude Code) or `terum-capture mcp install --client cursor`.

Both are idempotent — running them again when already connected leaves the existing config alone.

When MCP is wired for Claude Code, a short **"Terum Team Knowledge (MCP)"** block is also appended to `~/.claude/CLAUDE.md` telling the agent *when* to call the three tools (`search_team_knowledge`, `check_decision`, `get_standing_decisions`). Delivery is pull-only — nothing is injected automatically — so without this nudge the agent rarely reaches for the tools and team context silently never surfaces. The block is idempotent (added once) and never written for Cursor.

## How it works

- **Hook:** `setup` adds a `Stop` hook to `~/.claude/settings.json` that runs `"<python>" -m terum_capture upload` — routed through the signed Python interpreter (`sys.executable`) rather than the `terum-capture` console-script shim, because Windows Smart App Control / WDAC block unsigned pip/pipx `.exe` launchers on enforcing machines (which silently killed capture every session). `setup` migrates any older hook entry to this form; `logout` removes either.
- **Incremental upload:** an offset sidecar at `~/.terum/sent_<session_id>` tracks how much of each transcript has been sent, so only new turns are uploaded. Sidecars older than 7 days are cleaned up automatically.
- **What's captured:** your prompts and Claude's **text** responses (thinking blocks, tool calls, and tool results are stripped), the conversation title, the working directory, and session-level token usage. Trivial turns (< 10 chars) are dropped.
- **Config:** your API key lives in `~/.terum/config.json` (created `chmod 600`).

## Privacy

This tool uploads the text of your Claude Code conversations to your Terum account. It does **not** capture tool inputs/outputs, thinking blocks, file contents, or shell command output — only the prompt and assistant-reply text, plus token counts. Everything is tied to your account and your API key; revoke the key any time from the Terum dashboard.

## Development

```bash
git clone https://github.com/ryanliu-terum/terum-capture
cd terum-capture

# Build the venv from a 3.10+ interpreter (pin: see .python-version)
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # the dev extra is pytest
pytest
```

The suite must never touch your real home directory — it once rewrote a developer's live
`~/.claude/settings.json` and broke Claude Code (bug-560). `tests/conftest.py` redirects every
`~`-rooted path to `tmp_path`, and CI asserts it stayed that way. To check by hand:

```bash
bash scripts/home-fingerprint.sh ~/.claude/settings.json ~/.claude/CLAUDE.md ~/.terum/config.json > /tmp/before
pytest -q
bash scripts/home-fingerprint.sh ~/.claude/settings.json ~/.claude/CLAUDE.md ~/.terum/config.json > /tmp/after
diff -u /tmp/before /tmp/after   # any output means a test escaped its tmp_path
```

## License

MIT — see [LICENSE](LICENSE).
