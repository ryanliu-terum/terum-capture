#!/usr/bin/env bash
#
# Print a stable, sorted fingerprint (path + mtime + size) of the places this CLI can write under
# $HOME, so a caller can run it either side of the test suite and prove the suite touched none of
# them.
#
# Why this exists (bug-560). terum_capture resolves eight module-level constants from Path.home():
# ~/.claude/settings.json, ~/.claude/CLAUDE.md, ~/.claude.json, ~/.cursor/mcp.json,
# ~/.claude/projects, ~/.terum (the sidecar dir) and ~/.terum/config.json. tests/conftest.py now
# redirects every one of them to tmp_path, autouse, for every test. Before that fixture existed,
# running `pytest` silently rewrote the developer's LIVE ~/.claude/settings.json — re-pointing
# Claude Code's UserPromptSubmit delivery hook at a dev checkout's editable venv. The next time
# that checkout changed branch, every prompt in Claude Code errored and in-flow delivery stopped,
# with nothing saying why.
#
# The fixture is the fix; this is the gate that keeps it fixed.
#
# WHAT IS WATCHED, and why it is an allowlist. The paths come from scripts/home-watchlist.txt.
# The first version of this script scanned ALL of $HOME minus a denylist, on the theory that an
# enumerated list goes stale exactly when a ninth constant is added. Measured on a real runner,
# that does not work: a GitHub runner keeps the checkout AND $RUNNER_TEMP under $HOME
# (/home/runner/work/...), pip caches under ~/.cache, and — the one that actually broke it — the
# runner writes its OWN diagnostics to ~/actions-runner/_diag/*.log continuously WHILE the job
# runs. So the fingerprint differed on every run for reasons having nothing to do with the suite,
# and the gate failed always, which means it said nothing. A denylist against churn the
# environment owns cannot be maintained; the next runner image would reopen it.
#
# So the list is positive, and the staleness hole it opens is closed deterministically instead:
# tests/test_home_isolation_coverage.py parses every Path.home() constant out of src/ and fails if
# one is missing from the watchlist or from conftest.py's isolate_home fixture. A tenth constant
# breaks the suite at its source, which is both earlier and more specific than hoping a test
# happens to write there.
#
# mtime+size rather than a content hash: strictly more sensitive (a rewrite with identical bytes
# still moves mtime, and that is still an escape worth failing on) and cheaper.
#
# Usage in CI — snapshot, run, snapshot, diff:
#   bash scripts/home-fingerprint.sh > "$RUNNER_TEMP/home-before.txt"
#   pytest -q
#   bash scripts/home-fingerprint.sh > "$RUNNER_TEMP/home-after.txt"
#   diff -u "$RUNNER_TEMP/home-before.txt" "$RUNNER_TEMP/home-after.txt"   # any output = escape
#
# Usage on a real dev machine: pass explicit paths. The whole watchlist is too noisy there —
# Claude Code rewrites ~/.claude/.credentials.json and history.jsonl as you work, and Terum's own
# delivery hook writes ~/.terum/sent_* markers — so watch the files you care about:
#   bash scripts/home-fingerprint.sh ~/.claude/settings.json ~/.claude/CLAUDE.md ~/.terum/config.json
#
set -uo pipefail

WATCHLIST="$(cd "$(dirname "$0")" && pwd)/home-watchlist.txt"

fingerprint_path() {
  target="$1"
  if [ -d "$target" ]; then
    # Record the directory itself so that creating or removing it is visible even when empty,
    # then every file beneath it.
    printf 'DIR     %s\n' "$target"
    find "$target" -type f -printf '%p  %T@  %s\n' 2>/dev/null | LC_ALL=C sort
  elif [ -e "$target" ]; then
    find "$target" -maxdepth 0 -printf '%p  %T@  %s\n' 2>/dev/null
  else
    # ABSENT is a real fingerprint, not an error: a CI runner has no ~/.claude at all, and
    # "still absent" is exactly what the gate needs to assert.
    printf 'ABSENT  %s\n' "$target"
  fi
}

if [ "$#" -gt 0 ]; then
  for target in "$@"; do
    fingerprint_path "$target"
  done
  exit 0
fi

if [ ! -f "$WATCHLIST" ]; then
  echo "home-fingerprint: missing watchlist at $WATCHLIST" >&2
  exit 1
fi

while IFS= read -r line || [ -n "$line" ]; do
  entry="${line%%#*}"
  entry="$(printf '%s' "$entry" | tr -d '[:space:]')"
  [ -n "$entry" ] || continue
  fingerprint_path "$HOME/$entry"
done < "$WATCHLIST"
