"""Drift guards for the home-directory isolation (bug-560).

tests/conftest.py redirects every `~`-rooted constant to tmp_path, and CI fingerprints the paths in
scripts/home-watchlist.txt either side of the suite to prove nothing escaped. Both are lists, and a
list is only safe if something fails when it goes stale. This is that something.

The bug it guards against is not hypothetical and is not "someone forgets to update a list": what
actually happened is that PR #11 widened `cmd_setup_hook()` to write two MORE files under `~`, under
tests that were already considered safe, and from that merge onward the suite rewrote the
developer's live ~/.claude/settings.json on every run. Catching a new `~`-rooted constant HERE, at
its declaration, is earlier and more specific than waiting for the CI fingerprint to notice that
some test happened to write there.
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
WATCHLIST = REPO_ROOT / "scripts" / "home-watchlist.txt"
CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def _watched_entries() -> set[str]:
    """The top-level `~` entries CI fingerprints, from the shared watchlist file."""
    entries = set()
    for raw in WATCHLIST.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.add(line)
    return entries


def _is_path_home(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "home"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "Path"
    )


def _home_rooted_constants() -> list[tuple[str, str, str, int]]:
    """Every `NAME = Path.home() / "<entry>" / ...` in src/, as (module, NAME, entry, lineno)."""
    found: list[tuple[str, str, str, int]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            entry = _leading_home_entry(node.value)
            if entry is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.append((path.stem, target.id, entry, node.lineno))
    return found


def _leading_home_entry(node: ast.AST) -> str | None:
    """For `Path.home() / ".claude" / "settings.json"`, return '.claude'."""
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if _is_path_home(node.left):
            if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
                return node.right.value
            return None
        node = node.left
    return None


def _conftest_isolated_attrs() -> set[tuple[str, str]]:
    """(module, ATTR) pairs that isolate_home monkeypatches."""
    tree = ast.parse(CONFTEST.read_text(encoding="utf-8"), filename=str(CONFTEST))
    isolated: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "setattr"):
            continue
        module, attr = node.args[0], node.args[1]
        if isinstance(module, ast.Name) and isinstance(attr, ast.Constant) and isinstance(attr.value, str):
            isolated.add((module.id, attr.value))
    return isolated


def test_the_parse_actually_finds_the_known_constants():
    """Guards the two tests below from being vacuously true if the AST walk silently found nothing.

    Eight constants exist today (backfill 1, commands 4, config 1, maintenance 1, upload 1).
    """
    constants = _home_rooted_constants()
    assert len(constants) >= 8, f"expected the 8 known ~-rooted constants, parsed {constants}"
    assert ".claude" in {entry for _, _, entry, _ in constants}


def test_every_home_rooted_constant_is_on_the_ci_watchlist():
    watched = _watched_entries()
    missing = [(m, n, e, ln) for m, n, e, ln in _home_rooted_constants() if e not in watched]
    assert not missing, (
        "these constants write somewhere CI does not fingerprint, so a test escaping into them "
        "would go unnoticed (bug-560). Add the top-level entry to scripts/home-watchlist.txt:\n"
        + "\n".join(f"  src/terum_capture/{m}.py:{ln}  {n} -> ~/{e}" for m, n, e, ln in missing)
    )


def test_every_home_rooted_constant_is_isolated_by_conftest():
    isolated = _conftest_isolated_attrs()
    missing = [
        (m, n, e, ln) for m, n, e, ln in _home_rooted_constants() if (m, n) not in isolated
    ]
    assert not missing, (
        "these constants point into the developer's REAL home and are not redirected by "
        "conftest.py's isolate_home fixture, so any test touching them rewrites live config "
        "(bug-560). Add a monkeypatch.setattr for each:\n"
        + "\n".join(f"  src/terum_capture/{m}.py:{ln}  {n} -> ~/{e}" for m, n, e, ln in missing)
    )


def test_watchlist_has_no_stray_entries():
    """The reverse direction: an entry nothing points at is dead weight that makes the gate look
    broader than it is. Keeping it exact is what lets the fingerprint stay noise-free."""
    entries = {entry for _, _, entry, _ in _home_rooted_constants()}
    stray = _watched_entries() - entries
    assert not stray, f"watchlist entries no constant in src/ points at: {sorted(stray)}"
