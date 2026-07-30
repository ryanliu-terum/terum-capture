"""Tests for scripts/check_error_streams.py — the AST gate for bug-559 and bug-561.

The gate exists because two GREP sweeps for this bug class each missed sites, and both misses were
structural (the pattern could not see the variant). So these tests are deliberately written as
variants: shapes a regex would wave through, and shapes a too-eager regex would wrongly condemn.
Several cases below are the verbatim shapes of the real bugs, and the last one pins the whole
current source tree clean so a regression shows up here rather than in a user's terminal.
"""
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LINTER_PATH = REPO_ROOT / "scripts" / "check_error_streams.py"


def _load_linter():
    """Load the checker from scripts/ — it is dev tooling, deliberately not part of the wheel."""
    spec = importlib.util.spec_from_file_location("check_error_streams", LINTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


linter = _load_linter()


def rules(source: str) -> set[str]:
    """The set of rule names the checker reports for a snippet."""
    return {v.rule for v in linter.check_source(source, "snippet.py")}


# --- Rule 1: a fatal diagnostic must not go to stdout (bug-559) ----------------------------------

def test_error_print_to_stdout_is_flagged():
    assert "stdout-diagnostic" in rules('def cmd_x():\n    print("Error: no key")\n    raise SystemExit(1)\n')


def test_fstring_error_print_is_flagged():
    """The variant the FIRST grep sweep could not see."""
    assert "stdout-diagnostic" in rules('def cmd_x():\n    print(f"Error: bad {code}")\n    raise SystemExit(1)\n')


def test_unknown_command_print_is_flagged():
    """bug-559 verbatim: `Unknown command: delivery-hook` sat unread on stdout."""
    assert "stdout-diagnostic" in rules('def main():\n    print(f"Unknown command: {command}")\n')


def test_uppercase_error_prefix_is_flagged():
    """A rephrasing: a solution hardcoded to the exact string "Error:" would miss this."""
    assert "stdout-diagnostic" in rules('def cmd_x():\n    print("ERROR: broken")\n    raise SystemExit(1)\n')


def test_error_print_to_stderr_is_clean():
    src = 'import sys\ndef cmd_x():\n    print("Error: no key", file=sys.stderr)\n    sys.exit(1)\n'
    assert rules(src) == set()


def test_die_is_the_compliant_form():
    assert rules('def cmd_x():\n    die("Error: no key")\n') == set()


# --- Rule 1: the documented stdout-legitimate prefixes must NOT be flagged -----------------------

def test_warning_prefix_is_not_a_diagnostic():
    """Seven live Warning: sites are non-fatal — execution continues and the command exits 0."""
    src = 'def cmd_x():\n    print(f"Warning: Could not configure hook: {exc}")\n'
    assert rules(src) == set()


def test_status_print_then_exit_one_is_clean():
    """cmd_status's deliberate shape. Rule 1 keys on the message prefix, not on print-then-exit,
    so `Status: invalid or revoked` — the ANSWER to what status was asked — needs no exemption."""
    src = (
        'import sys\n'
        'def cmd_status():\n'
        '    print("Status: invalid or revoked (HTTP 401)")\n'
        '    sys.exit(1)\n'
    )
    assert rules(src) == set()


def test_ambiguous_opener_on_stdout_is_not_flagged():
    """"Could not open browser. Visit this URL:" is deliberate stdout — the URL is the product."""
    src = 'def cmd_x():\n    print(f"Could not open browser. Visit this URL:\\n  {url}")\n'
    assert rules(src) == set()


def test_leading_interpolation_is_a_known_blind_spot():
    """Documented limitation, pinned so it is a known gap rather than a surprise: an f-string
    that OPENS with an expression has no inspectable literal prefix."""
    assert rules('def cmd_x():\n    print(f"{label} Error: bad")\n') == set()


# --- Rule 2: a command that reports a failure must exit non-zero (bug-561) -----------------------

def test_bug561_shape_print_error_then_bare_return():
    """The verbatim pre-fix shape of all seven cmd_setup sites: both defects at once."""
    src = (
        'def cmd_setup():\n'
        '    if not ok:\n'
        '        print("Error: Round-trip verification failed. Config deleted.")\n'
        '        return\n'
        '    proceed()\n'
    )
    assert rules(src) == {"stdout-diagnostic", "failure-exits-zero"}


def test_err_then_bare_return_is_flagged():
    """The shape Rule 1 CANNOT catch: once the message correctly moves to stderr via err(),
    err()-then-return still hands a success exit code to the caller."""
    src = 'def cmd_setup():\n    if not ok:\n        err("Error: no token")\n        return\n    go()\n'
    assert rules(src) == {"failure-exits-zero"}


def test_err_then_return_value_is_flagged():
    """The variant the SECOND grep sweep could not see: print-then-`return "failed"`."""
    src = 'def cmd_setup():\n    err("Error: no token")\n    return "failed"\n'
    assert rules(src) == {"failure-exits-zero"}


def test_err_then_falling_off_the_end_is_flagged():
    """No return at all: the function ends, returns None, and the process exits 0."""
    assert rules('def cmd_setup():\n    err("Error: no token")\n') == {"failure-exits-zero"}


def test_err_then_exit_one_is_clean():
    src = 'import sys\ndef cmd_setup():\n    err("Error: no token")\n    sys.exit(1)\n'
    assert rules(src) == set()


def test_err_then_exit_zero_is_flagged():
    src = 'import sys\ndef cmd_setup():\n    err("Error: no token")\n    sys.exit(0)\n'
    assert rules(src) == {"failure-exits-zero"}


def test_err_then_raise_is_clean():
    """An uncaught exception exits non-zero, so the failure IS detectable."""
    src = 'def cmd_setup():\n    try:\n        go()\n    except OSError:\n        err("Error: io")\n        raise\n'
    assert rules(src) == set()


def test_err_then_both_branches_die_is_clean():
    """False-positive guard: control cannot fall through an if/else where both arms exit."""
    src = (
        'def cmd_setup():\n'
        '    err("Error: no token")\n'
        '    if retryable:\n'
        '        die("giving up")\n'
        '    else:\n'
        '        die("fatal")\n'
    )
    assert rules(src) == set()


def test_err_then_only_one_branch_dies_is_flagged():
    """The inverse of the above: one arm falls through, so a path still exits 0."""
    src = 'def cmd_setup():\n    err("Error: no token")\n    if retryable:\n        die("giving up")\n'
    assert rules(src) == {"failure-exits-zero"}


def test_err_in_loop_with_die_after_loop_is_clean():
    src = (
        'def cmd_setup():\n'
        '    for item in items:\n'
        '        if bad(item):\n'
        '            err("Error: bad item")\n'
        '    die("aborting")\n'
    )
    assert rules(src) == set()


def test_helper_reporting_and_returning_is_clean():
    """_configure_mcp's real contract: report via err(), return a sentinel, let cmd_* decide.
    Rule 2 is scoped to cmd_* precisely so this legitimate pattern is not condemned."""
    src = 'def _configure_mcp(client):\n    err(f"Error: unknown MCP client {client!r}.")\n    return "failed"\n'
    assert rules(src) == set()


def test_nested_function_return_is_not_the_commands_return():
    src = (
        'def cmd_setup():\n'
        '    def _on_fail():\n'
        '        return None\n'
        '    err("Error: no token")\n'
        '    die("aborting")\n'
    )
    assert rules(src) == set()


def test_helper_reports_and_caller_returns_is_a_known_blind_spot():
    """bug-561's SEVENTH site, verbatim in shape, and the one this gate does not catch.

    `_browser_auth()` reported the reason to stderr and returned None; cmd_setup answered with a
    bare `return`, so the failure exited 0 with nothing lexically wrong inside cmd_setup. Catching
    it requires deciding "the helper reported, so the caller's early return is a silent failure" —
    interprocedural and unsound in general, since cmd_setup's neighbouring "user DECLINED" return
    is a cancellation where exiting 0 is CORRECT. Pinned so the limit is known, not discovered.
    """
    src = (
        'def cmd_setup():\n'
        '    token = _browser_auth()   # reports its own reason to stderr, returns None on failure\n'
        '    if not token:\n'
        '        return\n'
        '    go(token)\n'
    )
    assert rules(src) == set()


def test_pre_fix_history_is_caught(tmp_path):
    """Replay of the real bug-561 shapes as they existed in commit 995a969's cmd_setup."""
    src = (
        'import sys\n'
        'def cmd_setup():\n'
        '    if resp.status_code == 409:\n'
        '        print("Error: You have 10 active keys. Revoke one first.")\n'
        '        return\n'
        '    if resp.status_code == 401:\n'
        '        print("Error: Token expired or invalid. Run setup again.")\n'
        '        return\n'
        '    print("Setup complete")\n'
    )
    found = linter.check_source(src, "commands.py")
    assert {v.rule for v in found} == {"stdout-diagnostic", "failure-exits-zero"}
    # Both sites, both rules — not one site standing in for the class.
    assert len([v for v in found if v.rule == "failure-exits-zero"]) == 2


# --- Rule 2: the fail-open exemption is narrow ---------------------------------------------------

def test_fail_open_hook_command_is_exempt():
    """cmd_upload is the Stop hook: a non-zero exit would break the user's Claude Code session
    over a failed background upload, so reporting to stderr and exiting 0 is CORRECT here."""
    src = (
        'import sys\n'
        'def cmd_upload():\n'
        '    try:\n'
        '        _do_upload()\n'
        '    except Exception as exc:\n'
        '        print(f"terum-capture: upload failed: {exc}", file=sys.stderr)\n'
        '    sys.exit(0)\n'
    )
    assert rules(src) == set()


def test_a_new_command_does_not_inherit_the_exemption():
    """The exemption is a named allowlist, not a shape. A new hook-looking command is flagged
    first, so exempting it stays a conscious decision."""
    src = (
        'import sys\n'
        'def cmd_newhook():\n'
        '    print("Error: broken", file=sys.stderr)\n'
        '    sys.exit(0)\n'
    )
    assert rules(src) == {"failure-exits-zero"}


def test_fail_open_list_matches_the_real_hook_entrypoints():
    """If a second command becomes a hook entrypoint, this is where the decision surfaces."""
    assert linter.FAIL_OPEN_COMMANDS == frozenset({"cmd_upload"})


# --- The regression pin -------------------------------------------------------------------------

def test_the_whole_source_tree_is_clean():
    sources = sorted((REPO_ROOT / "src").rglob("*.py"))
    assert sources, "no source files found — is the checkout complete?"
    violations = linter.check_paths(sources)
    assert not violations, "output-discipline violations:\n" + "\n".join(str(v) for v in violations)


def test_main_exits_nonzero_on_a_violation(tmp_path, capsys):
    """The gate must actually FAIL the build, not merely print. A CI step that reports a problem
    and exits 0 would be bug-561 committed a second time, in the gate for bug-561."""
    clean = tmp_path / "clean.py"
    clean.write_text('def cmd_x():\n    die("Error: nope")\n', encoding="utf-8")
    assert linter.main([str(clean)]) == 0

    dirty = tmp_path / "dirty.py"
    dirty.write_text('def cmd_x():\n    print("Error: nope")\n    return\n', encoding="utf-8")
    assert linter.main([str(dirty)]) == 1
    stderr = capsys.readouterr().err
    assert "stdout-diagnostic" in stderr and "failure-exits-zero" in stderr
