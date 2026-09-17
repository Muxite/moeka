# Upstream sync handover — 2026-09-17

Written for a fresh agent picking this up cold. Assumes competence, assumes
zero context on this branch. Read this before touching anything.

## 0. Constraints that still apply

- Work only in `/home/muk/projects/moeka-sync` (worktree). `/home/muk/projects/moeka`
  is the live checkout `moeka.service` runs from — stays on `main`, untouched.
- Never push. Land everything as commits on this branch; the owner opens the PR.
- **`~/.nanobot` and `~/.nanobot-sessions` are strictly read-only.** Do not move,
  delete, create, or modify anything there, not even cleanup — see §7, the
  incident this whole session-store detour exists because of.
- Never `ruff format` (destroys blame). `ruff check` only.
- `scripts/test-docker.sh` is the *only* number that counts for pass/fail — see
  §4 for why the local `.venv` lies by omission (missing extras → silent skips).

## 1. Exact state

- Branch: `merge/upstream-main-2026-08-05`
- HEAD: `5f4c6872d09587799444557a95ddc38cd408c916`
- Base: this branch was cut from `origin/nightly` / `main` tip `2b19da40` (see
  `.agent/upstream-sync-plan.md` for the original 7-batch plan).
- Batches 1 and 2 are merged and closed out (see the corresponding sections
  lower in `.agent/upstream-sync-notes.md`, commits `a833b464` and
  `679cc4e8`/`277e367b`). Batch 2's checkpoint is
  `0c684c5a99ff9cfa6f33b1a6172b5a189741f249` (2026-08-12).
- `71150b04` — read-only plugin-seam assessment (`.agent/plugin-seam-assessment.md`),
  no code change. This is where the owner decided to convert the session
  store to upstream's `SessionStore` Protocol before continuing batches.

Commits since `71150b04` (all mine, this session):

- **`3cb4f03c`** — the actual conversion. Adds `nanobot/session/sqlite_store.py`
  (new file, `SqliteSessionStore` implementing upstream's `SessionStore`
  Protocol) and resets `nanobot/session/manager.py` to be byte-identical to
  the batch-2 checkpoint (`git diff 0c684c5a... -- nanobot/session/manager.py`
  is empty — still true right now). Adopts ADR-0001 (sessions live outside the
  workspace by default, at a workspace-relative sibling directory — see
  `default_sessions_root()` in `sqlite_store.py`). Fixes the three `_conn()`
  reach-ins (`session_list_index.py`, `memory.py`, the CLI restore command)
  via a `get_store()` accessor. Updates every call site
  (`AgentLoop.from_config`/`__init__`, `gateway_runtime._run_gateway`, the
  `serve` command, `sessions restore-workspace`) and the test suite to inject
  `SqliteSessionStore` explicitly with a consistent `sessions_root`. Also
  fixes `Dockerfile.test`'s CMD (`pytest tests/` → `pytest tests/
  nanobot/channels/`, matching `pyproject.toml`'s `testpaths`) and adds the
  first structural test-isolation layer (root `conftest.py`'s `$HOME`
  redirect + mkdir guard; `tests/conftest.py`'s `_guard_live_workspace`
  extended to wrap `JsonlSessionStore`/`SqliteSessionStore.__init__`
  directly). **This commit is undocumented in `upstream-sync-notes.md`** —
  the incident (§7) interrupted before that write-up happened. Fixing that
  now, in this same pass.
- **`b013eba7`** — `Dockerfile.test` never `COPY`'d the root `conftest.py`, so
  the isolation layer added in `3cb4f03c` was silently inactive on every
  Docker run. Fixed the `COPY`. This had inflated an apparent 45-test
  regression down to the real number: 7 (see §3). Also lands the fix for
  those 7 (explicit `tmp_path` workspace overrides in
  `tests/cli/test_commands.py`, matching the one sibling test that already
  did this).
- **`5f4c6872`** — the migrate command. `SqliteSessionStore.__init__` no
  longer auto-migrates a legacy in-workspace `sessions.db`; it only warns
  (`_warn_if_legacy_db_in_workspace`, replacing
  `_migrate_legacy_db_from_workspace`). New `nanobot sessions migrate --from
  --to [--no-dry-run] [--yes] [--force]`: dry-run unless *both* `--no-dry-run`
  and `--yes` are given (two-key arming — no single flag can trigger a real
  move); refuses to overwrite an existing destination; refuses a source that
  fails a `BEGIN IMMEDIATE`/`busy_timeout=0` probe (best-effort "is another
  process writing to this right now" check) unless `--force`; moves the
  `sessions.db`/`-wal`/`-shm` triplet, then runs `PRAGMA integrity_check` on
  the destination before declaring success, rolling the files back to source
  if that check fails. New primitives in `sqlite_store.py`:
  `plan_session_database_migration`, `migrate_session_database`,
  `SessionDbMigrationError`. Tests: `tests/session/test_session_db_migrate.py`
  (10 tests, all passing).

Neither `b013eba7` nor `5f4c6872` were recorded in `upstream-sync-notes.md`
before this handover — that gap is being closed in the same commit as this
file (§8 below has the exact edit).

## 2. The conversion, honestly

`git diff upstream/main -- nanobot/session/manager.py` currently reports
**−479/+214** — do not read that as "the conversion failed." It is being
diffed against the *live, constantly-advancing* `upstream/main` tip
(currently `835cac0a`, 2026-09-17), which is **744 − 236 = 508 commits ahead
of what this branch has actually merged** (batches 3–7 are not yet merged;
see §5). Of those, **14 commits between the batch-2 checkpoint and today's
`upstream/main` touch `manager.py` directly** — that's where the −479/+214
comes from, and it is exactly the "27 upstream commits, highest-churn memory
file" risk called out in the original plan (`.agent/upstream-sync-plan.md`).

**The correct, currently-true success criterion** is against the checkpoint
this branch has actually merged up to, not the tip:

```
git diff 0c684c5a99ff9cfa6f33b1a6172b5a189741f249 -- nanobot/session/manager.py
# → empty, right now
```

That command returning empty is what "the conversion is done" means at this
point in the batch sequence. It will go non-empty again the moment batch 3
merges in upstream's next `manager.py`-touching commits — that's expected
and correct, not a regression. **Each future batch's `manager.py` conflicts
should be resolved by taking upstream's side wholesale.** There is no
forked moeka logic left in that file to preserve — see below.

**What moved where:**

- `nanobot/session/manager.py` (1699 lines) is **100% upstream, byte-for-byte,
  at the checkpoint**. It still contains upstream's own `JsonlSessionStore`
  class (lines 546–1452, ~906 lines) — this is *not* dead code moeka forgot
  to delete; `SessionManager.__init__` unconditionally constructs one
  internally (see the flag in §6 — this is a real sharp edge, not a
  cosmetic leftover). `SessionManager` itself (line 1453 onward — caching,
  `SessionPolicy`, retention/continuity, `get_or_create_transient`,
  `fork_session_before_user_index`) is the wrapper both backends sit behind.
  None of it is moeka-specific; none of it needs converting further.
- `nanobot/session/sqlite_store.py` (870 lines, new) holds everything
  backend-specific for SQLite: the six `SessionStore` Protocol methods
  (`load`/`save`/`delete`/`read`/`read_metadata`/`list_sessions`), schema
  management, the legacy-jsonl one-time importer, ADR-0001's
  workspace-identity/namespace-claim logic (reused from
  `JsonlSessionStore`'s private classmethods — see the module docstring for
  why that coupling is intentional and what to check if upstream renames
  them), `dump_jsonl`/`export_to_workspace` (debug/downgrade export), the
  `get_store()` accessor, and the new migration primitives from `5f4c6872`.

**So: is anything left in `manager.py` that's forked-and-needs-converting?
No.** The conversion (as scoped: "moeka's identity is the SQLite backend,
not the storage location") is complete for what's merged. The remaining work
is *maintaining* that as batches 3–7 land, not finishing something half-done.

## 3. Step 2/4 — the 9+45 failures, enumerated and classified

The coordinator asked for this twice and it never arrived; here it is in
full.

**Original report: 54 Docker failures** (7 already-known + 43 new +
2 already-known + 2 already-known — see breakdown below). Investigating
the 43 found they were **not real**: `Dockerfile.test` never `COPY`'d the
root `conftest.py` (added in `3cb4f03c`), so the `$HOME` redirect, mkdir
guard, and `_isolate_sessions_root` fixture were silently inactive on every
Docker run. Fixed in `b013eba7`; re-running Docker made all 43 disappear.
They were a Docker/local environment mismatch (the websocket tests' own
`isolate_webui_workspace_state` fixture points `get_data_dir()` straight at
`tmp_path`; only `_isolate_sessions_root`'s more specific
`get_runtime_subdir` patch was preventing the resulting workspace/session-root
collision — present locally, absent in Docker pre-fix), not a defect in the
conversion.

**The real, remaining 7** — all in `tests/cli/test_commands.py`, all
**classification (b)**, zero are (a):

| Test | Command under test | Production site |
|---|---|---|
| `test_gateway_health_endpoint_binds_and_serves_expected_responses[127.0.0.1-...]` | `gateway` | `nanobot/cli/gateway_runtime.py:393` |
| `test_gateway_health_endpoint_binds_and_serves_expected_responses[0.0.0.0-...]` | `gateway` | `nanobot/cli/gateway_runtime.py:393` |
| `test_gateway_agent_task_owns_initial_mcp_provider_close` | `gateway` | `nanobot/cli/gateway_runtime.py:393` |
| `test_gateway_shutdown_event_exits_forever_runtime_tasks` | `gateway` | `nanobot/cli/gateway_runtime.py:393` |
| `test_serve_cli_options_override_api_config` | `serve` | `nanobot/cli/commands.py:360` |
| `test_serve_allows_loopback_without_api_key` | `serve` | `nanobot/cli/commands.py:360` |
| `test_serve_passes_configured_api_key` | `serve` | `nanobot/cli/commands.py:360` |

All 7: `config = Config()` with no `agents.defaults.workspace` override →
`workspace_path` defaults to real `~/.nanobot` → the production call site
constructs `SqliteSessionStore(...)` **eagerly, as a constructor argument,
before** the mockable `SessionManager` — bypassing the test suite's existing
`session_manager=` monkeypatch seam entirely. That's (b): a real production
gap, not a test-only mistake. Fixed for now via explicit `tmp_path`
workspace overrides in the 7 tests (matching the one sibling test,
`test_serve_uses_api_config_defaults_and_workspace_override`, that already
did this and passed) — **but the underlying production gap (three eager
construction sites, no shared seam) is not fixed.** That's the "step 4"
factory-consolidation work, not started. The three sites, for the record:

1. `nanobot/cli/commands.py:360` — inside `serve()`
2. `nanobot/cli/commands.py:468` — inside `sessions restore-workspace`
   (no test currently exercises this one directly, but same shape)
3. `nanobot/cli/gateway_runtime.py:393` — inside `_run_gateway()`

**Where `session storage must be outside the agent workspace` is enforced:
both files, independently.**
- `nanobot/session/manager.py:557-561`, inside `JsonlSessionStore.__init__`
  — upstream's own check, byte-identical, pre-existing. Not something moeka
  added.
- `nanobot/session/sqlite_store.py:161-165`, inside `SqliteSessionStore.__init__`
  — added by me in `3cb4f03c`, deliberately mirroring upstream's check so
  ADR-0001 holds regardless of which backend is active.

Not purely inherited, not purely ours — it's a duplicated invariant by
design (one per backend), which is itself worth a second look: if a future
refactor changes the wording/logic in one, the other will drift.

## 4. Current numbers (Docker only)

```
scripts/test-docker.sh
```
→ **6333 passed, 4 failed, 31 skipped**, ~172s.

The 4 failures are the same two pre-existing environment-artifact
categories documented since batch 1/2, unchanged:
- 2× `tests/tools/test_tool_validation.py::test_exec_guard_allows_public_urls`
  — no DNS/network egress in this sandbox.
- 2× `tests/webui/test_mcp_presets_api.py` (`test_test_mcp_preset_connects_and_reports_tools`,
  `test_test_mcp_preset_inspects_tools_outside_the_enabled_allowlist`) — no
  `npx`/playwright on PATH in the test image.

Both are pre-existing (present before this session's work), documented in
`upstream-sync-notes.md`'s batch-2 section, and not something to chase
further unless the test image itself changes.

## 5. What remains

**Step 4 (not started)** — consolidate the three eager `SqliteSessionStore(...)`
construction sites (§3) behind one factory seam that routes through
`get_state_home()`, so the test suite has one seam to mock instead of three
independent constructor calls. Also fix `nanobot/gateway/service.py`'s
`_working_directory()`/`_working_directory_text()` (lines 214-223): both
fall back to bare `Path.home()` when `options.workspace` isn't set, skipping
even `get_state_home()`'s `MOEKA_WORKSPACE`/`MOEKA_STATE`/`NANOBOT_HOME`
override chain — a second, independent "resolves under the real home"
pattern, distinct from the three `SqliteSessionStore` sites.

**Then**: re-confirm `git diff 0c684c5a... -- nanobot/session/manager.py`
is still empty (it should be untouched by step 4 — that work is entirely in
`commands.py`/`gateway_runtime.py`/`service.py`), run full
`scripts/test-docker.sh`, commit, then resume batches 3–7:

| # | Checkpoint (date, sha) | Commits | Cumulative |
|---|---|---|---|
| 3 | 2026-08-19 `1018bdb7fee35acd6a52e2409d15cec65d3d3c09` | 167 | 403 |
| 4 | 2026-08-26 `f9d449ef` | 92 | 495 |
| 5 | 2026-09-02 `d81aa5a4` | 76 | 571 |
| 6 | 2026-09-09 `20f115bf` | 87 | 658 |
| 7 | 2026-09-16 `2fb16593` | 86 | 744 |

(These SHAs are from the original survey in `upstream-sync-plan.md`, cut
2026-09-16 — worth a quick `git log --oneline <sha> -1` sanity check before
merging, since `upstream/main` has moved past all of them by now; the dates
still anchor which upstream commits belong to which batch.)

Per §2/§3, expect every batch from here to touch `manager.py` again — the
resolution rule is now simple and mechanical: **take upstream's side of
`manager.py` wholesale**, there is nothing moeka-specific left in it to
re-apply. Conflicts in `sqlite_store.py` (new file, won't conflict directly,
but upstream's `SessionStore` Protocol shape in `manager.py` could change in
a way `sqlite_store.py` needs to follow — e.g. a new Protocol method) are
the ones that need real judgment: check the Protocol definition
(`manager.py`'s `class SessionStore(Protocol)`, currently line 532) each
batch for additions.

No specific judgment call was mid-thought when this session ended — step 4
had not been started, just scoped.

## 6. Risk assessment — where I'd look first for a defect

In order of how likely I think a real bug is hiding there:

1. **`SessionManager.__init__` unconditionally building an internal, unused
   `JsonlSessionStore`** even when `store=SqliteSessionStore(...)` is
   injected (`manager.py:1464`: `self._jsonl_store =
   JsonlSessionStore(workspace, sessions_root=sessions_root)`, always, then
   `self._store = store if store is not None else self._jsonl_store`). This
   is upstream's own shape, not something I added, but it means **every**
   `SessionManager(...)` call site — including ones I haven't touched or
   audited — pays the JsonlSessionStore construction cost and its own
   ADR-0001 check (`root == canonical_workspace or
   root.is_relative_to(canonical_workspace)`) on every single instantiation,
   using whatever `sessions_root` was passed (or its own default if not). If
   a future call site passes a `sessions_root` that's consistent for the
   injected `SqliteSessionStore` but *not* explicitly passed to
   `SessionManager` itself, the internal `JsonlSessionStore` will compute
   its own default and may raise where the SQLite store wouldn't have, or
   vice versa. I chased exactly this class of bug twice already (see
   "Problem Solving" history) and I would not be surprised if one more call
   site somewhere still has this asymmetry. Grep for `SessionManager(` and
   check every call passes the same `sessions_root` to both.
2. **The duplicated ADR-0001 invariant** (§3, last paragraph) — two
   independent copies of the same check, in two files. A future edit to one
   (e.g. loosening it, or changing the error message parsed by a script)
   won't automatically apply to the other.
3. **`_import_legacy_jsonl`'s ordering comment** (`sqlite_store.py:308-311`)
   claims it "runs before `JsonlSessionStore`'s own migration claims the
   same files... so nothing is double imported," relying on construction
   order (`SessionManager` builds the internal `JsonlSessionStore` as a
   constructor *argument* before `store=` is used) — this is exactly the
   kind of implicit ordering dependency that's easy to break by refactoring
   `SessionManager.__init__`'s argument order or turning `store` into a
   lazily-constructed property. I did not write a test that would fail if
   this ordering broke; I'd want one before touching that constructor again.
4. **The 14 unmerged upstream commits touching `manager.py`** (§2) are
   unread by me — I know they exist and roughly the size of the diff, not
   their content. Given the plan's own flag that this window includes
   upstream splitting session concerns into new modules
   (`session_messages.py`, `summary.py`, `turn_continuation.py`,
   `webui_turns.py`), there's a real chance one of them changes the
   `SessionStore` Protocol shape itself (not just `SessionManager`
   internals), which `sqlite_store.py` would then need a matching update
   for — I have not previewed those commits to check.

## 7. The incident this detour came from (summary — full detail already in `upstream-sync-notes.md`)

2026-09-17: a test process constructed a session store against the real
`~/.nanobot`, which tripped the (then-unrestricted) automatic migration
logic against `moeka.service`'s live database. Caught mid-move, stopped
before forcing past a permission block, reported immediately. Owner
restored the data personally (verified intact: same inodes, integrity
check passed, 4 sessions / 1,738 messages). Root cause, full isolation-hole
audit, and the resulting design changes (this whole session: structural
test isolation, the explicit migrate command, the Dockerfile fix) are
written up under the incident heading already in `upstream-sync-notes.md` —
this handover doesn't repeat that, only the commits (§1) that came after it.

## 8. Notes-file sync

`upstream-sync-notes.md` is being updated in the same commit as this file
to add a short "batch 2.5 — session store conversion" section covering
`3cb4f03c`/`b013eba7`/`5f4c6872`, since none of the three were recorded
there before now. See that file for the pre-existing incident write-up this
handover builds on.
