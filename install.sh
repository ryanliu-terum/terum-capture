#!/usr/bin/env bash
#
# terum-capture installer
# Usage: curl -fsSL https://raw.githubusercontent.com/ryanliu-terum/terum-capture/main/install.sh | bash
#
# Installs the terum-capture CLI via pipx, falling back to uv if the pipx path
# fails (notably: Homebrew's python@3.13/3.14 can't load pyexpat on macOS
# 26.1/26.2, which breaks pipx's first-run bootstrap — Homebrew/homebrew-core#277330).
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

# --- install path 1: pipx --------------------------------------------------
install_with_pipx() {
  if ! command -v pipx >/dev/null 2>&1; then
    info "pipx not found — installing it."
    if command -v brew >/dev/null 2>&1; then
      brew install pipx || return 1
    else
      "$PYBIN" -m pip install --user pipx || return 1
    fi
    "$PYBIN" -m pipx ensurepath >/dev/null 2>&1 || pipx ensurepath >/dev/null 2>&1 || true
  fi

  local pipx_cmd
  pipx_cmd="$(command -v pipx || echo "$PYBIN -m pipx")"

  info "Installing terum-capture from $REPO (via pipx)"
  $pipx_cmd install --python "$PYBIN" --force "$REPO"
}

# --- install path 2: uv (fallback) -----------------------------------------
# uv's managed Pythons bundle their own expat, so they are immune to the
# Homebrew pyexpat/libexpat mismatch that breaks pipx on macOS 26.1/26.2.
install_with_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    info "uv not found — installing it."
    if command -v brew >/dev/null 2>&1; then
      brew install uv || return 1
    else
      curl -LsSf https://astral.sh/uv/install.sh | sh || return 1
      export PATH="$HOME/.local/bin:$PATH"
    fi
  fi

  info "Installing terum-capture from $REPO (via uv)"
  uv tool install --managed-python --force "$REPO"
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

# --- install terum-capture -------------------------------------------------
if ! install_with_pipx; then
  echo
  err "pipx install failed."
  info "On macOS 26.1/26.2 this is usually Homebrew's Python being unable to load"
  info "pyexpat (Homebrew/homebrew-core#277330), which breaks pipx's bootstrap."
  info "Falling back to uv, whose managed Pythons are unaffected."
  echo
  if ! install_with_uv; then
    err "uv install failed too. Please report this:"
    err "  https://github.com/ryanliu-terum/terum-capture/issues"
    exit 1
  fi
fi

echo
info "terum-capture installed."
echo
echo "Next step — run setup (opens your browser to log in):"
echo
echo "    terum-capture setup"
echo
echo "If 'terum-capture' is not found, restart your shell (the installer updated your PATH)."
