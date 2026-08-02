# SPEC — MCP install via `terum-capture` (Tiers 1 + 2)

**Status:** design settled, ready to build.
**Implementer:** Fable (single-repo, additive, low ambiguity — do not re-derive the design).
**Repo:** `terum-capture` (this repo) only. No MVP-backend change is required.
**Branch note:** repo is currently on `fix/callback-pna-header`. Branch this work off `main`
(or off whatever is current once that PR lands) — do not build on top of the PNA fix branch
unless it has merged.

---

## 0. One-paragraph summary

`terum-capture setup` already mints a `trm_` API key, has it in plaintext in hand for one
moment, and edits Claude Code's config files. Terum's remote MCP server authenticates with
**the same `trm_` key type**. So connecting Claude Code (or Cursor) to Terum's MCP server is
almost free: reuse the key we already have and write one more config entry. This spec adds
(1) an opt-in Y/n prompt at the end of `setup` that wires Claude Code's MCP, and (2) a
standalone `terum-capture mcp install [--client claude|cursor]` for users who ran setup
earlier. Both call one shared helper, `_configure_mcp(...)`.

---

## 1. Background facts (verified from source — treat as ground truth)

- **The key is in hand at `cmd_setup`.** In `src/terum_capture/commands.py`, `cmd_setup`
  does `data = resp.json(); api_key = data["key"]` (line ~86–87) — the response of
  `POST {api_url}/keys`. This plaintext `trm_` string is the **only** moment the key is
  retrievable; the backend stores only a SHA-256 hash. The MCP config needs exactly this
  string.
- **MCP auth = the same `trm_` Bearer key as capture** (MVP `docs/mcp.md` §1). No new key
  kind, no new mint flow.
- **`setup` already edits Claude Code config.** `_configure_hook()` read-modify-writes
  `~/.claude/settings.json`; `_append_claude_md()` appends to `~/.claude/CLAUDE.md`. Adding
  an MCP server entry is the same class of operation and must copy the same
  idempotent, non-clobbering, warn-don't-crash discipline.
- **The MCP endpoint is `{api_url}/mcp`.** `DEFAULT_API_URL = "https://api.terum.ai/api"`
  already carries the `/api` suffix, so the full URL is `https://api.terum.ai/api/mcp`
  (streamable HTTP, no SSE). Do **not** append `/api` again.
- **Config stores `{api_key, api_url}`** at `~/.terum/config.json` (`config.py`,
  `load_config()` / `save_config()`, chmod 600). Tier 2 reads the key from here.
- **Tier 2 cannot mint a key.** It only holds the stored `trm_` key. `POST /api/keys`
  **rejects a `trm_` caller with 403** — only a Supabase JWT mints. This is load-bearing for
  the key-naming decision in §5.

---

## 2. ✅ VERIFY-FIRST — RESOLVED (probed on this machine, 2026-07-19, claude v2.1.215)

This was the highest-uncertainty part of the design. It has now been **empirically resolved**
by probing `claude mcp add` directly (added a throwaway user-scope server, inspected the
written config, removed it). Ground truth — build to these exact facts:

- **Config file (user scope): `~/.claude.json`.** The entry lands at the **top-level**
  `mcpServers` key (`d["mcpServers"]["terum"]`), NOT under `projects[cwd]` and NOT in a
  separate `.mcp.json`.
- **Exact entry shape** (this is what the direct-write fallback must reproduce verbatim):
  ```json
  { "type": "http", "url": "<mcp_url>", "headers": { "Authorization": "Bearer <key>" } }
  ```
- **`claude` IS on PATH** at `~/.local/bin/claude` — the same dir pipx installs to — so the
  shell-out primary path normally works. The fallback is still kept for machines where it
  isn't.
- **⚠️ Scope flag is mandatory.** `claude mcp add`'s default scope is **`local`** (only the
  current working directory's project). For a global install available in every project the
  command **MUST pass `--scope user`**:
  ```bash
  claude mcp add --transport http terum <mcp_url> --header "Authorization: Bearer <key>" --scope user
  ```
  Omitting `--scope user` (as the first draft of this spec did) would silently install into
  whatever directory `setup` happened to run in — a real bug. Always pass `--scope user`.
- Flags confirmed: `-t/--transport http`, `-H/--header`, `-s/--scope {local,user,project}`.
- `claude mcp remove terum --scope user` is the inverse (for tests / manual cleanup).

If a future maintainer sees different behavior on a newer Claude Code, re-probe and follow the
observed behavior — but as of v2.1.215 the above is exact.

---

## 3. The shared helper — `_configure_mcp(...)`

Add to `src/terum_capture/commands.py`. Signature:

```python
def _configure_mcp(api_key: str, api_url: str, client: str = "claude") -> str:
    """Wire an MCP server pointing at {api_url}/mcp, authed with api_key.
    Returns one of: "installed" | "already" | "failed".
    Never raises — mirrors _configure_hook's warn-don't-crash contract."""
```

Constants to add near the existing `CLAUDE_SETTINGS` / `CLAUDE_MD`:

```python
CLAUDE_JSON = Path.home() / ".claude.json"        # confirmed §2 — user-scope MCP store (top-level "mcpServers")
CURSOR_MCP = Path.home() / ".cursor" / "mcp.json"
MCP_SERVER_NAME = "terum"
```

### 3.1 Behavior by client

**`client == "claude"`:**
1. `mcp_url = f"{api_url}/mcp"`.
2. **Idempotency check first.** Read the target config (see §2). If a server named `terum`
   already exists, return `"already"` — do not overwrite (the existing entry may hold a
   different/older key the user set deliberately).
3. **Primary path:** if `shutil.which("claude")` is truthy, run (note **`--scope user`** —
   mandatory per §2, else it installs to the cwd's local project only):
   ```python
   subprocess.run(
       ["claude", "mcp", "add", "--transport", "http", MCP_SERVER_NAME, mcp_url,
        "--header", f"Authorization: Bearer {api_key}", "--scope", "user"],
       capture_output=True, text=True, timeout=15,
   )
   ```
   On returncode 0 → return `"installed"`. On non-zero (or `FileNotFoundError` /
   `TimeoutExpired`) → fall through to the fallback.
4. **Fallback (direct JSON write):** idempotent read-modify-write of `~/.claude.json`
   (`CLAUDE_JSON`, confirmed §2). Model exactly on `_configure_hook`:
   - `mkdir(parents=True, exist_ok=True)` on the parent (home always exists, but harmless).
   - Load existing JSON if the file exists (`{}` otherwise). **Never** truncate or replace
     the file wholesale — merge into the existing dict. (`~/.claude.json` is large and holds
     all of Claude Code's state; clobbering it would wipe the user's whole config.)
   - `mcp_servers = config.setdefault("mcpServers", {})`. If `MCP_SERVER_NAME` already
     present, return `"already"`.
   - Write the entry in the **exact shape confirmed in §2**:
     `{"type": "http", "url": mcp_url, "headers": {"Authorization": f"Bearer {api_key}"}}`.
   - `write_text(json.dumps(config, indent=2) + "\n")`.
   - Return `"installed"`.
   - Any exception → `print("Warning: Could not configure MCP: ...")`; return `"failed"`.

**`client == "cursor"`:** no `claude mcp add` equivalent — **direct write only**, to
`~/.cursor/mcp.json`, in the format from MVP `docs/mcp.md` §3:
```json
{ "mcpServers": { "terum": { "url": "<api_url>/mcp",
  "headers": { "Authorization": "Bearer trm_..." } } } }
```
Same idempotent read-modify-write + no-clobber + `"already"` short-circuit + warn-on-failure
rules as the Claude fallback. (Cursor's entry has no `"type": "http"` — it infers HTTP from
`url`. Match the doc exactly.)

Unknown `client` → `print` a clear error and return `"failed"`.

### 3.2 The credential is a secret — keep it off argv where avoidable

The primary path passes `Bearer {api_key}` as a subprocess argument, which is visible in the
local process table briefly. This is acceptable (same machine, same user, transient) and is
the documented `claude mcp add` interface — do not invent an alternative. But: **never log the
full key**. All success/error messages print only the `api_key[:8]` prefix, matching
`cmd_setup`'s existing `prefix = api_key[:8]` convention. The written config files are the
key's resting place; ensure the Cursor file, if newly created, is written with the same care
(`~/.terum/config.json` is chmod 600 — `~/.cursor/mcp.json` and `~/.claude.json` are owned by
their tools, so do not chmod them, just don't create them world-readable if you must create
the parent).

---

## 4. Tier 1 — the opt-in prompt in `cmd_setup`

Wire it in at the **end of `cmd_setup`, after `_append_claude_md()`** (line ~106), using the
in-hand `api_key`. Claude Code only (setup is the Claude Code installer; Cursor is Tier 2).

```python
_configure_hook()
_append_claude_md()

_maybe_configure_mcp_interactive(api_key, api_url)   # new

prefix = api_key[:8]
print(f"\nTerum connected! Key: {prefix}...")
...
```

`_maybe_configure_mcp_interactive(api_key, api_url)`:
- **Guard non-interactive runs.** If `not sys.stdin.isatty()` (headless `--token`/CI install),
  **skip silently unless** an explicit opt-in flag was passed (see §7 `--mcp`). Opt-in must
  never happen without a human saying yes — a silent MCP wire on a headless box would violate
  the settled "ask, don't bundle" decision (§8).
- Prompt (default yes, matching the handoff's `[Y/n]`):
  ```
  Also connect Claude Code to your team's shared decisions & conflict-checks (read-only)? [Y/n]
  ```
  Empty input → treat as yes. `n`/`no` → skip and say nothing further.
- On yes → call `_configure_mcp(api_key, api_url, client="claude")` and print per result:
  - `"installed"` → `"MCP connected — your agent can now pull team decisions & run conflict checks."`
  - `"already"` → `"MCP already configured — left it as-is."`
  - `"failed"` → `"Could not auto-configure MCP. Run 'terum-capture mcp install' later, or see <docs URL>."`

The trailing "Terum connected!" block prints regardless — capture setup succeeded even if the
user declined MCP.

---

## 5. Key-naming decision — REUSE the single key (locked)

**v1 reuses the one already-minted `{hostname}` key for both capture and MCP. Do not mint a
second key.**

Why this is the right call (not just the simplest):
- **Tier 2 structurally cannot mint** — it holds only the stored `trm_` key, and
  `POST /api/keys` 403s a `trm_` caller (§1). If Tier 1 minted a `{hostname}-mcp` key but
  Tier 2 could not, the two entry points would behave differently for the same feature.
  Reuse keeps them uniform.
- No MVP-backend change, no extra call against the 10-key cap (`POST /keys` returns 409 at 10).
- The `trm_` key is already the same scope for both directions in v1.

**Tradeoff, stated plainly:** revoking the key to kill MCP-read also kills capture-write, and
vice versa — they are not independently revocable in v1. That is acceptable because true
scope separation is the job of **Tier 4 (OAuth, §9)**, which separates MCP-read from
capture-write cleanly. A `{hostname}-mcp` second key would be a half-measure that only works
in Tier 1. Do **not** build it.

(If a future maintainer wants independent revocation before OAuth lands, the minimal move is a
second `POST /api/keys` with `name=f"{hostname}-mcp"` **inside Tier 1 only**, where the JWT is
still in hand — explicitly out of scope here.)

---

## 6. Tier 2 — `terum-capture mcp install [--client claude|cursor]`

For users who ran `setup` earlier and never got the prompt (or want to add Cursor).

- Loads config via `load_config()`. If absent or no `api_key` → print
  `"Not configured. Run: terum-capture setup"` and `sys.exit(1)` (match `cmd_status`).
- `--client` defaults to `claude`; also accepts `cursor`. Unknown value → error + exit 1.
- Calls `_configure_mcp(config["api_key"], config["api_url"], client=<client>)` and prints
  per the same result mapping as §4 (adjusting "Claude Code"/"Cursor" wording).
- **No prompt here** — running `mcp install` *is* the consent. Non-interactive-safe by design.

Add a `cmd_mcp_install(client: str)` function in `commands.py` (thin — resolves config, calls
the helper, prints). Keep route-handler-style thinness.

---

## 7. CLI wiring (`src/terum_capture/cli.py`)

Current dispatch is flat (`command = args[0]`). Add a two-token `mcp install` subcommand and
an optional `--mcp` / `--no-mcp` flag on `setup`.

- **`setup`:** extend the existing arg loop to also recognize `--mcp` (force the MCP prompt/
  install even when non-interactive — headless opt-in) and `--no-mcp` (skip MCP entirely).
  Pass a tri-state (`None` = ask if TTY, `True` = force yes, `False` = force skip) into
  `cmd_setup`, which forwards it to `_maybe_configure_mcp_interactive`. Default `None`
  preserves today's interactive behavior.
- **`mcp`:** new branch:
  ```python
  elif command == "mcp":
      if len(args) >= 2 and args[1] == "install":
          from terum_capture.commands import cmd_mcp_install
          client = "claude"
          i = 2
          while i < len(args):
              if args[i] == "--client" and i + 1 < len(args):
                  client = args[i + 1]; i += 2
              else:
                  i += 1
          cmd_mcp_install(client)
      else:
          print("Usage: terum-capture mcp install [--client claude|cursor]")
          sys.exit(1)
  ```
- Update **both** usage strings (the no-args banner and the unknown-command fallback) to list
  `mcp` — e.g. `"Commands: upload, setup, status, logout, mcp"`.

---

## 8. Settled design decisions — DO NOT relitigate

1. **Opt-in prompt, never silent bundle.** Capture *writes up* (your sessions leave your
   machine); MCP *reads down* (teammates' decisions enter your agent's context) — different
   trust directions, different consent. Ryan wants the user **asked**. Tier 1 is a Y/n prompt;
   headless installs skip MCP unless `--mcp` is explicit.
2. **Reuse one key** (§5) — locked.
3. **Shell-out primary, direct-write fallback** — both kept regardless of what §2 finds.

---

## 9. Tier 4 (OUT OF SCOPE — note only, must not be precluded)

The forward-compatible end-state is **MCP OAuth**: `claude mcp add ... <url>` with **no
`--header`** → browser login → short-lived scoped token, retiring the static key and cleanly
separating MCP-read scope from capture-write. MVP `docs/mcp.md` currently defers OAuth. This
design is forward-compatible: when OAuth lands, `_configure_mcp` simply stops passing
`--header` (and the fallback drops the `headers` block). **Do not build it now**; just don't
design anything that blocks it (e.g., don't hard-assume a header is always present in the
idempotency check — key on the server *name* `terum`, not on the header).

---

## 10. Error handling matrix (consolidated)

| Situation | Behavior |
|---|---|
| `claude` not on PATH | Silent fall-through to direct-JSON-write fallback. |
| `claude mcp add` non-zero exit / timeout | Fall through to fallback. If fallback also fails → `"failed"` + Warning. |
| `terum` server already configured | Return `"already"`, print "left it as-is", touch nothing. |
| Config file unreadable / bad JSON | Warn, return `"failed"`. Never crash setup. Never overwrite a file you couldn't parse. |
| Non-TTY setup, no `--mcp` | Skip MCP silently (capture still succeeds). |
| Tier 2, no local config | `"Not configured. Run: terum-capture setup"`, exit 1. |
| Unknown `--client` | Error + exit 1. |

All MCP failures are **non-fatal to capture setup** — mirror `_configure_hook`'s
`print("Warning: ...")` and continue.

---

## 11. Tests (add to `tests/`, model on `test_config.py`)

Unit tests, no network, filesystem via `tmp_path` + monkeypatching `Path.home()` (or the
module-level path constants). Cover:

1. `_configure_mcp` **primary path** — `shutil.which` returns a path, `subprocess.run`
   mocked to returncode 0 → returns `"installed"`, invoked with the expected argv (assert the
   `--header` contains the key and the URL is `{api_url}/mcp`).
2. `_configure_mcp` **fallback** — `shutil.which` returns `None` → writes the expected
   `mcpServers.terum` entry to the target file; **existing unrelated `mcpServers` entries and
   other top-level keys are preserved** (no-clobber).
3. **Idempotency** — second call with `terum` already present returns `"already"` and does not
   rewrite/duplicate.
4. **Cursor** — `client="cursor"` writes `~/.cursor/mcp.json` in the documented `url`+`headers`
   shape; merges into an existing file without clobbering.
5. **Bad-JSON target** — pre-seed the config file with garbage → returns `"failed"`, does not
   overwrite the file.
6. Tier 2 **no-config** path exits 1 with the right message.
7. Tier 1 **non-TTY skip** — `sys.stdin.isatty()` monkeypatched False, no `--mcp` → helper not
   called.

---

## 12. Docs to update in the same PR

- **`README.md`** (this repo): add a `terum-capture mcp install` row to the Commands table and
  a short "Connect your agent to team knowledge (MCP)" note; mention the setup-time prompt and
  the `--mcp`/`--no-mcp` flags.
- **MVP `docs/mcp.md`** is the source of truth for the endpoint/command and does **not** need
  changing for this work (it already documents the manual `claude mcp add`). Optionally add one
  line noting `terum-capture mcp install` as the CLI shortcut — nice-to-have, not required, and
  it lives in the other repo so skip it unless trivially convenient.

---

## 13. Definition of done

- `terum-capture setup` on a TTY prompts for MCP after capture is wired; yes → Claude Code MCP
  `terum` server present and `claude mcp list` shows it connected (or the fallback wrote the
  user-scope config).
- `terum-capture mcp install` and `terum-capture mcp install --client cursor` both work from a
  previously-configured machine with no re-auth.
- Re-running either is idempotent (`"already"`, no duplicate/clobber).
- Headless `setup --token ...` does **not** wire MCP unless `--mcp` is passed.
- All new tests green; existing suite unaffected (`pytest`).
- README updated.
```

