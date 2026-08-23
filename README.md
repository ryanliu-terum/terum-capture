# terum-capture

Capture your [Claude Code](https://claude.com/claude-code) CLI sessions into [Terum](https://terum.ai)'s knowledge pipeline.

`terum-capture` installs a Claude Code **Stop hook** that, after each turn, reads the session transcript, extracts the new prompts and responses, and uploads them to your Terum account — where they're compacted into structured notes and made searchable. Setup is a one-time browser login; capture is automatic from then on.

Capture is **project-scoped**. When you run `setup` interactively, it lists your recent Claude Code projects and lets you **pick which one(s) to capture** — it then writes a git-ignored hook into each selected repo's `.claude/settings.local.json`, so only those projects are captured and nothing is committed. You can also choose "every project" (global) from the same prompt, or select projects non-interactively with flags.

## Install

One command — it finds a Python ≥ 3.10, installs pipx if needed, and automatically falls back to [uv](https://docs.astral.sh/uv/) with a self-contained Python when the pipx path can't work:

```bash
curl -fsSL https://raw.githubusercontent.com/ryanliu-terum/terum-capture/main/install.sh | bash
terum-capture setup
```

**Alternative** — if you prefer to run the tools yourself (requires Python 3.10+ and [`pipx`](https://pipx.pypa.io/)):

```bash
pipx install git+https://github.com/ryanliu-terum/terum-capture
```

> [!NOTE]
> **macOS 26.1/26.2 (Tahoe):** bare `pipx install` fails outright on these versions — every Homebrew Python is broken (`platform.mac_ver()` returns empty, the same defect family as [Homebrew/homebrew-core#277330](https://github.com/Homebrew/homebrew-core/issues/277330)), and pipx ≥ 1.16 creates venvs through uv, which refuses a broken interpreter. This is not specific to terum-capture. Use the one-liner above (it falls back to uv automatically), or directly: `uv tool install --managed-python git+https://github.com/ryanliu-terum/terum-capture` — same end state (isolated venv, `terum-capture` on your `PATH`), and `terum-capture update` works normally afterwards.

`pipx` (or `uv tool`) gives `terum-capture` one isolated, machine-wide install. The **CLI stays global**; what `setup` configures is per-project by default. The Stop hook runs through that install's Python interpreter by absolute path (`sys.executable`), so it keeps working regardless of your shell `PATH`.

After installing, run `terum-capture setup` and **pick the project(s) to capture** from the prompt (the current directory is the default), then **start a new Claude Code session** in one of them — existing sessions won't have the hook loaded yet.

## Commands

| Command | What it does |
|---------|--------------|
| `terum-capture setup` | Browser login → creates an API key, then **prompts you to pick which project(s) to capture** (or "every project"). For each chosen project it installs the Stop hook in `.claude/settings.local.json` and a short summary instruction in `CLAUDE.local.md` — both git-ignored. Interactive setup then offers to import your past sessions, and to connect Claude Code's MCP to Terum (see below). |
| `terum-capture backfill` | Import your **existing** Claude Code sessions (last 30 days by default) into Terum, so a fresh install isn't starting from an empty graph. Re-runnable and crash-safe — already-sent sessions are skipped. |
| `terum-capture status` | Show your key prefix, API URL, installed version, and whether the key is still valid (plus a note if a newer version is available). |
| `terum-capture update` | Reinstall the latest CLI from GitHub and refresh the Stop hook. Run this to pick up fixes — the hook also self-heals its config daily, and `status` tells you when an update is available. |
| `terum-capture setup-hook` | Re-write just the Stop-hook entry (no login, no new key), in whichever scopes already have one — the machine-wide hook and/or this project's. Refresh-only: it repairs drift and never installs a hook where you didn't ask for one. Rarely needed by hand — `update` runs it for you. |
| `terum-capture logout` | Remove local config and uninstall the current project's hook (or the machine-wide hook with `--global`). **Does not revoke the key** — revoke that from the dashboard. |
| `terum-capture upload` | Invoked automatically by the Stop hook (reads hook input from stdin). You don't run this manually. |
| `terum-capture mcp install` | Connect an already-set-up machine to Terum's MCP server (see below). Accepts `--client claude` (default) or `--client cursor`. |
| `terum-capture delivery install` | Opt in to the in-flow delivery hook: on every Claude Code prompt, relevant team knowledge is injected before the model works (plus a periodic reminder to conflict-check its own decisions). Requires the MCP-connected setup. `delivery uninstall` removes it. Fail-open: if Terum is unreachable, nothing is injected and your session is unaffected. |

`setup` accepts `--project <path>` (install into a specific project without the prompt — **repeatable**, e.g. `--project ~/a --project ~/b`, to capture several at once), `--global` (install machine-wide in `~/.claude` instead), `--url <api>` (defaults to `https://api.terum.ai/api`), and `--token <jwt>` to skip the browser for headless/CI installs (non-interactive setup skips the backfill and MCP prompts). Passing `--project`/`--global` (or piping with `--token`) skips the interactive picker, so automated installs never block. It also accepts `--mcp` (install MCP directly without prompting, even on a non-interactive run) and `--no-mcp` (skip MCP entirely); by default it asks with a `[Y/n]` prompt when run interactively and skips silently otherwise. The same pattern applies to in-flow delivery: `--delivery` / `--no-delivery`, with an interactive default-Yes `[Y/n]` prompt. `update` never installs delivery on a machine that didn't opt in — it only refreshes what's already there.

`logout` accepts `--project <path>` (uninstall a specific project's hook) and `--global` (remove the machine-wide hook); with no flag it uninstalls the current directory's hook.

**Scope note:** capture is per-project, but MCP and in-flow delivery are machine-wide — MCP lives in `~/.claude.json` and the delivery hook in `~/.claude/settings.json`, so they apply to every Claude Code session regardless of which projects you chose to capture. They're read-only pulls of team knowledge, not capture. `delivery uninstall` removes the delivery hook.

`backfill` accepts `--days N` (window, default 30), `--all` (no time window — import everything), and `--limit N` (cap the number of sessions). It discovers transcripts under `~/.claude/projects/`, paces uploads under the server rate limit, backs off on throttling, and reports how many were imported vs. already captured. Uploaded sessions finish processing server-side asynchronously over the next day or so.

## Connect your agent to team knowledge (MCP)

Terum runs a remote MCP server that lets your agent pull your team's shared decisions and run conflict checks — read-only, separate from the write-up capture flow above. It reuses the same `trm_` API key `setup` already mints, so there's no second sign-in.

- **During `setup`:** after the hook is installed, you're asked `Also connect Claude Code to your team's shared decisions & conflict-checks (read-only)? [Y/n]`. Say yes (or just hit enter) and it's wired automatically.
- **Later, or for Cursor:** run `terum-capture mcp install` (Claude Code) or `terum-capture mcp install --client cursor`.

Both are idempotent — running them again when already connected leaves the existing config alone.

When MCP is wired for Claude Code, a short **"Terum Team Knowledge (MCP)"** block is also appended to `~/.claude/CLAUDE.md` telling the agent *when* to call the three tools (`search_team_knowledge`, `check_decision`, `get_standing_decisions`). Delivery is pull-only — nothing is injected automatically — so without this nudge the agent rarely reaches for the tools and team context silently never surfaces. The block is idempotent (added once) and never written for Cursor.

## How it works

- **Hook:** `setup` adds a `Stop` hook that runs `"<python>" -m terum_capture upload` — into each selected project's `.claude/settings.local.json` (git-ignored, capturing only that project), or `~/.claude/settings.json` with `--global`. It's routed through the signed Python interpreter (`sys.executable`) rather than the `terum-capture` console-script shim, because Windows Smart App Control / WDAC block unsigned pip/pipx `.exe` launchers on enforcing machines (which silently killed capture every session). `setup` migrates any older hook entry to this form; `logout` removes either.
- **Project picker:** the interactive prompt lists your known projects by reading each session's recorded working directory from the transcripts under `~/.claude/projects/` (the directory names there are a lossy encoding of the path, so the real path comes from the transcript). Projects whose folder no longer exists are dropped. The current directory is always offered as the default, even if it has no prior sessions.
- **Project vs global:** project scope writes `.claude/settings.local.json` + `CLAUDE.local.md` and adds both to the repo's `.gitignore` so they're never committed. `--global` writes `~/.claude/settings.json` + `~/.claude/CLAUDE.md`. Your API key config is always global (`~/.terum/config.json`) and shared across every project.
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

Two conventions are enforced by `scripts/check_error_streams.py` (also run in CI): a fatal
diagnostic goes to stderr via `output.die()`/`output.err()`, never stdout — a supervising Claude
Code hook surfaces stderr, so a reason on stdout is invisible exactly when it matters (bug-559) —
and a `cmd_*` that reports a failure must exit non-zero (bug-561). Run it with
`python scripts/check_error_streams.py`; the contract it enforces is documented in
`src/terum_capture/output.py`.

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
