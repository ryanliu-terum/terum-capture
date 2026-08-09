"""Once-a-day upkeep, run from the Stop hook AFTER an upload completes.

Two jobs, both best-effort — every failure is swallowed so this can never delay or
break capture, and it runs at most once per 24h (guarded by a timestamp file):

  (C) Self-heal the hook config: re-apply ``_refresh_installed_hooks()`` so a stale hook
      entry (e.g. an old 15s Stop-hook timeout left behind when the package was upgraded
      but ``setup`` never re-run) converges to whatever the INSTALLED package specifies —
      no user action needed. Refresh-only: it repairs the scopes that already have a hook
      (global and/or this project) and never installs one, so a background upkeep pass can
      never widen a project-scoped capture into machine-wide capture.

  (B) Staleness check: ask the server for the latest published version, sending our own
      version for fleet telemetry. If we are behind, record a marker (surfaced by
      ``status``) and print a one-line nag. Fails open — a missing endpoint or offline
      network simply means no nag, never a crash.
"""
import sys
import time
from pathlib import Path

import httpx

from terum_capture import __version__

TERUM_DIR = Path.home() / ".terum"
STAMP_FILE = TERUM_DIR / ".last_maintenance"
UPDATE_MARKER = TERUM_DIR / ".update_available"
CHECK_INTERVAL_SECONDS = 24 * 3600
CHECK_TIMEOUT = 3.0


def _due(now: float) -> bool:
    try:
        return now - STAMP_FILE.stat().st_mtime >= CHECK_INTERVAL_SECONDS
    except OSError:
        return True  # no stamp yet -> due


def _touch_stamp(now: float) -> None:
    try:
        TERUM_DIR.mkdir(parents=True, exist_ok=True)
        STAMP_FILE.write_text(str(int(now)))
    except OSError:
        pass


def _version_tuple(v: str) -> tuple:
    parts = []
    for p in v.strip().lstrip("v").split("."):
        # Split off any pre-release/build suffix (e.g. "2rc1") -> leading digits only.
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    """True if ``latest`` is a strictly higher version than ``current`` (dotted-int compare)."""
    if not latest:
        return False
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except Exception:
        return False


def read_update_available() -> str | None:
    """The pending newer version recorded by the last check, or None. Used by ``status``."""
    try:
        v = UPDATE_MARKER.read_text().strip()
    except OSError:
        return None
    return v if is_newer(v, __version__) else None


def _self_heal_hook() -> None:
    try:
        from terum_capture.commands import _refresh_installed_hooks
        _refresh_installed_hooks()
    except Exception:
        pass


def _check_latest_version(config: dict) -> str | None:
    """Returns the newer version string when we are behind, else None (incl. every failure)."""
    if (
        not config
        or not config.get("api_url")
        or not config.get("api_key", "").startswith("trm_")
    ):
        return None
    try:
        resp = httpx.get(
            f"{config['api_url']}/capture/latest-version",
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "X-Terum-Capture-Version": __version__,
            },
            timeout=CHECK_TIMEOUT,
        )
    except Exception:
        return None  # offline / DNS / timeout -> fail open, no nag
    if resp.status_code != 200:
        return None  # endpoint not deployed yet (404) or transient -> fail open
    try:
        latest = str(resp.json().get("version", "")).strip()
    except Exception:
        return None
    if is_newer(latest, __version__):
        try:
            UPDATE_MARKER.write_text(latest)
        except OSError:
            pass
        print(
            f"terum-capture: a newer version ({latest}) is available — "
            f"run 'terum-capture update' (you have {__version__}).",
            file=sys.stderr,
        )
        return latest
    # We're current — clear any stale marker so `status` stops nagging.
    try:
        UPDATE_MARKER.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def run_daily_maintenance(config: dict | None) -> str | None:
    """Best-effort; never raises. Call AFTER the upload, guarded by the caller's try too.

    Returns the newer version string when THIS run's staleness check found one, else None —
    the Stop hook uses that to surface a user-visible nag (systemMessage) at most once per
    check interval. Deliberately NOT the persisted marker: the marker outlives the check by
    up to a day, and the Stop hook fires on every assistant turn, so keying the nag off the
    marker would print it every turn (wallpaper, the same failure mode INSTRUCTION_EVERY_N
    exists to avoid).
    """
    try:
        now = time.time()
        if not _due(now):
            return None
        # Stamp BEFORE the work so a hard-down server or a slow check can never make this
        # run every turn — at most one attempt per interval regardless of outcome.
        _touch_stamp(now)
        _self_heal_hook()
        return _check_latest_version(config)
    except Exception:
        return None
