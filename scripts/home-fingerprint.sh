#!/usr/bin/env bash
#
# Print a stable, sorted fingerprint (path + mtime + size) of files under $HOME, so a caller can
# run it either side of the test suite and prove the suite touched nothing outside its tmp_path.
#
# Why this exists (bug-560). terum_capture resolves eight module-level constants from
# Path.home(): ~/.claude/settings.json, ~/.claude/CLAUDE.md, ~/.claude.json, ~/.cursor/mcp.json,
# ~/.claude/projects, ~/.terum/config.json, ~/.terum/delivery_state.json and the ~/.terum sidecar
# dir. tests/conftest.py now redirects every one of them to tmp_path, autouse, for every test.
# Before that fixture existed, running `pytest` silently rewrote the developer's LIVE
# ~/.claude/settings.json — re-pointing Claude Code's UserPromptSubmit delivery hook at a dev
# checkout's editable venv. The next time that checkout changed branch, every prompt in Claude
# Code errored and in-flow delivery stopped, with nothing saying why.
#
# The fixture is the fix; this is the gate that keeps it fixed. It scans the whole tree rather
# than that list of eight ON PURPOSE: the failure mode is a NEW ~-rooted write path appearing
# under tests that were "already safe" (PR #11 grew two of them), so an enumerated list goes
# stale exactly when it matters.
#
# mtime+size rather than a content hash: it is ~50x cheaper over a runner's home (~/.rustup and
# ~/.cargo alone are tens of thousands of files) and strictly more sensitive — a rewrite with
# identical bytes still moves mtime, and that is still an escape worth failing on.
#
# Two trees are pruned:
#   $HOME/.cache — pip's wheel cache. No CLI constant points there.
#   $HOME/work   — on a GitHub runner the checkout AND $RUNNER_TEMP both live here
#                  (/home/runner/work/...). That is the code under test plus this script's own
#                  scratch files, not the home state being protected. Leaving it in would make
#                  every run differ (__pycache__, .pytest_cache, the fingerprint files
#                  themselves) and the gate would fail always, i.e. mean nothing.
#
# Usage in CI — snapshot, run, snapshot, diff:
#   bash scripts/home-fingerprint.sh > "$RUNNER_TEMP/home-before.txt"
#   pytest -q
#   bash scripts/home-fingerprint.sh > "$RUNNER_TEMP/home-after.txt"
#   diff -u "$RUNNER_TEMP/home-before.txt" "$RUNNER_TEMP/home-after.txt"   # any output = escape
#
# Usage on a real dev machine, where all of $HOME churns on its own (Claude Code rewrites
# ~/.claude/.credentials.json and history.jsonl as you work, and Terum's own delivery hook writes
# ~/.terum/sent_* markers): pass the paths to watch and only those are fingerprinted.
#   bash scripts/home-fingerprint.sh ~/.claude/settings.json ~/.claude/CLAUDE.md ~/.terum/config.json
#
set -uo pipefail

if [ "$#" -gt 0 ]; then
  for target in "$@"; do
    if [ -e "$target" ]; then
      find "$target" -maxdepth 0 -printf '%p  %T@  %s\n'
    else
      # ABSENT is a real fingerprint, not an error: a CI runner has no ~/.claude at all, and
      # "still absent" is exactly what the gate needs to assert.
      printf 'ABSENT  %s\n' "$target"
    fi
  done
  exit 0
fi

find "$HOME" \( -path "$HOME/.cache" -o -path "$HOME/work" \) -prune -o -type f \
  -printf '%p  %T@  %s\n' 2>/dev/null | LC_ALL=C sort
