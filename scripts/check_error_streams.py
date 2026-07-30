#!/usr/bin/env python3
"""AST gate for the two output-discipline rules that bug-559 and bug-561 established.

Rule 1 — a fatal diagnostic never goes to stdout.
    `print("Error: ...")` is a bug: Claude Code runs several of these commands as hooks, and a
    supervisor reports a failed child by surfacing its STDERR. Put the reason on stdout and the
    user gets "Failed with non-blocking status code: No stderr output" — told that it broke and
    denied the one line saying why (bug-559). Worse, on UserPromptSubmit stdout is treated as
    context to INJECT, so a diagnostic there is a candidate for being fed to the model as if it
    were retrieved team content. Route it through output.err()/output.die().

Rule 2 — a command that reports a failure must EXIT non-zero.
    bug-561: `cmd_setup` aborted on seven terminal failures with a bare `return`, printing
    `Error:` and exiting 0 — so `setup && next-step` ran next-step after onboarding had failed,
    and two of those paths had already deleted the config. Rule 1 alone does not catch this: once
    the message correctly moves to err(), `err(...)` + `return` still exits 0.

Why this is an AST check and not a grep. Two sweeps for this class each missed sites, and both
misses were STRUCTURAL — the pattern chosen could not see the variant. The first grepped
`print(...)` near `sys.exit(1)` and missed print-then-bare-`return` (7 sites in cmd_setup). The
second missed print-then-`return "failed"` (_configure_mcp). A third regex would have its own
blind spot; `return` vs `sys.exit` vs `raise` is a question about control flow, so it needs a
parser. Grep is the tool that failed here, twice.

Deliberately NOT flagged, each verified against the real source:

- `Warning:` and `Status:` prefixes on stdout. Seven `Warning:` messages are non-fatal — execution
  continues and the command exits 0, so nothing is concealed from a supervisor. `Status: invalid
  or revoked` is the ANSWER to what `status` was asked, belongs beside the Key:/API: lines it is
  part of, and its non-zero exit is the machine-readable signal. Because Rule 1 keys on the
  message PREFIX rather than on "print followed by an exit", cmd_status needs no exemption.
- Ambiguous openers like "Could not ..." are not in the prefix list. Two live sites print exactly
  that to stdout on purpose ("Could not open browser. Visit this URL:" — the URL IS the product
  the user must click). A rule that flags those trains people to disable the rule.
- Hook entrypoints are exempt from Rule 2 (see FAIL_OPEN_COMMANDS). A Stop / UserPromptSubmit
  hook is FAIL-OPEN BY CONSTRUCTION: `cmd_upload` reports to stderr and then `sys.exit(0)`
  deliberately, because a non-zero exit there would break the user's Claude Code session over a
  failed background upload. Requiring non-zero there would be the opposite of correct.

Measured coverage, by replaying the checker against the pre-fix source in git history: it finds
all eight bug-559 stdout diagnostics in cli.py (including the `Unknown command:` line that started
it) and six of bug-561's seven cmd_setup sites.

The seventh is a KNOWN BLIND SPOT, and worth stating plainly rather than rounding up. That site
had no report call inside cmd_setup at all: `_browser_auth()` reported the reason to stderr and
returned None, and cmd_setup answered with a bare `return`, so the failure was invisible with
nothing lexically wrong in cmd_setup. Catching it means deciding that "helper reported to stderr,
therefore the caller's early return is a silent failure" — interprocedural, and unsound in
general, because plenty of helpers legitimately warn on a path the caller then abandons on
purpose (cmd_setup's own "user DECLINED" return is a cancellation, and exiting 0 there is
correct). Rule 2 stays intraprocedural so it can stay trustworthy; test_error_stream_lint.py pins
this gap with the real shape so it is a known limit, not a surprise.

Usage:
    python scripts/check_error_streams.py            # scan src/, exit 1 on any violation
    python scripts/check_error_streams.py FILE ...   # scan specific files
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

# Message openers that mean "we cannot do what was asked". Kept deliberately short: every entry
# must be unambiguously fatal, because the cost of a false positive is that someone deletes the
# gate. `Warning`/`Status` are the documented stdout-legitimate prefixes and are absent by design.
DIAGNOSTIC_PREFIXES = ("Error", "ERROR", "Fatal", "Unknown command", "Usage:")

# Commands invoked BY a Claude Code hook, where exiting non-zero would break the user's session.
# Their contract is to report on stderr and exit 0. Adding to this set should be a conscious act:
# a new hook entrypoint gets flagged by Rule 2 first, which is the right direction to fail.
FAIL_OPEN_COMMANDS = frozenset({"cmd_upload"})

REPORTERS = frozenset({"err", "die"})
EXITERS = frozenset({"exit", "_exit"})


class Violation:
    def __init__(self, path: Path, line: int, rule: str, message: str) -> None:
        self.path, self.line, self.rule, self.message = path, line, rule, message

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


def _func_name(call: ast.Call) -> str:
    """`err(...)` -> 'err', `output.err(...)` -> 'err', `sys.exit(...)` -> 'exit'."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _leading_text(node: ast.expr | None) -> str:
    """The literal head of a printed message: "Error: x" and f"Error: {x}" both yield 'Error:'."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]  # only the literal HEAD counts; f"{x} Error" is not a diagnostic
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return ""


def _is_diagnostic(call: ast.Call) -> bool:
    first = call.args[0] if call.args else None
    return _leading_text(first).lstrip().startswith(DIAGNOSTIC_PREFIXES)


def _goes_to_stderr(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "file" and isinstance(kw.value, ast.Attribute) and kw.value.attr == "stderr":
            return True
    return False


def _exit_code(call: ast.Call) -> int | None:
    """The literal exit code, or None when it is absent or computed (treated as non-zero)."""
    if not call.args:
        return 0  # sys.exit() with no argument exits 0
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
        return arg.value
    return None


def _statement_call(stmt: ast.stmt) -> ast.Call | None:
    """A bare call used as a statement — the only shape a report or an exit takes."""
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        return stmt.value
    return None


def _child_blocks(stmt: ast.stmt) -> list[list[ast.stmt]]:
    blocks: list[list[ast.stmt]] = []
    for field in ("body", "orelse", "finalbody"):
        block = getattr(stmt, field, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            blocks.append(block)
    for handler in getattr(stmt, "handlers", []) or []:
        blocks.append(handler.body)
    return blocks


def _exits_nonzero(stmt: ast.stmt) -> bool:
    """Does this single statement guarantee a non-zero exit?"""
    if isinstance(stmt, ast.Raise):
        return True  # an uncaught exception exits non-zero
    call = _statement_call(stmt)
    if call is not None:
        name = _func_name(call)
        if name == "die":
            return True  # output.die() is err() + sys.exit(1), typed NoReturn
        if name in EXITERS:
            return _exit_code(call) != 0
    if isinstance(stmt, ast.If):
        # Both arms must exit, or control can still fall through. This is what keeps
        # `err(...)` followed by `if x: die(a) else: die(b)` from being a false positive.
        return bool(stmt.orelse) and _reaches_nonzero_exit(stmt.body) and _reaches_nonzero_exit(stmt.orelse)
    return False


def _reaches_nonzero_exit(tail: list[ast.stmt]) -> bool:
    """Walk what runs after a failure report and decide whether it must exit non-zero.

    Conservative on purpose — it errs toward reporting a violation. A false positive is fixed by
    routing through die(), which is what the code should say anyway; a false negative silently
    reintroduces bug-561's exit-0-on-failure.
    """
    for stmt in tail:
        if _exits_nonzero(stmt):
            return True
        if isinstance(stmt, ast.Return):
            return False  # the bug-561 shape: report, then hand a success exit back to the caller
        call = _statement_call(stmt)
        if call is not None and _func_name(call) in EXITERS and _exit_code(call) == 0:
            return False
    return False  # fell off the end: the function returns None and the process exits 0


def _collect_reports(
    stmts: list[ast.stmt], after: list[ast.stmt], sink: list[tuple[ast.Call, list[ast.stmt]]]
) -> None:
    """Pair every failure report with the statements that run after it (its continuation)."""
    for index, stmt in enumerate(stmts):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # a nested def has its own control flow; its returns are not this one's
        tail = stmts[index + 1:] + after
        call = _statement_call(stmt)
        if call is not None:
            name = _func_name(call)
            reports_failure = name == "err" or (
                name == "print" and (_goes_to_stderr(call) or _is_diagnostic(call))
            )
            if reports_failure:
                sink.append((call, tail))
        for block in _child_blocks(stmt):
            _collect_reports(block, tail, sink)


def check_source(source: str, path: str | Path = "<source>") -> list[Violation]:
    """Return every output-discipline violation in one module's source."""
    path = Path(path)
    tree = ast.parse(source, filename=str(path))
    violations: list[Violation] = []

    # Rule 1 — a fatal diagnostic on stdout, anywhere in the module.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _func_name(node) == "print":
            if _is_diagnostic(node) and not _goes_to_stderr(node):
                head = _leading_text(node.args[0] if node.args else None).strip()
                violations.append(Violation(
                    path, node.lineno, "stdout-diagnostic",
                    f'print("{head[:40]}...") writes a fatal diagnostic to stdout. A supervising '
                    f"hook surfaces stderr, so this reason is invisible when it matters (bug-559). "
                    f"Use output.die() to report and exit, or output.err() when the caller owns "
                    f"the control flow.",
                ))

    # Rule 2 — a cmd_* that reports a failure must exit non-zero.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("cmd_") or node.name in FAIL_OPEN_COMMANDS:
            continue
        reports: list[tuple[ast.Call, list[ast.stmt]]] = []
        _collect_reports(node.body, [], reports)
        for call, tail in reports:
            if _reaches_nonzero_exit(tail):
                continue
            violations.append(Violation(
                path, call.lineno, "failure-exits-zero",
                f"{node.name}() reports a failure here and then exits 0 — no caller can detect "
                f"it, so `terum-capture <cmd> && next-step` runs next-step anyway (bug-561). End "
                f"this path with output.die(), sys.exit(1) or a raise. If this command is a Claude "
                f"Code hook and must stay fail-open, add it to FAIL_OPEN_COMMANDS with the reason.",
            ))

    return sorted(violations, key=lambda v: (v.line, v.rule))


def check_paths(paths: list[Path]) -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(paths):
        violations.extend(check_source(path.read_text(encoding="utf-8"), path))
    return violations


def main(argv: list[str]) -> int:
    if argv:
        targets = [Path(a) for a in argv]
    else:
        root = Path(__file__).resolve().parent.parent
        targets = sorted((root / "src").rglob("*.py"))
    if not targets:
        print("check_error_streams: no Python files to check", file=sys.stderr)
        return 1

    violations = check_paths(targets)
    for violation in violations:
        print(str(violation), file=sys.stderr)
    if violations:
        print(
            f"\n{len(violations)} output-discipline violation(s). "
            f"See the contract in src/terum_capture/output.py.",
            file=sys.stderr,
        )
        return 1
    print(f"check_error_streams: {len(targets)} file(s) clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
