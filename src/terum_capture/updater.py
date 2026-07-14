"""`terum-capture update`: reinstall the CLI from git, then refresh the hook config.

Self-updating a running process is a two-step dance. We reinstall the package
(replacing this very code on disk), then invoke the NEWLY installed code in a fresh
subprocess to rewrite the Stop-hook config — so a shipped change to the hook timeout
or command actually reaches the user's ~/.claude/settings.json. The running process
keeps its old code in memory; that is fine, it exits right after.

The version string is frozen historically (0.1.0 forever), so a plain `pipx upgrade`
would no-op; we force a reinstall from git HEAD regardless of the reported version.
"""
import shutil
import subprocess
import sys

from terum_capture import __version__

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


def _reinstall_cmd() -> list[str]:
    if _pipx_manages_this():
        # --force reinstalls even though pipx thinks the (frozen) version is current.
        return ["pipx", "install", "--force", REPO_URL]
    # pip branch: --force-reinstall because the version string may not have changed.
    return [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", REPO_URL]


def cmd_update():
    print(f"terum-capture {__version__} — reinstalling from git HEAD ...")
    cmd = _reinstall_cmd()
    try:
        result = subprocess.run(cmd, timeout=300)
    except FileNotFoundError:
        print(f"Error: could not run {cmd[0]!r}. Is it installed and on your PATH?")
        sys.exit(1)
    except subprocess.SubprocessError as exc:
        print(f"Error: reinstall failed to run: {exc}")
        sys.exit(1)
    if result.returncode != 0:
        print(f"Error: reinstall failed (exit {result.returncode}).")
        sys.exit(1)

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
