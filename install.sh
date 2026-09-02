#!/usr/bin/env bash
#
# terum-capture installer
# Usage: curl -fsSL https://raw.githubusercontent.com/ryanliu-terum/terum-capture/main/install.sh | bash
#
# Installs the terum-capture CLI from PyPI via pipx, falling back to uv if the
# pipx path fails. Notably: on macOS 26.1/26.2, Homebrew Pythons are broken
# (`platform.mac_ver()` returns empty — the same defect family as
# Homebrew/homebrew-core#277330), so the pipx phase always fails there:
# pipx >=1.16 creates venvs through uv, and uv refuses a broken interpreter.
# uv's managed Pythons are self-contained and unaffected.
# It does NOT run `terum-capture setup` (setup needs an interactive terminal).

set -euo pipefail

PKG="terum-capture"
LOG="$(mktemp "${TMPDIR:-/tmp}/terum-capture-install.XXXXXX")"

err()  { printf '\033[31merror:\033[0m %s\n' "$1" >&2; }
info() { printf '\033[36m==>\033[0m %s\n' "$1"; }

# Print a one-line reason from the captured output of a failed phase:
# the last "Caused by:" line if present, else the first "error"-ish line.
fail_reason() {
  local reason
  reason="$(grep -E '^[[:space:]]*Caused by:' "$LOG" 2>/dev/null | tail -1 \
    | sed -E 's/^[[:space:]]*Caused by:[[:space:]]*//' || true)"
  if [ -z "$reason" ]; then
    reason="$(grep -iE 'error|failed' "$LOG" 2>/dev/null | head -1 || true)"
  fi
  if [ -n "$reason" ]; then
    printf '    reason: %s\n' "$reason"
  fi
}

# --- find a Python >= 3.10 -------------------------------------------------
find_python() {
  for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        command -v "$cand"
        return 0
      fi
    fi
  done
  return 1
}

# --- install path 1: pipx --------------------------------------------------
install_with_pipx() {
  if ! command -v pipx >/dev/null 2>&1; then
    info "pipx not found — installing it."
    if command -v brew >/dev/null 2>&1; then
      brew install pipx >>"$LOG" 2>&1 || return 1
    else
      "$PYBIN" -m pip install --user pipx >>"$LOG" 2>&1 || return 1
    fi
    "$PYBIN" -m pipx ensurepath >/dev/null 2>&1 || pipx ensurepath >/dev/null 2>&1 || true
  fi

  local pipx_cmd
  pipx_cmd="$(command -v pipx || echo "$PYBIN -m pipx")"

  $pipx_cmd install --python "$PYBIN" --force "$PKG" >>"$LOG" 2>&1
}

# --- install path 2: uv (fallback) -----------------------------------------
# uv's managed Pythons are self-contained, so they are immune to the broken
# Homebrew Pythons on macOS 26.1/26.2 (empty mac_ver / pyexpat defects) that
# doom the pipx phase.
install_with_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    info "uv not found — installing it."
    if command -v brew >/dev/null 2>&1; then
      brew install uv >>"$LOG" 2>&1 || return 1
    else
      curl -LsSf https://astral.sh/uv/install.sh | sh >>"$LOG" 2>&1 || return 1
      export PATH="$HOME/.local/bin:$PATH"
    fi
  fi

  uv tool install --managed-python --force "$PKG" >>"$LOG" 2>&1
}

PYBIN="$(find_python || true)"
if [ -z "${PYBIN:-}" ]; then
  err "Python 3.10+ is required but not found."
  err "Install it (e.g. 'brew install python@3.12' or via pyenv) and re-run."
  exit 1
fi
info "Using Python: $PYBIN ($("$PYBIN" --version 2>&1))"

# --- install terum-capture -------------------------------------------------
info "Attempting pipx installation..."
if install_with_pipx; then
  info "pipx installation succeeded."
else
  info "pipx installation failed, trying uv fallback..."
  fail_reason
  if install_with_uv; then
    info "uv fallback succeeded."
  else
    err "uv fallback failed too."
    fail_reason
    echo
    err "Full installer output:"
    cat "$LOG" >&2
    err "Please report this: https://github.com/ryanliu-terum/terum-capture/issues"
    exit 1
  fi
fi
rm -f "$LOG"

echo
info "terum-capture installed — run 'terum-capture setup' to link to your Terum account."
echo "(If 'terum-capture' is not found, restart your shell — the installer updated your PATH.)"
