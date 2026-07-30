"""Global test isolation: no test may touch the developer's REAL home directory.

Why this exists (bug-559). `tests/test_setup_hook.py` patched `_configure_hook` and believed
that made `cmd_setup_hook()` safe. Then PR #11 widened `cmd_setup_hook()` to also call
`_upsert_mcp_usage_claude_md()` and `_refresh_delivery_hook_if_installed()`, and nobody updated
the tests. So from that merge onward, **running the test suite silently rewrote the developer's
live ~/.claude/settings.json** — re-pointing the UserPromptSubmit delivery hook at whatever
interpreter ran pytest (a dev checkout's editable venv). The next time that checkout changed
branch, every prompt in Claude Code errored and in-flow delivery stopped firing, with nothing
saying why. It also rewrote ~/.claude/CLAUDE.md.

That is a whole bug CLASS, not one mistake: eight module-level constants point into `~`, and any
future function that touches one is one unpatched call away from doing it again. Patching test by
test cannot hold — the tests that broke were "already safe" until a function grew a new side
effect underneath them. So the isolation is enforced here, once, for every test, autouse.

If a test genuinely needs to assert on real-home behaviour, it must opt out explicitly rather
than this fixture opting in.
"""
import pytest

from terum_capture import backfill, commands, config, delivery_hooks, maintenance, upload


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    """Redirect every ~-rooted constant at its point of use to a per-test tmp dir.

    Patched as module ATTRIBUTES, not via HOME, because these are resolved at import time —
    ``Path.home()`` has already run by the time a test starts, so monkeypatching the env var
    would not move them.
    """
    home = tmp_path / "home"
    claude = home / ".claude"
    terum = home / ".terum"
    claude.mkdir(parents=True)
    terum.mkdir(parents=True)

    monkeypatch.setattr(commands, "CLAUDE_SETTINGS", claude / "settings.json")
    monkeypatch.setattr(commands, "CLAUDE_MD", claude / "CLAUDE.md")
    monkeypatch.setattr(commands, "CLAUDE_JSON", home / ".claude.json")
    monkeypatch.setattr(commands, "CURSOR_MCP", home / ".cursor" / "mcp.json")
    monkeypatch.setattr(backfill, "CLAUDE_PROJECTS", claude / "projects")
    monkeypatch.setattr(config, "CONFIG_DIR", terum)
    monkeypatch.setattr(config, "CONFIG_FILE", terum / "config.json")
    monkeypatch.setattr(maintenance, "TERUM_DIR", terum)
    monkeypatch.setattr(upload, "TERUM_DIR", terum)
    monkeypatch.setattr(delivery_hooks, "STATE_FILE", terum / "delivery_state.json")

    # _CallbackHandler.result is CLASS state that survives between tests, so whichever OAuth-callback
    # test ran last decides which branch `wait_for_callback` takes in the next one. That made a
    # stderr test pass alone and fail in-suite. Reset it so ordering cannot leak.
    monkeypatch.setattr(config._CallbackHandler, "result", None)

    return home
