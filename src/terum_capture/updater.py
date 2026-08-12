"""`terum-capture update`: reinstall the CLI from git, then refresh the hook config.

Self-updating a running process is a two-step dance. We reinstall the package
(replacing this very code on disk), then invoke the NEWLY installed code in a fresh
subprocess to rewrite the Stop-hook config — so a shipped change to the hook timeout
or command actually reaches the user's ~/.claude/settings.json. The running process
keeps its old code in memory; that is fine, it exits right after.

Release versions don't bump on every commit (and were frozen at 0.1.0 historically),
so a plain `pipx upgrade` could no-op; we force a reinstall from git HEAD regardless
of the reported version.
"""
import shutil
import subprocess
import sys

from terum_capture import __version__
from terum_capture.output import die

REPO_URL = "git+https://github.com/ryanliu-terum/terum-capture"


def _pipx_manages_this() -> bool:
    """True when this interpreter lives inside a pipx-managed venv.

    pipx installs each app into ``.../pipx/venvs/<app>/``; the running interpreter's
    prefix contains that segment. We also require the ``pipx`` launcher on PATH, since
    ``update`` shells out to it. Anything else (a plain ``pip install`` into a venv, a
    dev editable install) takes the pip branch.
    """
    if not shutil.which("pipx"):
        return False
    prefix = sys.prefix.replace("\\", "/").lower()
    return "/pipx/venvs/" in prefix


def _uv_manages_this() -> bool:
    """True when this interpreter lives inside a uv tool venv.

    ``uv tool install`` places each tool in ``.../uv/tools/<app>/``. uv tool venvs
    are created WITHOUT pip, so the pip branch below would die with "No module
    named pip" — updates must shell out to ``uv`` itself.
    """
    if not shutil.which("uv"):
        return False
    prefix = sys.prefix.replace("\\", "/").lower()
    return "/uv/tools/" in prefix


def _reinstall_cmd() -> list[str]:
    if _pipx_manages_this():
        # --force reinstalls even though pipx thinks the (unchanged) version is current.
        return ["pipx", "install", "--force", REPO_URL]
    if _uv_manages_this():
        # Mirrors the pipx branch. uv re-resolves the interpreter on --force,
        # preferring its managed Pythons when any are installed.
        return ["uv", "tool", "install", "--force", REPO_URL]
    # pip branch: --force-reinstall because the version string may not have changed.
    return [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", REPO_URL]


def cmd_update():
    print(f"terum-capture {__version__} — reinstalling from git HEAD ...")
    cmd = _reinstall_cmd()
    try:
        result = subprocess.run(cmd, timeout=300)
    except FileNotFoundError:
        die(f"Error: could not run {cmd[0]!r}. Is it installed and on your PATH?")
    except subprocess.SubprocessError as exc:
        die(f"Error: reinstall failed to run: {exc}")
    if result.returncode != 0:
        die(f"Error: reinstall failed (exit {result.returncode}).")

    # Refresh the Stop-hook config using the JUST-installed code — this process still
    # holds the OLD code in memory, so a new hook timeout/command only lands in
    # settings.json when the freshly installed interpreter writes it.
    try:
        subprocess.run([sys.executable, "-m", "terum_capture", "setup-hook"], timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Updated, but could not refresh the hook automatically: {exc}")
        print("Run: terum-capture setup-hook")
        return
    print("Update complete. Restart any open Claude Code sessions to load the new hook.")
