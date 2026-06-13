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
| `terum-capture setup` | Browser login → creates an API key, installs the Stop hook in `~/.claude/settings.json`, and appends a short summary instruction to `~/.claude/CLAUDE.md`. |
| `terum-capture status` | Show your key prefix, API URL, and whether the key is still valid. |
| `terum-capture logout` | Remove local config and uninstall the hook. **Does not revoke the key** — revoke that from the dashboard. |
| `terum-capture upload` | Invoked automatically by the Stop hook (reads hook input from stdin). You don't run this manually. |

`setup` accepts `--url <api>` (defaults to `https://api.terum.ai/api`) and `--token <jwt>` to skip the browser for headless/CI installs.

## How it works

- **Hook:** `setup` adds `{"type": "command", "command": "terum-capture upload", "timeout": 15}` to the `Stop` hooks in `~/.claude/settings.json`.
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
