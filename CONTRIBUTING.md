# Contributing to terum-capture

Thanks for your interest in improving terum-capture. This is a small, test-heavy
codebase with a few hard rules that exist because breaking them has bitten real
users; read this page before opening a PR and the review will be quick.

## Development setup

Requires Python ≥ 3.10.

```bash
git clone https://github.com/ryanliu-terum/terum-capture
cd terum-capture
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

`pip install -e ".[dev]"` is the one documented way to get a working test
environment — it installs the pinned pytest the suite expects.

## The rules CI enforces

**Tests never touch your real home directory.** Every `~`-rooted path the CLI
uses is redirected to `tmp_path` by `tests/conftest.py`. If you add a new
`~`-rooted constant, wire it into that fixture and into
`scripts/home-watchlist.txt`, or CI's home-isolation check will fail. This rule
exists because a test run once rewrote a developer's real
`~/.claude/settings.json` and broke every Claude Code prompt afterwards.

**Fatal diagnostics go to stderr, and failure exits non-zero.** Claude Code runs
several of these commands as hooks: a supervisor surfaces a failed child's
*stderr*, and on some hook events *stdout is injected as model context*. So
`print("Error: ...")` is a bug twice over. Route diagnostics through
`output.err()` / `output.die()`, and make sure any command that reports a
failure also exits non-zero. `scripts/check_error_streams.py` (an AST gate, run
by the test suite) enforces both rules — don't work around it; if it flags your
code, the code is wrong.

**All of CI must be green.** The pytest matrix runs on every interpreter from
3.10 through 3.14. PRs target `main`.

## Making changes

- Keep the repo's comment convention: comments state the constraint or the bug
  that made the code this way, not what the next line does.
- New behavior needs a test. Look at the existing `tests/test_*.py` for the
  house style — they are hermetic (no network, no real `$HOME`).
- The install path (`install.sh`) and the self-updater (`updater.py`) are the
  highest-risk surfaces: they run unattended on user machines. Changes there
  get extra scrutiny.

## Releases (maintainers)

Versioning is manual and single-sourced: bump `__version__` in
`src/terum_capture/__init__.py` (hatch reads it from there), update
`CHANGELOG.md`, then tag `vX.Y.Z` and publish a GitHub release.

## Questions and discussion

Open a GitHub issue. For security reports, see [SECURITY.md](SECURITY.md) —
please don't open public issues for vulnerabilities.
