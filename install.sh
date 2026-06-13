#!/usr/bin/env bash
#
# terum-capture installer
# Usage: curl -fsSL https://raw.githubusercontent.com/ryanliu-terum/terum-capture/main/install.sh | bash
#
# Installs the terum-capture CLI via pipx, then prints next steps.
# It does NOT run `terum-capture setup` (setup needs an interactive terminal).

set -euo pipefail

REPO="git+https://github.com/ryanliu-terum/terum-capture"

err()  { printf '\033[31merror:\033[0m %s\n' "$1" >&2; }
info() { printf '\033[36m==>\033[0m %s\n' "$1"; }

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

if ! command -v git >/dev/null 2>&1; then
  err "git is required but not found. Install git and re-run."
  exit 1
fi

PYBIN="$(find_python || true)"
if [ -z "${PYBIN:-}" ]; then
  err "Python 3.10+ is required but not found."
  err "Install it (e.g. 'brew install python@3.12' or via pyenv) and re-run."
  exit 1
fi
info "Using Python: $PYBIN ($("$PYBIN" --version 2>&1))"

# --- ensure pipx -----------------------------------------------------------
if ! command -v pipx >/dev/null 2>&1; then
  info "pipx not found — installing it."
  if command -v brew >/dev/null 2>&1; then
    brew install pipx
  else
    "$PYBIN" -m pip install --user pipx
  fi
  "$PYBIN" -m pipx ensurepath >/dev/null 2>&1 || pipx ensurepath >/dev/null 2>&1 || true
fi

PIPX="$(command -v pipx || echo "$PYBIN -m pipx")"

# --- install terum-capture -------------------------------------------------
info "Installing terum-capture from $REPO"
$PIPX install --python "$PYBIN" --force "$REPO"

echo
info "terum-capture installed."
echo
echo "Next step — run setup (opens your browser to log in):"
echo
echo "    terum-capture setup"
echo
echo "If 'terum-capture' is not found, restart your shell (pipx updated your PATH)."
