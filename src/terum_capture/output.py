"""Stream discipline for CLI output.

The contract, which bug-559 exists because we broke:

- **stdout is the product.** What the user asked for. `status`'s report (including
  ``Status: invalid or revoked``, which is the answer to the question even though it exits
  non-zero), ``MCP connected``, a hook's JSON payload.
- **stderr is the diagnostic.** Why we could not do what was asked: usage errors, unknown
  commands, unmet preconditions, failed reinstalls.

This matters more here than in a normal CLI because Claude Code runs several of these commands
as **hooks**, and a hook is supervised:

1. A supervisor reports a failed child by surfacing its **stderr**. Put the reason on stdout and
   the user gets "Failed with non-blocking status code: No stderr output" — told that it broke
   and denied the one line saying why. That is bug-559: ``Unknown command: delivery-hook`` sat
   unread on stdout while every prompt errored and in-flow delivery never fired.
2. On ``UserPromptSubmit``, Claude Code treats a hook's stdout as **context to inject**. A
   diagnostic on stdout is not merely invisible — on any path that exits 0 it is a candidate for
   being fed to the model as though it were retrieved team content.

Route every "we cannot proceed" message through :func:`die` so no call site drifts back to
stdout. Deliberately NOT for reporting commands whose output happens to be bad news.

**A failing command must also EXIT non-zero.** bug-561: `cmd_setup` aborted on seven terminal
failures with a bare ``return``, printing ``Error: ...`` to stdout and exiting **0** — so
``setup && next-step`` ran ``next-step`` after onboarding had failed, and two of those paths had
already deleted the config. `print`-then-`return` is the same defect as `print`-then-wrong-stream,
one notch worse: there is no failure signal at all. When sweeping for this class, grep for the
`return` variant too — the bug-559 sweep grepped only ``sys.exit(1)`` and structurally could not
see these.
"""

import sys
from typing import NoReturn


def err(*lines: str) -> None:
    """Print diagnostic ``lines`` to stderr WITHOUT exiting.

    For diagnostics whose caller still owns the control flow — e.g. ``_browser_auth`` must report
    why it failed but return ``None`` so ``cmd_setup`` decides what to do, rather than exiting from
    inside a helper.
    """
    for line in lines:
        print(line, file=sys.stderr)


def die(*lines: str, code: int = 1) -> NoReturn:
    """Print diagnostic ``lines`` to stderr and exit with ``code`` (default 1).

    Mirrors argparse, which writes usage errors to stderr and exits non-zero.
    """
    err(*lines)
    sys.exit(code)
