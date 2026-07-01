# terum-capture

Capture your [Claude Code](https://claude.com/claude-code) CLI sessions into [Terum](https://terum.ai)'s knowledge pipeline.

`terum-capture` installs a Claude Code **Stop hook** that, after each turn, reads the session transcript, extracts the new prompts and responses, and uploads them to your Terum account — where they're compacted into structured notes and made searchable. Setup is a one-time browser login; capture is automatic from then on.

Capture is **project-scoped**. When you run `setup` interactively, it lists your recent Claude Code projects and lets you **pick which one(s) to capture** — it then writes a git-ignored hook into each selected repo's `.claude/settings.local.json`, so only those projects are captured and nothing is committed. You can also choose "every project" (global) from the same prompt, or select projects non-interactively with flags.

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

`pipx` gives `terum-capture` one isolated, machine-wide install. The **CLI stays global**; what `setup` configures is per-project by default. The Stop hook runs through that install's Python interpreter by absolute path (`sys.executable`), so it keeps working regardless of your shell `PATH`.

After installing, run `terum-capture setup` and **pick the project(s) to capture** from the prompt (the current directory is the default), then **start a new Claude Code session** in one of them — existing sessions won't have the hook loaded yet.

## Commands

| Command | What it does |
|---------|--------------|
| `terum-capture setup` | Browser login → creates an API key, then **prompts you to pick which project(s) to capture** (or "every project"). For each chosen project it installs the Stop hook in `.claude/settings.local.json` and a short summary instruction in `CLAUDE.local.md` — both git-ignored. Interactive setup then offers to import your past sessions. |
| `terum-capture backfill` | Import your **existing** Claude Code sessions (last 30 days by default) into Terum, so a fresh install isn't starting from an empty graph. Re-runnable and crash-safe — already-sent sessions are skipped. |
| `terum-capture status` | Show your key prefix, API URL, and whether the key is still valid. |
| `terum-capture logout` | Remove local config and uninstall the current project's hook (or the machine-wide hook with `--global`). **Does not revoke the key** — revoke that from the dashboard. |
| `terum-capture upload` | Invoked automatically by the Stop hook (reads hook input from stdin). You don't run this manually. |

`setup` accepts `--project <path>` (install into a specific project without the prompt — **repeatable**, e.g. `--project ~/a --project ~/b`, to capture several at once), `--global` (install machine-wide in `~/.claude` instead), `--url <api>` (defaults to `https://api.terum.ai/api`), and `--token <jwt>` to skip the browser for headless/CI installs. Passing `--project`/`--global` (or piping with `--token`) skips the interactive picker, so automated installs never block. `logout` accepts `--project <path>` (uninstall a specific project's hook) and `--global` (remove the machine-wide hook); with no flag it uninstalls the current directory's hook.

`backfill` accepts `--days N` (window, default 30), `--all` (no time window — import everything), and `--limit N` (cap the number of sessions). It discovers transcripts under `~/.claude/projects/`, paces uploads under the server rate limit, backs off on throttling, and reports how many were imported vs. already captured. Uploaded sessions finish processing server-side asynchronously over the next day or so.

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
pip install -e .
pip install pytest
pytest
```

## License

MIT — see [LICENSE](LICENSE).
