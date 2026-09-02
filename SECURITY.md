# Security Policy

terum-capture reads Claude Code session transcripts on your machine and uploads
them to your Terum account. That means vulnerabilities here can expose
conversation content, tokens, or the integrity of the hook that runs after
every Claude Code turn — we take reports seriously.

## Reporting a vulnerability

Please report vulnerabilities privately via
[GitHub private vulnerability reporting](https://github.com/ryanliu-terum/terum-capture/security/advisories/new)
— do **not** open a public issue.

Include what you can: affected version (`terum-capture --version`), platform,
reproduction steps, and impact as you understand it. You'll get an
acknowledgment within a few days, and we'll keep you updated as we triage and
fix.

## Scope

In scope:

- The `terum-capture` CLI and its Stop/UserPromptSubmit hook behavior
- `install.sh` and the self-update path (`terum-capture update`)
- Handling of transcripts, credentials, and config under `~/.terum*` and
  `.claude/settings.local.json`

Out of scope for this repo (report to Terum directly instead):

- The hosted Terum backend and dashboard (`api.terum.ai`, `app.terum.ai`)

## Supported versions

Only the latest release is supported. The CLI self-updates and nags on stale
versions by design; fixes ship as new releases rather than backports.
