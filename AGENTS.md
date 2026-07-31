# AGENTS.md — terum-capture

Instructions for non-Claude coding agents (Codex CLI, etc.) working in this repo.

**This file is a loader, not the source of truth.** Unlike Terum-MVP, this repo has no
per-directory instruction files — the authoritative statements of each invariant are the module
docstrings named below. Read the cited module before your first edit in it.

**Your role here is implementer, not architect.** You are normally invoked against a spec in
Terum-MVP's `.planning/specs/` that was written and locked upstream. Implement what the spec says.
**If the spec is ambiguous, STOP and record the question — do not resolve the fork yourself.**

**What this is:** a Python CLI that installs a Claude Code `Stop` hook, parses new turns out of
Claude Code transcripts, and POSTs them to Terum's ingest pipeline. It is `pipx`-installed on real
developer machines and **runs supervised, as a hook**. Both of those shape every rule below.

---

## Top invariants

### 1. The sidecar offset advances ONLY after a confirmed 2xx

This is the anti-data-loss invariant. Authoritative: the `_write_sidecar` and `_process_transcript`
docstrings in `src/terum_capture/upload.py`.

Each transcript has a sidecar at `~/.terum/sent_<session_id>` holding the byte offset already
uploaded. Advance it before the POST succeeds and those turns are skipped **permanently** — there
is no second chance, because the next run reads from the advanced offset. The return vocabulary
encodes exactly this and must be preserved:

| status | meaning | sidecar |
| --- | --- | --- |
| `uploaded` | every event POSTed with a 2xx | advanced |
| `skipped` | nothing new (`file_size <= last offset`) | unchanged, no POST |
| `no_turns` | new bytes, no qualifying turns | advanced |
| `rate_limited` | a 429 | **NOT advanced** — caller backs off and retries |
| `failed` | non-2xx / non-429 / transport error | **NOT advanced** |

Corollaries, both already implemented — do not "simplify" either away:
- The sidecar write goes through a temp file + `os.replace`, so a kill mid-write cannot leave a
  torn file. A truncated or unparseable sidecar resets to offset 0 — a full, safe reprocess. The
  server dedups, so re-sending is cheap; a false "already sent" is unrecoverable. **When the two
  failure modes trade off, always choose re-send.**
- Auxiliary fields (`repo`, `tokens`) may be written with an UNCHANGED offset. That is safe by
  construction and is not a violation of this rule.

### 2. stdout is the product; stderr is the diagnostic

Authoritative: the module docstring of `src/terum_capture/output.py`. Gated by
`scripts/check_error_streams.py` (AST, its own CI job).

- **stdout** = what the user asked for. `status`'s report, `MCP connected`, a hook's JSON payload.
  This includes reports whose content is bad news — `Status: invalid or revoked` is the *answer*.
- **stderr** = why we could not do what was asked: usage errors, unknown commands, unmet
  preconditions, failed reinstalls. Route every one through `output.die()` or `output.err()`.

Why it matters more here than in a normal CLI — a hook is **supervised**:
1. A supervisor reports a failed child by surfacing its **stderr**. Put the reason on stdout and
   the user sees "Failed with non-blocking status code: No stderr output" — told it broke and
   denied the one line saying why. That is bug-559.
2. On `UserPromptSubmit`, Claude Code treats a hook's stdout as **context to inject**. A diagnostic
   on stdout is not merely invisible — on any path exiting 0 it is a candidate for being fed to the
   model as though it were retrieved team content.

### 3. A command that reports a failure must EXIT non-zero

bug-561: `cmd_setup` aborted on seven terminal failures with a bare `return`, printing `Error: …`
to stdout and exiting **0** — so `setup && next-step` ran `next-step` over a failed onboarding, and
two of those paths had already deleted the config.

`print`-then-`return` is the same defect as `print`-then-wrong-stream, one notch worse: there is no
failure signal at all. **When sweeping for this class, grep the `return` variant too** — the
bug-559 sweep grepped only `sys.exit(1)` and structurally could not see these.

### 4. The test suite must never touch the real `$HOME`

bug-560: running `pytest` rewrote the developer's real `~/.claude/settings.json`, breaking every
Claude Code prompt after the next branch switch. `tests/conftest.py` has an **autouse**
`isolate_home` fixture that monkeypatches every `~`-rooted constant at its module to a `tmp_path`.

**If you add a new `~`-rooted module constant, patch it into `isolate_home` in the same commit.**
CI fingerprints `$HOME` before and after the suite and fails on any diff. The watchlist
(`scripts/home-watchlist.txt`) is the single source of truth for both that script and
`tests/test_home_isolation_coverage.py`, which fails if a `Path.home()` constant in `src/` names an
entry missing from it.

**Do not reproduce that check locally and trust the result.** CI is hermetic; your machine is not.
Running `scripts/home-fingerprint.sh` around a local `pytest` reports a diff from whatever else is
writing under `~/.claude` at the time — Claude Code's own `.last-cleanup`, `.claude.json` backup
rotation, plugin-cache markers, session transcripts. Verified 2026-07-31: a local run showed six
changed paths and **none** were suite-owned. Before concluding the isolation regressed, check
whether the changed paths are actually in the watchlist (`.claude/settings.json`, `.claude.json`,
`.cursor`, `.terum`); if they are not, it is your environment, not the suite.

### 5. Partial work must never report "complete"

A run that skipped anything reports it, does not advance state past unfetched items, and surfaces
the partial state. The status vocabulary in invariant 1 *is* this mechanism — a helper that returns
an empty result on a non-OK status is silent data loss. Return an explicit stub or raise so retry
engages.

### 6. `delivery_hooks.py` is FAIL-OPEN BY CONSTRUCTION

Any error — no config, unreachable backend, timeout, bad payload — must degrade to injecting
nothing. It runs on `UserPromptSubmit`, so a raise or a slow path blocks the user's prompt. Its
swallowed errors are **deliberate and load-bearing**; do not "fix" them into raises. Read the module
docstring before touching it.

---

## Gates — run these yourself before reporting done

```bash
pip install ".[dev]"                    # pytest is in the `dev` extra, not a runtime dependency
pytest -q
python scripts/check_error_streams.py   # AST gate for invariants 2 and 3, stdlib only
```

There is **no** ruff, mypy, or npm gate in this repo — do not invent one, and do not import the
Terum-MVP `npm run lint / typecheck / test` battery. `requires-python` is `>=3.10` and CI runs the
suite on 3.10–3.14; if your change is version-sensitive, say which interpreters you actually ran.

**Report real counts, not impressions.** Record the pass/fail baseline BEFORE your change so "no
regressions" is a recorded diff (`baseline 2 failing {a,b} → still 2 {a,b}`), not a feeling. If a
gate fails for a reason you did not introduce, say so explicitly.

---

## Bug logs live in the OTHER repo

This repo has no `.planning/`. Bug logs for terum-capture are filed in **Terum-MVP** at
`.planning/debug/capture-cli/`, and bug numbers are global across both repos.

You cannot allocate a number from here — `scripts/next-bug-number.sh` is in Terum-MVP. **Never
grep or `ls` for the next number** (that is a known collision race). If you find a bug, describe it
in your report and let the caller allocate the number.

---

## Git discipline

- **Stage only the files your task touched.** Concurrent agent sessions and worktrees share this
  `.git`. Never `git add -A`; flag unrelated changes as follow-ups.
- Never commit or push unless explicitly asked. Never add `Co-Authored-By` lines.
- If you are in a worktree, stay in it.
- Unlike Terum-MVP, this repo has **no `.githooks/pre-push` gate** and commits land on `main`
  directly — CI on `main` is the only thing standing behind a bad push. Do not treat a green local
  run as equivalent.

---

## Hard stops — ask, do not proceed

- Anything that writes outside the repo: `~/.terum/`, `~/.claude/`, `~/.claude.json`,
  `~/.cursor/mcp.json`. These are the user's live config; the whole point of invariant 4 is that
  even the *tests* must not touch them.
- Changing the wire contract with Terum-MVP's ingest endpoint (event payload shape, auth header,
  `api_url` semantics). That is a coordinated two-repo change.
- Adding a runtime dependency. `httpx` is the only one, deliberately — this is `pipx`-installed on
  developer machines and every addition is a new install failure mode.
- Publishing, tagging, or bumping `__version__`. The release order is load-bearing and spans both
  repos: bump `__version__` → merge → **tag** → *only then* bump `LATEST_CAPTURE_VERSION` in
  Terum-MVP. That constant is the fleet's rollout trigger, so pointing it at an untagged version
  tells every client to install something that does not exist. Drifting it the other way is
  bug-576 — 0.3.0 and 0.4.0 both shipped while it still said `0.2.0`, so no user was ever nagged.
  Terum-MVP's `npm run check:capture-version-sync` is the lockstep gate.
- Anything the spec did not authorize.

---

## Reporting back

Finish with: what you changed (file list), the real gate output including which interpreters ran,
any place the spec was ambiguous and what you did about it, and the single claim you are least
confident is correct.
