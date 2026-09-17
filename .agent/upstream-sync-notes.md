# Upstream sync working notes — 2026-09-16

## Branch
`merge/upstream-main-2026-08-05`, created off `main` @ `2b19da40` (never
checked out/committed to `main` itself). Batch 1 target checkpoint:
`upstream/main` @ `67805f5db8dc8d3b1a61fbc0f29bb1a6d010b86e` (2026-08-05,
134 commits ahead of `main`).

## Survey findings (see plan for full detail)
- `merge/upstream-main-2026-07-28` and `-08-03` branches are stale/closed:
  fully merged into `main` already (0 unique commits vs `main`, both still
  744 behind `upstream/main` — same as `main`). Not "in-flight".
- `nightly` is stale (2026-05-17), 20 commits behind `main`, 1951 behind
  `upstream/main`, 43 ahead of `upstream/main` — bypassed by direct
  commits to `main`. Flagged for owner, not fixed here.
- `feat/sqlite-vec-memory` fully merged into `main`, irrelevant.
- `main..upstream/main`: 744 commits, 1165 files, +197725/-51116,
  2026-07-28→2026-09-16.
- `nanobot/core` (moeka's plugin/RAG core, MoekaCore, VecStore): 0
  upstream commits touch it in this window.
- `session/manager.py`: 27 upstream commits. `tools/shell.py`: 14 upstream
  commits including a deny-pattern-scoping branch — highest scrutiny.
  `tools/registry.py`: 5 upstream commits.

## Batch 1 log — complete

Merge commit `8ba7a3db` (+ post-merge fixup `0efcad88`), 21 conflicted files.

### Conflicts resolved
- `.github/workflows/ci.yml` — additive: kept moeka's NANOBOT_MIN_TESTS
  guard + upstream's BasedPyright step, both.
- `CONTRIBUTING.md` — kept deleted (moeka policy).
- `README.md` — kept moeka's (ours) entirely; branding identity, always-keep.
- `pyproject.toml` — additive merge of both sides' new deps
  (basedpyright, pytest-xdist, olostep python_version guard); removed one
  accidental duplicate `olostep` key introduced while merging.
- `nanobot/agent/context.py` — 3 hunks: import list (additive), memory
  section (kept moeka's vec-based `get_memory_context`, later fixed a
  double-`read_memory()` call — see fixup below), `_load_bootstrap_files`
  docstring (kept moeka's `bootstrap_overrides` note + upstream's
  `list[str]` annotation).
- `nanobot/agent/loop.py` — `schedule_background`: kept upstream's `set`
  + `.discard()` container, re-attached moeka's `log_task_exceptions`
  done-callback on top (both wanted).
- `nanobot/agent/memory.py` — additive TYPE_CHECKING imports; kept moeka's
  SQLite-based `prune_dream_sessions` (queries `sessions` table) over
  upstream's jsonl-glob version.
- `nanobot/agent/runner.py` — kept moeka's `is_blank_text`/config-driven
  `max_length_recoveries` guard verbatim — **this was wrong**, see fixup.
- `nanobot/agent/tools/loader.py` — kept moeka's allow/deny scope params
  on `ToolLoader.load` (plugin abstraction), tightened `ctx` type to
  `ToolContext` per upstream.
- `nanobot/agent/tools/shell.py` — trivial signature merges only; no
  actual deny-pattern-scoping changes landed in this batch (that upstream
  branch hasn't merged yet). Verified allow_sudo/deny lists unaffected.
- `nanobot/channels/manager.py` — kept moeka's dispatcher watchdog
  counter, adopted upstream's tighter `asyncio.Task[None]` typing and
  `cast`-based allow_from parsing.
- `nanobot/cli/commands.py` — upstream refactored the ~630-line inline
  `_run_gateway` out into `nanobot/cli/gateway_runtime.py` (+`gateway.py`,
  `runtime_config.py`, `webui_support.py`). Deleted moeka's now-duplicate
  inline copy; ported moeka-only logic missing from the extracted file
  into `gateway_runtime.py` (see below).
- `nanobot/cli/gateway_runtime.py` (new upstream file, not itself
  conflicted, but needed moeka logic ported in) — added: WAL-checkpoint
  session-store teardown (`agent.sessions.close()`) on shutdown, and the
  Dream-cycle `store.reindex_memory()` call + fixed `prune_dream_sessions`
  call signature (was passing a jsonl path, moeka's takes the manager).
- `nanobot/config/loader.py` — 3 hunks. Kept moeka's non-fatal
  missing-`${VAR}` warning behavior (deliberate runtime-safety default,
  documented in CLAUDE.md) instead of adopting upstream's new
  `ConfigLoadError(kind="missing_env")` hard-fail; adopted upstream's
  new `ConfigLoadError`/`ConfigIssue`/ `SettingsError` diagnostics
  machinery for the *other* error kinds (invalid_json, invalid_schema,
  invalid_root) since those are genuine improvements orthogonal to the
  missing-env leniency. Removed a stray module-level
  `_resolve_tool_config_refs` import that would have broken moeka's lazy
  circular-import-resilient rebuild pattern (gotcha #8).
- `nanobot/config/paths.py` — kept moeka's `_default_workspace()` (flat
  workspace layout), widened `get_workspace_path` param type per upstream.
- `nanobot/session/manager.py` — the big one. Upstream split
  `SessionManager` into a `SessionStore` Protocol + `JsonlSessionStore` +
  thin `SessionManager` wrapper (nothing else in the tree consumed the
  Protocol in this checkpoint). Kept moeka's single SQLite-backed
  `SessionManager` as-is (documented as a future integration opportunity
  in its own docstring) rather than reshaping it. Ported forward,
  independent of that split: `Session.provider_state` (new field,
  `ProviderConversationState` persistence for OpenAI-Responses-style
  continuation — wired through SQLite `save`/`_load` under a reserved
  `_provider_state` metadata key, stripped from `read_session_metadata`);
  `Session.clear()` and `retain_recent_legal_suffix()` now reset it too
  (fixup, see below). Also added `_PROVIDER_STATE_RECORD_TYPE`-equivalent
  constants were NOT needed (jsonl-only), but did add public `get_cached()`
  (fixup — `nanobot/sdk/clients.py` already called it, unconditionally).
  First attempt at this conflict was botched by a naive scripted "always
  keep ours" pass that missed that upstream's `JsonlSessionStore` body
  was inserted as *unconflicted* new content interleaved with the real
  conflict hunks; recovered with `git checkout --ours` on the whole file
  and re-applied the intended change by hand.
- `nanobot/webui/session_list_index.py` — same shape of problem as
  manager.py: upstream added a `.webui_session_index.json` scan-cache
  (147→430 lines) that moeka's SQLite store makes unnecessary (it already
  answers the same query in one statement). Dropped the cache, but kept
  its public contract (`WEBUI_SESSION_INDEX_INTERNAL_FIELDS`,
  `indexed_workspace_scope`, `_indexed_workspace_scope_fields`) since
  `nanobot/webui/ws_http.py` depends on it — reimplemented directly
  against `session_manager.read_session_metadata()` instead of a cached
  index row.
- `tests/agent/test_session_atomic.py`, `tests/webui/test_session_list_index.py` —
  additive imports; adapted/removed sub-tests that asserted jsonl-file-format
  internals (`_get_session_path`, raw `_type: provider_state` jsonl
  records, a file-read race with `open()`) that don't apply to the SQLite
  store, replacing with SQLite-equivalent assertions where the underlying
  behavior still mattered.
- `tests/cli/test_commands.py`, `tests/config/test_env_interpolation.py` —
  dropped/rewrote tests asserting the missing-`${VAR}` hard-fail behavior
  (see loader.py above); kept new tests unrelated to that (e.g.
  `TestResolveSingleEnvRefs`).
- `webui/bun.lock` — regenerated via `bun install` rather than hand-merged.

### Post-merge fixup (commit `0efcad88`)
`scripts/test-docker.sh` didn't even build after the merge — pre-existing
gaps in `Dockerfile.test` (never copied `hatch_build.py` or `scripts/`,
still copied the just-removed `bridge/` dir) plus a stale `uv.lock`
against the merge's `pyproject.toml` edits. Fixed the Dockerfile and ran
`uv lock`. Docker suite then surfaced 34 real failures beyond the
Dockerfile issue, mostly from the two "kept moeka, upstream added a new
capability on top" conflicts above:
- Missing `SessionManager.get_cached()` — added (see above).
- `retain_recent_legal_suffix()` not clearing stale `provider_state`.
- `AgentLoop._schedule_background` → renamed to public `schedule_background`
  by an unconflicted upstream edit; one moeka test still called the old
  private name — updated the test, not the (already-correct) production
  code.
- `get_memory_context()` called `read_memory()` a second, redundant time
  after my context.py resolution — refactored to accept an already-read
  `long_term` string (see above), restoring a call-count test.
- **Runner.py length-recovery regression**: my "keep ours" resolution on
  `nanobot/agent/runner.py` reverted an upstream bugfix (commit
  `511c764f`, "Fixes #5133") that intentionally removed an
  `is_blank_text(clean)` guard from the length-recovery branch, because
  that guard was itself the bug (a truncated response with blank content
  and a dropped tool call could never reach recovery). Restored upstream's
  fix.
- Several tests asserting the missing-`${VAR}` hard-fail behavior in
  other call paths (`load_provider_snapshot`, `load_model_preset_catalog`,
  `nanobot status` CLI, `Nanobot.from_config`) — same treatment as the
  loader-level test: rewritten to assert the non-fatal-warning behavior.
- `ModuleNotFoundError: No module named 'scripts'` — `Dockerfile.test`
  never copied `scripts/`, needed by
  `tests/channels/test_channel_plugins.py` (pre-existing gap, unrelated
  to merge content, just never hit before).
- `tests/session/test_session_store.py` (new upstream file) — deleted
  entirely; every test in it constructed `SessionManager(..., store=...)`
  against the discarded Protocol.
- `tests/utils/test_webui_workspaces.py` — one test used a mock
  `SessionStore`; rewrote it against a real SQLite `SessionManager`
  (arguably a better test — no mocking needed since the real store has
  no metadata cache to go stale).
- `tests/integration/test_config_real.py::test_raises_on_malformed_json` —
  unrelated to leniency (malformed JSON still hard-fails correctly, just
  with upstream's new nicer error message); fixed the stale regex match.

### Remaining Docker-only failures (3) — environment, not regressions
Verified these pass on the host and fail identically in **any** container
in this sandbox (even a bare `python:3.13-slim` doing
`socket.getaddrinfo('example.com', None)` fails — no DNS/network egress
at all here):
- `test_exec_guard_allows_public_urls[wget ...]` and `[python3 ...]` — the
  SSRF guard (`nanobot/security/network.py:resolve_url_target`) does a
  real `socket.getaddrinfo()` to prove a public hostname isn't
  private/internal; with no DNS it can't resolve, so it fails closed.
- `test_test_mcp_preset_connects_and_reports_tools` — needs the
  `npx`/playwright command on PATH; not installed in the test image, only
  the "connect" step is mocked.

### Deviation verification (post-merge, on the final commit)
- `allow_sudo: bool = False` — default unchanged (2 occurrences, both False).
- `rm -rf`/`dd`/`mkfs`/`shutdown` absent from `_DEFAULT_DENY_PATTERNS`;
  only the fork-bomb pattern remains there.
- `nanobot/session/manager.py` uses `sessions.db` (WAL) — confirmed.
- `ChannelManager._dispatch_with_watchdog` present and wired.
- `bg_shell.enabled()` still hardcoded `return False`.
- `nanobot channels enable/disable` CLI commands present in commands.py.
- Telegram `drop_pending_updates: bool = True` — confirmed.
- Lazy `Config`/`ToolsConfig` model_rebuild pattern (`_schema_refs_ready`)
  intact in `nanobot/config/loader.py`.
- No `CONTRIBUTING.md`, no `images/nanobot_logo.png`;
  `images/GitHub_README.png` present.
- `.agent/*`, `CLAUDE.md`, `bin/*` launchers untouched by this batch (no
  upstream commits touch them).

### New deviation surfaced by this batch (flagging per instructions)
`nanobot/session/manager.py`'s docstring now documents that moeka
deliberately did **not** adopt upstream's `SessionStore` Protocol /
`JsonlSessionStore` split introduced at this checkpoint. If a *future*
batch wants to make moeka's SQLite store implement that Protocol (a
genuinely good "plugin abstraction" fit), start there — the method
surface already matches 1:1 as of this checkpoint.

## Batch 2 — complete, STOPPED after this batch (owner review needed)

Merge commit `f7257d0e` (+ post-merge fixup `277e367b`), checkpoint
`0c684c5a99ff9cfa6f33b1a6172b5a189741f249` (2026-08-12, 102 commits).
Worked in worktree `/home/muk/projects/moeka-sync` on branch
`merge/upstream-main-2026-08-05`; `/home/muk/projects/moeka` stayed on
`main` throughout (verified after every step).

### Conflicts resolved (9 files)
- `README.md` — kept moeka's (ours), branding identity.
- `docker-compose.yml` — kept deleted (moeka runs natively via uv, no
  containers).
- `docs/configuration.md` — additive: moeka's semantic-memory/session-
  storage docs + upstream's new "Agent Plugins v1" docs section, both kept.
- `nanobot/agent/context.py` — gated moeka's semantic memory retrieval
  behind upstream's new `include_memory` flag (used by ephemeral/
  non-persisted sessions).
- `nanobot/agent/loop.py` — kept moeka's `MyTool` allow/deny scoping,
  folded in upstream's comment simplification.
- `nanobot/agent/skills.py` (3 hunks) — integrated upstream's new **Agent
  Plugins v1** skill discovery (`nanobot/agent/plugins.py`,
  `enabled_agent_plugin_skills()`) alongside moeka's inline-skills support;
  delegated frontmatter parsing to upstream's extracted
  `parse_skill_metadata()` helper (kept moeka's inline-skill shortcut on
  top). Agent Plugins v1 (installable `plugin.json` packages under
  `<workspace>/plugins/`, with `mcp.json`/`skills/` and progressive
  loading) is itself worth a closer look in a later batch — it's a real
  plugin-abstraction feature, currently just wired through unchanged.
- `nanobot/channels/websocket/tests/test_websocket_http_routes.py`
  (6 hunks) — mixed resolution: kept moeka's REST-GET-with-bearer-token
  test style where it covers something upstream's `_webui_mutate` RPC
  helper doesn't (extra `/messages` 404-boundary and encoded-key checks);
  took upstream's `_webui_mutate` style elsewhere. Both test helpers
  exercise the same underlying `_handle_session_delete` route.
- `nanobot/nanobot.py` — additive: upstream's `ToolRegistry`/`MCPProvider`
  wiring alongside moeka's `defaults` var (used for `vec_config`).
- `nanobot/session/manager.py` — again the big one (see below).
- `webui/bun.lock` — regenerated via `bun install`.

### `nanobot/session/manager.py` — the recurring architectural conflict

Upstream (2026-08-12) kept building out the `SessionStore` Protocol /
`JsonlSessionStore` split from batch 1 (1174→1699 lines). Reconfirmed the
batch-1 decision: kept moeka's single SQLite-backed `SessionManager`, did
**not** reshape it into the Protocol. But — unlike batch 1, where the only
things riding along were self-contained (`provider_state`) — this
checkpoint's split carries genuinely new, **backend-independent** features
that other already-merged, unconflicted code now calls unconditionally.
Ported all of them onto moeka's SQLite implementation:
- `Session.policy: SessionPolicy` (`persist`, `log_content`,
  `disabled_tools`) — brand new dataclass, didn't exist in moeka at all.
  `nanobot/agent/loop.py` and the new `nanobot/webui/temporary_chats.py`
  (WebUI "Temporary Chat" ephemeral sessions) reference
  `session.policy.*` unconditionally; without this the merge wouldn't
  even import.
- `SessionManager.get_or_create_transient()` — used by Temporary Chats to
  get a fresh, never-persisted `Session` without touching the DB.
  `SessionManager.save()` now no-ops when `policy.persist` is False.
- `Session.get_history()` — ported upstream's compaction-boundary
  continuity fix verbatim (keep a small raw suffix near
  `last_consolidated` so replay doesn't jump straight from a summary to
  mid-turn content); added the `MIN_COMPACTED_REPLAY_MESSAGES` constant
  it and `nanobot/agent/memory.py` both need.
- `SessionManager.get_cached()` — public alias moeka was already missing
  (should have been added in batch 1; `nanobot/sdk/clients.py` calls it
  unconditionally — this is why 6 SDK-facing tests only started failing
  once other batch-2 code paths exercised it more).
- `retain_recent_legal_suffix()` — **found via test failure, not via the
  conflict itself** (this region wasn't marked conflicting — "ours" was
  silently kept and turned out to already be missing a fix): upstream
  commits `60282d15`/`8dfce4c1` fix a real bug where a proactive
  `_channel_delivery` message (e.g. a heartbeat-triggered reminder) gets
  dropped during hard-cap or extend-to-user trimming because it isn't a
  `user`-role message itself. Ported the two-line fix verbatim; see the
  post-merge fixup commit.
- `restore_sessions_to_workspace()` / `SessionRestoreResult` — new
  `nanobot sessions restore-workspace` CLI command (downgrade helper)
  calls this unconditionally. Implemented a moeka-appropriate version
  (jsonl-exports every session via the existing `dump_jsonl()`) since
  moeka has no jsonl-store rollback concept to replicate.
- `get_runtime_subdir` re-imported (unused internally, `# noqa: F401`) —
  `tests/conftest.py`'s autouse `_isolate_sessions_root` fixture patches
  this module attribute unconditionally for ADR-0001 test isolation; the
  name has to resolve even though moeka's default session location
  doesn't use it.
- Added an **opt-in** `sessions_root` constructor param (SQLite db file
  location only — defaults to the pre-existing in-workspace path, so the
  live service's on-disk layout is unchanged) and, separately, deleted
  `tests/session/test_session_location.py` (14 new tests) wholesale — see
  next section for why.

### ⚠️ Needs human review — ADR-0001 NOT adopted (this is why I stopped)

Upstream introduced **ADR-0001**: sessions are stored **outside the agent
workspace by default** (a per-workspace `.nanobot/workspace-id` identity
marker inside the workspace maps to session storage under the instance
data root), specifically so a workspace-scoped shell/file tool can never
read or tamper with its own conversation history. `tests/conftest.py`'s
own new autouse fixture assumes this is universal ("Session storage lives
under the active runtime data root... per ADR-0001, so without
redirection tests would write into the real home").

I did **not** adopt the default-relocation behavior. What's in this merge
is opt-in only (`sessions_root=` param, unused unless a caller passes it
explicitly) — moeka's live gateway path (`nanobot/cli/gateway_runtime.py`)
still constructs `SessionManager(config.workspace_path)` with no
`sessions_root`, so **the live service's session storage location is
unchanged** (`<workspace>/sessions.db`, same as before this whole sync
effort started).

This is a deliberate, conservative choice, not an oversight — but it
means moeka does **not** get this security improvement automatically, and
I think that's a real gap worth closing: moeka's shell sandbox is
deliberately permissive (`rm -rf`, `dd`, etc. are not blocked by default),
which makes "the agent's own tools can read/delete its own session
history" a *more* serious concern for moeka than for stock nanobot's more
locked-down default posture. Adopting ADR-0001 for real would mean:
- Relocating the live service's `sessions.db` out of `~/.nanobot` (moeka's
  flat workspace==state-home layout makes "outside the workspace" a
  bigger structural change than it is for upstream's nested layout).
  Every existing moeka install would need a migration path.
  - This is explicitly a "changes runtime behavior of the live service"
  judgment call per the coordinator's stop criteria — not something to
  guess at mid-merge.
- Rewriting `tests/session/test_session_location.py` (deleted here, 14
  tests) against moeka's SQLite store instead of `JsonlSessionStore`.

**Recommendation for the owner:** decide whether moeka should adopt
ADR-0001's out-of-workspace-by-default posture (likely yes, given the
permissive shell sandbox), and if so, decide the migration story for
`~/.nanobot/sessions.db` before any batch attempts it. Until then, future
batches should keep treating `sessions_root` as opt-in only, matching what
this batch shipped.

### Docker test results
Full suite: **5326 passed, 4 failed, 16 skipped** (was 5130/3/16 at the
end of batch 1). All 4 failures are the same two pre-existing environment
artifacts as batch 1 (no DNS/network egress in this sandbox; no
`npx`/playwright on PATH in the test image) — one more test in each
family now exists in this batch's new test coverage and hits the same
known limits. No new failure *categories*. Two Docker/test infra gaps
fixed along the way (both pre-existing, unrelated to merge *content*,
just newly triggered): `Dockerfile.test` never copied `scripts/` (needed
by `tests/channels/test_channel_plugins.py`, actually surfaced in batch 1
too but only now hit) and never had access to one webui fixture file
needed by a new cross-language test
(`tests/utils/test_webui_event_projection_equivalence.py`) — added a
narrow `.dockerignore` negation + targeted `COPY` for just that file
rather than shipping the whole (git-ignored, large) `webui/` tree.

### Real regressions caught and fixed (see fixup commit `277e367b`)
- `nanobot/core/core.py`'s `MoekaCore.from_config()` broke entirely (33
  tests) because upstream made `AgentLoop.from_config()`'s `tool_registry`
  a required kwarg via an unconflicted signature change, and moeka's own
  `nanobot/core` (0 upstream commits touch it, per the original survey)
  never got updated to match. This is exactly the "conflicts touch
  nanobot/core" risk category — except it wasn't a *conflict*, it was
  silent breakage from an upstream change to a *shared* function
  signature. Fixed by constructing a `ToolRegistry()` there.
- `retain_recent_legal_suffix()` delivery-drop bug — see above.

### Deviation verification (post-merge, on the final commit)
Same checklist as batch 1, all still holding: `allow_sudo` defaults
False, no destructive patterns in `_DEFAULT_DENY_PATTERNS`, `sessions.db`
SQLite store (still in-workspace by default — see ADR-0001 note above),
`_dispatch_with_watchdog` present, `bg_shell.enabled()` returns False,
`nanobot channels enable/disable` CLI present, Telegram
`drop_pending_updates=True`, lazy `Config`/`ToolsConfig` model_rebuild
intact, no `CONTRIBUTING.md`/`nanobot_logo.png`.

## Batch 2.5 — session store conversion to upstream's SessionStore Protocol

Not a numbered batch (no new upstream commits merged) — this is the
SqliteSessionStore/ADR-0001 conversion the owner asked for before
continuing to batch 3, done in three commits on top of `71150b04`
(the read-only plugin-seam assessment that made this decidable). Full
detail, including the incident that interrupted the write-up, is in
`.agent/upstream-sync-handover.md`; this section is the short version so
the record here isn't a gap.

- **`3cb4f03c`** — `nanobot/session/manager.py` reset to byte-identical
  with the batch-2 checkpoint (`0c684c5a`); all of moeka's SQLite session
  logic moved into a new `nanobot/session/sqlite_store.py`, implementing
  upstream's `SessionStore` Protocol. ADR-0001 adopted: sessions live
  outside the workspace by default, at a workspace-relative sibling
  directory (`default_sessions_root()`). The three `_conn()` reach-ins
  (`session_list_index.py`, `memory.py`, the CLI restore command) fixed
  via a `get_store()` accessor. Dockerfile.test's CMD fixed to actually
  run `nanobot/channels/` (previously silently skipped). First structural
  test-isolation layer added (root `conftest.py` $HOME redirect + mkdir
  guard; `tests/conftest.py`'s `_guard_live_workspace` extended to wrap
  the store constructors directly).
- **`b013eba7`** — `Dockerfile.test` never `COPY`'d the root `conftest.py`,
  so the isolation layer from `3cb4f03c` was inactive in Docker. Fixed.
  This had inflated an apparent 45-test regression to the real number: 7
  (all `tests/cli/test_commands.py`, all classification (b) — see the
  handover doc §3 for the full enumeration). Fixed those 7 via explicit
  `tmp_path` workspace overrides.
- **`5f4c6872`** — the migrate command. Startup no longer auto-migrates a
  legacy in-workspace `sessions.db` (only warns, via
  `_warn_if_legacy_db_in_workspace`). New `nanobot sessions migrate --from
  --to [--no-dry-run] [--yes] [--force]`: dry-run by default (two-key
  arming to actually move anything), refuses an existing destination,
  refuses a source that looks held open by another process unless
  `--force`, verifies destination `PRAGMA integrity_check` before
  releasing the source, rolls back on failure.

Success criterion as of this commit: `git diff 0c684c5a... --
nanobot/session/manager.py` is empty. Docker (`scripts/test-docker.sh`):
6333 passed, 4 failed (same known pre-existing artifacts), 31 skipped.

Not done yet: consolidating the three eager `SqliteSessionStore(...)`
construction sites behind one factory seam, and fixing
`nanobot/gateway/service.py`'s bare `Path.home()` fallback. See the
handover doc for the full remaining-work list and risk assessment before
picking this back up.

## Incident — 2026-09-17: live session DB relocated by a test-triggered migration

**What happened.** During the SqliteSessionStore/ADR-0001 conversion work,
a test process constructed a session store against the real `~/.nanobot`
workspace, which tripped ADR-0001's out-of-workspace migration logic
against `moeka.service`'s live database (PID 2091). I caught it mid-way
(a `mv` I attempted to undo it was correctly blocked by the permission
system as irreversible), stopped immediately, and reported rather than
forcing the corrective move. The coordinator restored the data personally
and confirmed integrity (same inodes, 4 sessions / 1,738 messages intact).
Separately, 24 stray directories under the legacy `~/.nanobot/sessions/`
tree were found to contain real data (22 of 24 non-empty, 35 messages,
written 04:58 that day) — **not** "schema-only" as I first, wrongly,
asserted without checking row counts. Lesson: never claim "no real data"
without having verified content.

### 1. Root cause, precisely

Two independent constructor call sites in **moeka's own code** build a
`SqliteSessionStore` *eagerly, as a constructor argument*, before the
(mockable) `SessionManager` is ever called — so the test suite's existing
`session_manager=` monkeypatch seam (`_patch_cli_command_runtime` in
`tests/cli/test_commands.py`, which patches
`nanobot.session.manager.SessionManager`) never intercepts them:

- `nanobot/cli/commands.py:360`, inside `serve()`:
  `store=SqliteSessionStore(runtime_config.workspace_path, sessions_root=_sessions_root)`
- `nanobot/cli/gateway_runtime.py:393`, inside `_run_gateway()`:
  `store=SqliteSessionStore(config.workspace_path, sessions_root=_sessions_root)`

Reproduced directly: `tests/cli/test_commands.py::test_serve_passes_configured_api_key`
(and its siblings `test_serve_allows_loopback_without_api_key`,
`test_serve_cli_options_override_api_config`, plus the three
`test_gateway_*` tests that share `_patch_serve_runtime`/
`_patch_cli_command_runtime`) construct `config = Config()` with **no**
`config.agents.defaults.workspace` override — unlike the one sibling test
that does (`test_serve_uses_api_config_defaults_and_workspace_override`,
which passes). With no override, `runtime_config.workspace_path` falls
back to the pydantic default `AgentDefaults.workspace: str = "~/.nanobot"`
(`nanobot/config/schema.py:141`), which `Path(...).expanduser()`
resolved — before this incident's fixes existed — straight to the real
developer/service home, because nothing in the test process redirected
`$HOME` or `MOEKA_WORKSPACE`/`NANOBOT_HOME` at the time. "A test or an
ad-hoc script" is not the root cause; the root cause is these two
call sites constructing a store as an unguarded constructor argument,
combined with a test file that (for 6 of ~9 `serve`/`gateway` tests)
never overrides the workspace default and had no structural isolation
forcing it to.

### 2. Every isolation hole found in the branch (not just mine)

Moeka's code:
- `nanobot/cli/commands.py:360` and `:468` (`sessions restore-workspace`),
  `nanobot/cli/gateway_runtime.py:393` — eager `SqliteSessionStore(...)`
  construction as above.
- `nanobot/gateway/service.py:217-224` — `resolve_workspace()` falls back
  to bare `Path.home()` (not even `get_state_home()`'s
  `MOEKA_WORKSPACE`/`NANOBOT_HOME` override chain) when no
  `--workspace`/`options.workspace` is given.

Upstream's code (unforked, byte-identical to `upstream/main` per the
merge's success criterion):
- `nanobot/session/manager.py`'s `JsonlSessionStore.__init__` (line ~555)
  defaults `sessions_root` to `get_runtime_subdir("sessions")` when not
  passed explicitly — this chain (`get_runtime_subdir` →
  `get_data_dir()` → `get_config_path().parent` → ... → `get_state_home()`
  → `Path.home() / ".nanobot"`, all in `nanobot/config/paths.py`) is what
  most plausibly created the 24 stray `~/.nanobot/sessions/<id>/`
  directories: any test or code path constructing a `JsonlSessionStore`
  (or, before the SQLite conversion, the old `SessionManager`) without an
  explicit `sessions_root=` and without `$HOME`/env redirection lands on
  the real home. This is a second, independent hole, not something the
  SQLite conversion introduced.
- `get_state_home()` itself (`nanobot/config/paths.py:14-48`) is the
  single real chokepoint for the "default resolves under real `$HOME`"
  family — every other `get_*_dir()` helper in that file routes through
  it, so it's the one function that needed the `$HOME`/env-var
  redirection (now in root `conftest.py`), not each call site individually.

Test suite (before this incident's fixes):
- No autouse fixture redirected `$HOME`/`Path.home()` for the whole
  session; isolation was "remember to pass an explicit workspace/tmp_path
  per test", which ~6 tests in `tests/cli/test_commands.py` didn't do.
- `tests/conftest.py`'s pre-existing `_guard_live_workspace` fixture only
  wrapped `SessionManager.__init__`, not `JsonlSessionStore.__init__` or
  `SqliteSessionStore.__init__` directly — so a code path that constructed
  either store type without going through `SessionManager` (exactly what
  `commands.py:360` and `gateway_runtime.py:393` do) had no guard at all.

### 3. Proposed design fix (not implemented yet — proposal only)

Per the coordinator's explicit instruction, migration must stop being an
implicit side effect of construction:

- Make `SqliteSessionStore.__init__` **never** migrate automatically.
  Startup (`serve`, `gateway`, `nanobot agent`, etc.) may *detect* a
  legacy `sessions.db` sitting inside the workspace or under the legacy
  jsonl dir and log a loud, actionable warning ("legacy session data
  found at X; run `nanobot sessions migrate --dry-run` to review"), but
  must never move a file itself.
  a `nanobot sessions migrate <source> <destination>` command:
  - Requires both `--from`/`--to` explicitly (or well-defined
    `--from-legacy-workspace`/`--to-sessions-root` flags with the
    resolved paths always echoed back before acting).
  - `--dry-run` (default-safe posture: consider making `--dry-run` the
    implicit behavior unless `--execute`/`--yes` is also passed) prints
    exactly what it would move — file names, byte sizes, source/dest —
    without touching anything.
  - Refuses to run unattended: requires either an interactive
    confirmation prompt or an explicit `--yes`.
  - Uses the existing `FileLock` migration lock and moves the
    `sessions.db`/`-wal`/`-shm` triplet atomically together, verifying an
    integrity check (`PRAGMA integrity_check`) on the destination before
    declaring success, with the source left untouched until that check
    passes.
  - The `nanobot sessions restore-workspace` CLI command
    (`nanobot/cli/commands.py:468`, already explicit/opt-in) is the right
    shape to model this on — it already takes explicit source/destination
    and never runs implicitly.
- The three eager `SqliteSessionStore(...)` construction call sites
  (`commands.py:360`, `commands.py:468`, `gateway_runtime.py:393`) should
  all go through one small factory/seam so tests can inject a fake
  without needing to know each call site's constructor shape by heart —
  this both fixes today's mock gap and gives batch 3+ one seam to extend
  instead of three.

### 4. Structural test isolation (implemented, verified)

- Root `conftest.py`: `pytest_configure()` (runs before collection,
  earlier than any fixture) redirects `$HOME`/`USERPROFILE` to a fresh
  `tempfile.mkdtemp()` for the whole test session, clears
  `MOEKA_WORKSPACE`/`MOEKA_STATE`/`NANOBOT_HOME`, and wraps
  `pathlib.Path.mkdir`/`os.makedirs`/`os.mkdir` to raise loudly if
  anything still resolves under the *real* `~/.nanobot` or
  `~/.nanobot-sessions` (guard is scoped to those two paths, not all of
  `$HOME`, so pytest's own `.pytest_cache` etc. under the real checkout
  stay writable).
- `tests/conftest.py`'s session-scoped `_guard_live_workspace` fixture
  extended to also wrap `JsonlSessionStore.__init__` and
  `SqliteSessionStore.__init__` directly (previously only
  `SessionManager.__init__`), so any construction path that bypasses
  `SessionManager` is still caught.
- Verified: this guard newly fails the exact 9 tests named in item 1/2
  above (nothing else) — i.e. "a test that tries to touch the real home
  cannot pass" now holds structurally, not by convention. Those 9 failures
  are not yet fixed (that's follow-up work, likely adding explicit
  `config.agents.defaults.workspace = str(tmp_path / ...)` to each,
  matching the one sibling test that already does this and passes).

## STOPPED HERE per coordinator instruction

Per the coordinator's stop criteria ("a merge requires a judgment call
that changes runtime behaviour of the live service"), I completed batch 2
to a clean, fully-tested state but did **not** start batch 3. Next batch
(3/7) should merge checkpoint `1018bdb7fee35acd6a52e2409d15cec65d3d3c09`
(2026-08-19, 167 commits) once the owner has weighed in on ADR-0001 above
— that decision will shape how `nanobot/session/manager.py` conflicts get
resolved from here on, since upstream will keep extending the
`SessionStore` split every batch.

## Batch 3 — 2026-09-17

Merge commit: recorded below after commit (this section is written before
the commit lands; see the commit that carries this same notes update for
the final sha — it is the merge commit itself, git-merge on top of
`51d0f996`, checkpoint `1018bdb7fee35acd6a52e2409d15cec65d3d3c09` (2026-08-19,
167 commits from `upstream/main`). Worked in worktree
`/home/muk/projects/moeka-sync` on branch `merge/upstream-main-2026-08-05`;
`/home/muk/projects/moeka` stayed on `main` throughout (verified before and
after).

### `nanobot/session/manager.py` (rule 1)
Auto-merged with **zero conflict markers** in this batch (the file was
already byte-identical to upstream going in, from the batch-2.5 conversion,
and this batch's upstream commits to it applied cleanly on top).
`git diff 1018bdb7... -- nanobot/session/manager.py` is empty — confirmed.

The Protocol did change shape this batch: `SessionStore` gained a new
`update_metadata(key, updates, *, fsync=False) -> bool` method, consumed by
a brand-new feature this batch (`nanobot/session/session_handles.py` —
pronounceable per-session "handles" for the WebUI / cross-session
mentions, via `SessionManager.update_session_metadata()`). moeka's
`SqliteSessionStore` did not implement it and would have raised
`AttributeError` the first time a WebUI session needed a handle. Added
`SqliteSessionStore.update_metadata()` (merges `updates` into the stored
metadata JSON blob, deliberately does not touch `updated_at` — matches
`JsonlSessionStore.update_metadata`'s contract exactly, verified against
its implementation).

Also discovered and removed, from the same "wholesale upstream" file: the
entire `enforce_file_cap` / `FILE_MAX_MESSAGES` / `set_file_cap_archiver`
mechanism (upstream commit `9ef1e292`, "fix(session): preserve complete
transcripts", deliberately removed a hard 2000-message-count cap in favor
of token-budget-based replay slicing + Dream/Consolidator archiving for
unbounded growth). moeka's `AgentLoop.__init__` called
`self.sessions.set_file_cap_archiver(self.context.memory.raw_archive)`
unconditionally — this method no longer exists on the upstream
`SessionManager`, so the merge would have crashed on every `AgentLoop`
construction. This was a **clean merge with no conflict markers** (loop.py
had its own separate, unrelated conflict elsewhere) — exactly the "silent
breakage in a cleanly-merged region" risk flagged going in. Removed the
call site in `nanobot/agent/loop.py`; `MemoryStore.raw_archive` itself is
untouched and still used by the Consolidator's own fallback paths in
`nanobot/agent/memory.py`. Confirmed no other reference to
`set_file_cap_archiver`/`enforce_file_cap`/`FILE_MAX_MESSAGES`/
`replay_max_messages_for_context`/`_file_cap_archiver` remains anywhere in
`nanobot/` or `tests/`. `tests/agent/test_max_messages_config.py` (221
lines, entirely about the removed cap) was deleted by the same upstream
commit; kept deleted.

### Conflicts resolved (14 files)
- `README.md` — kept moeka's (ours) entirely; branding identity, always-keep.
- `CONTRIBUTING.md` — modify/delete conflict (upstream modified, moeka
  deleted); kept deleted (moeka policy).
- `nanobot/agent/context.py` — upstream added a typed `SessionSummary`
  (`nanobot/session/summary.py`, new file, unconflicted) replacing a bare
  `str`, plus a `_without_duplicate_session_summary()` dedup pass so the
  "Recent History" section never repeats text already covered by the
  archived-context summary. Ported both onto moeka's hybrid
  recency+semantic `_build_history_section()` (VecStore-backed retrieval,
  moeka's own): `session_summary` is now typed `SessionSummary | None`
  throughout, and the dedup filter runs on `_build_history_section`'s
  combined (recency + semantic) entry list before rendering.
- `nanobot/agent/loop.py` — import-block and constructor-body conflicts;
  kept moeka's full constructor (vec_config/vec_store/ContextBuilder
  wiring, SqliteSessionStore construction with the ADR-0001 ordering
  comment) and re-attached upstream's `FileStateStore(max_sessions=...)`
  + `set_delete_observer` wiring, and the new `SessionSummary` import,
  both wanted. Also the file-cap removal above (not itself a conflict).
- `nanobot/agent/memory.py` — `prune_dream_sessions`: kept moeka's
  SQLite-query version (`SELECT key FROM sessions WHERE key LIKE
  'dream:%'`) over upstream's jsonl-glob-and-mtime-sort version; memory
  category, moeka's shape is the identity.
- `nanobot/channels/telegram/runtime.py` — upstream rewrote the polling
  loop into a proper stale-poll watchdog (`_watch_polling`,
  `_wait_for_app`, `_teardown_app`, `_note_poll_ok`) replacing moeka's
  simple "is `self._app` None" loop; took upstream's version wholesale
  (channels category). `drop_pending_updates` default-True wiring was
  untouched/unconflicted elsewhere in the same file — reverified intact.
- `nanobot/channels/websocket/runtime.py` — both sides added an unrelated
  field to the same outbound payload dict (moeka: inline `buttons`/
  `button_prompt`; upstream: `turn_id` from `WEBUI_TURN_METADATA_KEY`);
  kept both.
- `nanobot/channels/websocket/tests/test_websocket_http_routes.py` — pure
  import-list conflict (moeka added `SqliteSessionStore`, upstream added
  `SessionHandleResolver`); kept both.
- `nanobot/cli/gateway_runtime.py` — one real hunk: kept moeka's
  `store.reindex_memory()` call after Dream compaction (VecStore refresh).
  Everything else in this file's diff (`build_default_session_manager`,
  `GatewayInstance`/`GatewayClientLease` wiring) auto-merged clean.
- `nanobot/command/builtin.py` — same `store.reindex_memory()` pattern in
  `/dream`'s command handler; kept moeka's call.
- `nanobot/cron/service.py` — two hunks. Took upstream's `_store_dirty`
  flag + `store_path.parent.mkdir()` (durability bookkeeping) and its new
  try/except around the tick body (keeps in-memory state and retries next
  tick on any load/persist failure) wholesale. This **dropped** moeka's
  own "only persist when a job actually ran" optimization (commit
  `558fe35e`, guards against `jobs.json` being rewritten on every idle
  tick and clobbering concurrent hand edits) — per rule 3 (not a
  CLAUDE.md-documented deviation), taking upstream and flagging the drop
  here rather than silently keeping it. ⚠️ Flagged below for review — this
  one reads like a real, deliberate moeka bug fix, not an incidental diff.
- `nanobot/webui/session_list_index.py` — the other big one. Upstream
  (701 lines, up from ~260) added: a persistent `.webui_session_index.json`
  scan-cache (same shape as batch 1, already dropped — moeka's SQLite
  store answers the sidebar query in one statement) **and** a genuinely
  new, well-tested feature this batch: recovering sessions that exist only
  as a WebUI transcript file with no matching `sessions.db` row yet (e.g. a
  crash between the first WebUI turn and the agent's own session save).
  Rewrote the module from moeka's clean (cache-free) base, porting in just
  the transcript-recovery logic (`_webui_transcript_sources`,
  `_scan_transcript_row`, `_valid_transcript_session_key`,
  `_transcript_preview`/`_transcript_created_at`) — `list_webui_sessions()`
  now also scans `get_webui_dir()` for transcript files with no matching
  canonical session key and synthesizes a sidebar row for them. Dropped
  the cache-mechanics tests (don't apply, no cache); ported 8
  transcript-recovery tests from upstream's suite, adapted to moeka's
  `_manager()` SQLite fixture helper — all pass (22/22 in the rebuilt
  `tests/webui/test_session_list_index.py`).
- `tests/agent/test_dream_session.py` — kept moeka's 3 SQLite-based
  `TestPruneDreamSessions` tests; dropped upstream's jsonl-file-glob
  equivalents (don't apply to the SQLite backend, matches the
  `prune_dream_sessions` resolution above).
- `tests/agent/test_session_atomic.py` — kept moeka's SQLite-focused
  `TestSqliteRoundtrip` tests; additionally kept upstream's 3 new
  jsonl-mechanics tests (`test_unique_tmp_file_cleaned_up_on_write_failure`,
  `test_overwrite_preserves_latest_data`,
  `test_managers_for_same_directory_coordinate_saves`) since they exercise
  the *unforked*, always-constructed internal `JsonlSessionStore` directly
  — real reachable code in manager.py, worth testing on its own merits
  even though moeka's production path doesn't persist through it.
- `tests/session/test_session_fsync.py` — same reasoning: kept moeka's
  WAL-checkpoint durability tests as-is, but instead of dropping upstream's
  2 new directory-fsync-fallback tests (which don't make sense against the
  SQLite-backed `manager` fixture — patches on `nanobot.session.manager.os.*`
  would never fire), added a `TestJsonlStoreDirectoryFsync` class with its
  own bare, jsonl-backed `SessionManager` fixture so they exercise what
  they're actually meant to.
- `tests/cli/test_commands.py` — one hunk: kept moeka's already-existing
  comment explaining why
  `test_webui_missing_runtime_env_fails_before_starting_gateway` is
  dropped (documented CLAUDE.md deviation: missing `${VAR}` warns, not
  hard-fails).
- `tests/session/test_session_store.py` — modify/delete (moeka deleted it
  in batch 1 because every test there constructed `SessionManager(...,
  store=...)` against a Protocol moeka hadn't adopted yet). Since the
  batch-2.5 conversion, moeka's `SqliteSessionStore` **is** a real
  `SessionStore` Protocol implementation now, so took upstream's version
  wholesale — all 5 tests pass unmodified (they use a `MagicMock(spec=
  SessionStore)`, backend-agnostic by construction).

### Dropped in favor of upstream (rule 3), summary
- `nanobot/cron/service.py`'s "only persist on a tick that ran a job"
  optimization (see above) — ⚠️ flagged for review, see below.
- `test_webui_missing_runtime_env_fails_before_starting_gateway` — not
  newly dropped this batch, same pre-existing moeka policy, just
  reconfirmed at the conflict site.

### Real regressions caught and fixed post-merge (Docker-surfaced)
Both were **clean, unconflicted merges** that broke silently — exactly the
failure mode flagged going in:
- `tests/agent/test_thought_process.py` (4 failures) — its `_response()`
  `SimpleNamespace` mock helper was missing two new `LLMResponse` fields
  upstream added and `nanobot/agent/runner.py`'s (unconflicted) new
  `_usage_or_estimate()` now reads unconditionally: `generation_ms` and
  `ttft_ms` (both `int | None`, streaming telemetry). Added both to the
  mock with `None` defaults, matching the real dataclass.
- `tests/tools/test_exec_allow_patterns.py::test_guard_newline_chained_segment_still_hits_deny_patterns`
  (new test this batch, unconflicted insertion) — asserted a denied-later-
  segment ("`rm -rf /`" after an allowlisted first line) gets blocked by
  the *deny pattern* filter. moeka doesn't deny `rm -rf` by default (the
  permissive-sandbox deviation), so with `allow_patterns` set the same
  command is still correctly blocked, just via the *allowlist* filter
  instead. Rewrote the assertion + added a comment explaining why, rather
  than dropping the test (the underlying blocking behavior is correct,
  just via a different one of moeka's two independent guard mechanisms).

### "Clean merge, hunt for silent breakage" check — results
- `git diff HEAD@{1} -- nanobot/core/` (pre-merge state vs. merged/staged
  tree): **empty**. Matches the survey ("0 upstream commits touch
  nanobot/core in this window") — no MoekaCore/VecStore regression this
  batch, unlike batch 2's `AgentLoop.from_config()` incident.
- `Session` dataclass field set: unchanged between the batch-2 and
  batch-3 checkpoints (diffed directly) — no drift for `sqlite_store.py`'s
  row (de)serialization to worry about.
- `SessionManager.__init__` body: one real diff between checkpoints
  (`_file_cap_archiver` init line removed, `_delete_observer` init line
  already present from batch 2) — consistent with the file-cap removal
  above, no other signature/behavior drift found.
- Grepped every `nanobot/` and `tests/` reference to
  `set_file_cap_archiver`/`enforce_file_cap`/`FILE_MAX_MESSAGES`/
  `replay_max_messages_for_context`/`_file_cap_archiver`: only the
  explanatory comment left in `loop.py`. Grepped `.get_history(` call
  sites tree-wide: all already match the new `max_messages: int = 0`
  default semantics (auto-merged clean, no fixup needed).
- `sqlite_store.py`'s imports from `manager.py` (`_SESSION_LIST_PREVIEW_*`,
  `_SESSION_MIGRATION_LOCK_TIMEOUT_SECONDS`, `JsonlSessionStore`,
  `Session`, `SessionInfo`, `SessionMetadataPayload`, `SessionPayload`,
  `SessionRestoreResult`, `_message_preview_text`, `_metadata_title`) —
  all still present, all still same shape; confirmed via direct import
  (`python -c "import nanobot.session.sqlite_store"` etc.) and via the
  full Docker suite passing.

### Deviation verification (post-merge, on the final commit)
- `allow_sudo: bool = False` — default unchanged.
- `rm -rf`/`dd`/`mkfs`/`shutdown` absent from `_DEFAULT_DENY_PATTERNS`
  (only the fork-bomb pattern remains there); `_INTERNAL_DENY_PATTERNS`
  untouched.
- `tools.exec.allow_patterns` non-empty ⇒ whitelist-only mode — confirmed
  intact (and re-exercised by the fixed
  `test_guard_newline_chained_segment_still_hits_deny_patterns` above).
- `nanobot/session/manager.py`/`sqlite_store.py` both reference
  `sessions.db` (WAL) — confirmed.
- `ChannelManager._dispatch_with_watchdog` present and wired
  (`nanobot/channels/manager.py`).
- `bg_shell`'s `enabled(ctx)` still hardcoded `return False`.
- `nanobot channels enable/disable` CLI commands present in
  `nanobot/cli/commands.py` (untouched by this batch).
- Telegram `drop_pending_updates: bool = True` — confirmed, survived the
  polling-loop rewrite above.
- Lazy `Config`/`ToolsConfig` model_rebuild pattern — untouched by this
  batch (0 conflicts in `nanobot/config/schema.py` or `loader.py`).
- No `CONTRIBUTING.md`, no `images/nanobot_logo.png`;
  `images/GitHub_README.png` present.
- `.agent/*`, `CLAUDE.md`, `bin/*` launchers untouched by this batch.
- `git diff 1018bdb7fee35acd6a52e2409d15cec65d3d3c09 -- nanobot/session/manager.py`
  — empty, confirmed (rule 1 success criterion).

### `ruff check nanobot/ tests/` — clean, no new lint.

### Docker test results
Full suite (`scripts/test-docker.sh`): **6546 passed, 4 failed, 33
skipped** (baseline going in: 6348/4/31; +198 passed / +2 skipped from
this batch's new upstream test coverage, same 4 pre-existing failures, no
new failure categories). The 4 failures are the same two known
environment artifacts as every prior batch: `test_exec_guard_allows_public_urls`
×2 (no DNS/network egress in this sandbox) and the two
`tests/webui/test_mcp_presets_api.py::test_test_mcp_preset_*` tests (no
`npx`/playwright on PATH in the test image).

### ⚠️ Needs human review
- **`nanobot/cron/service.py`'s dropped "only persist when a job ran"
  optimization** (see above). This was a real moeka bug fix (commit
  `558fe35e`) protecting `jobs.json` from being rewritten every idle tick
  and clobbering concurrent hand edits to the file. Upstream's replacement
  (try/except around the tick, `_store_dirty` flag) is a genuine
  improvement for crash-resilience but doesn't address the same
  clobbering scenario. Recommend either: (a) re-adding the `if due_jobs:`
  guard on top of upstream's try/except in a follow-up commit, or (b)
  confirming the clobbering scenario no longer applies for some other
  reason and documenting why. Not re-added here per the merge's explicit
  "take upstream unless CLAUDE.md-documented" rule, but flagging loudly
  since dropping it silently would be exactly the kind of regression this
  whole exercise is trying to avoid.
- The transcript-recovery feature ported into
  `nanobot/webui/session_list_index.py` is new, real functionality (not
  just a shape-preserving port) — worth a closer look/smoke-test in the
  live WebUI at some point, though it's covered by 8 passing unit tests
  here.
- ADR-0001 out-of-workspace session storage: already adopted (batch 2.5),
  not revisited this batch — no new upstream commits touched that surface
  area in this window.

## Batch 4 — 2026-09-17

Merge commit `505fff2e` into `merge/upstream-main-2026-08-05`, checkpoint
`f9d449ef6c6d3d307c663890ae55d1eac4d088f8` (2026-08-26, "refactor(webui):
isolate websocket application orchestration (#5548)"), 92 commits since
batch 3's `1018bdb7`. Two follow-up fixup commits: `cdc7a5ae` (uv.lock
regen) and `7136de6d` + `d16b3608` (post-test-run fixes — see below).

### Conflicts resolved (11 files + 1 modify/delete)
- `README.md` — took upstream wholesale, then stripped the two
  `CONTRIBUTING.md` links it reintroduced (file stays deleted on moeka).
- `nanobot/session/manager.py` — took upstream wholesale, unconditional
  (rule 1). `git diff f9d449ef... -- nanobot/session/manager.py` empty,
  confirmed.
- `nanobot/agent/loop.py`, `runner.py`, `subagent.py` — combined moeka's
  `bootstrap_overrides`/`inline_skills`/`tools_allow`/`tools_deny`/
  AskUserInterrupt surface with upstream's `recovery_admission`/
  `continuation_callback`/`terminal_injection_callback` refactor. See
  "Silent breakage" below — this conflict resolution alone left several
  bugs that only surfaced once the goal-continuation dead code
  (`goal_active_predicate`/`goal_continue_message`/
  `build_goal_continue_message`) was dropped in favor of upstream's
  equivalent `continuation_callback` (same `_goal_continue` closure,
  simpler interface — confirmed functionally identical before removing).
- `nanobot/agent/skills.py` — merged moeka's inline-skills group
  (`root=None`, no on-disk path) with upstream's relative-path
  display-root feature (`use_relative_roots`/`display_root`).
- `nanobot/agent/tools/shell.py` — kept moeka's permissive-sandbox
  `description` property in full; upstream reduced it to a one-line stub
  ("Execute a shell command."). See "Silent breakage" below for a second,
  non-conflicted regression found in this same file.
- `nanobot/cli/gateway_runtime.py` — kept moeka's
  `sqlite_store.build_default_session_manager` import alongside upstream's
  new `RecoveryCoordinator` (both used: `session_manager =
  build_default_session_manager(...)`, `recovery = RecoveryCoordinator(
  sessions=session_manager, ...)`).
- `nanobot/utils/runtime.py` — dropped moeka's `build_goal_continue_message`
  (superseded, see above); kept `build_tool_failure_reflection_message`.
- `nanobot/webui/session_list_index.py` — kept moeka's SQLite-direct-query
  shape (no jsonl-scan `.webui_session_index.json` cache — the module's
  own docstring already explains why: `SessionManager.list_sessions()` is
  already one cheap SQL query). Ported in two genuine upstream
  improvements: (a) the `nanobot.webui.session_identity` helpers
  (`webui_session_key`/`webui_chat_id`/`WEBUI_SESSION_STORAGE_PREFIX`),
  replacing ad-hoc `"websocket:"` string literals, matching what the rest
  of `nanobot/webui/` already migrated to in this same batch; (b) the new
  `recovery_state` field, wired through
  `SqliteSessionStore.list_sessions()` via the new
  `recovery_state_from_metadata()` (mirrors the existing `model_preset`/
  workspace-scope computed-field pattern there).
- `tests/agent/test_runner_persistence.py` — kept deleted (`git rm`).
  Moeka consolidated its coverage into `tests/agent/test_runner.py` before
  batch 3 (commit `99cc6ee8`); all 6 of upstream's modified test names in
  this batch already exist there under the same names.
- `tests/cli/test_commands.py` — kept moeka's `**_kwargs`-tolerant
  `session_manager=lambda _workspace, **_kwargs: ...` lambda signature
  (`build_default_session_manager` calls `SessionManager(workspace,
  sessions_root=..., store=...)`, so the monkeypatched class must accept
  those kwargs) while adopting upstream's stronger
  `_EmptyGatewaySessionManager` fake (`list_sessions()`/`flush_all()`)
  over a bare `object()`, across all 7 occurrences.
- `tests/cron/test_cron_service.py` — kept moeka's `_dream_job()` helper
  and its 4 restart/reconciliation/validation tests
  (`test_register_system_job_preserves_run_history_across_restart`,
  `_reconciles_schedule_changes`, `test_add_job_rejects_unparseable_cron_expression`),
  added upstream's 2 new tests
  (`test_remove_system_job_retires_persisted_system_job`,
  `test_remove_system_job_without_store_file`) alongside them, reusing
  `_dream_job()` in the former instead of upstream's inline duplicate.
- `tests/webui/test_session_list_index.py` — dropped the
  `.webui_session_index.json` temp-file-cache-specific test (not
  applicable to moeka's SQLite-direct shape, matching the production-code
  decision above); kept/adapted upstream's model-preset-rename test and
  added the new pending-recovery-state test, both switched onto moeka's
  `_manager()` `SqliteSessionStore` fixture instead of a bare
  `SessionManager(tmp_path)`.

### Dropped in favor of upstream (one-line reason each)
- `AgentRunSpec.stream_progress_deltas` / the direct `progress_callback`
  wiring in `AgentLoop`'s main-turn `AgentRunSpec` construction — upstream
  refactored progress delivery to route through the `AgentTurnHookSpec`/
  `build_agent_turn_hook` hook system instead; the field is gone from
  `runner.py` entirely and `loop.py`'s call site already only used the
  hook-based path post-merge (auto-merged clean, verified equivalent).
- `AgentProgressHook.__init__`'s `on_iteration` callback param — dead code,
  zero call sites anywhere in the tree even pre-merge; upstream dropped it
  cleanly.
- **`fail_on_tool_error` (the whole feature, config field through
  runner.py's `spec.fail_on_tool_error` gating)** — this was originally an
  upstream feature (PR #4198, not moeka-authored) that upstream itself
  deliberately removed this batch. Confirmed via two new upstream tests:
  `tests/config/test_config_migration.py::test_load_config_ignores_removed_fail_on_tool_error`
  and `::test_save_config_drops_removed_fail_on_tool_error`, both
  asserting the config field is gone. See "Silent breakage" below for how
  this was initially mishandled.

### Protocol-shape changes found
`class SessionStore(Protocol)` in `manager.py` is unchanged from batch 3's
checkpoint (still has `update_metadata`) — no new Protocol methods this
batch. `sqlite_store.py` needed one addition regardless: `list_sessions()`
now also computes `recovery_state` per row (see session_list_index.py
above), mirroring the existing `model_preset`/workspace-scope pattern —
not a Protocol requirement, but a "keep the extra computed fields in sync
with what the WebUI sidebar needs" requirement, same shape as batch 3's
`update_metadata()` addition.

### Silent breakage in cleanly-merged code — three real regressions found

This bit again, a third time running (see batches 3's writeup for the
first two). All three were only caught because the full Docker suite was
run and its *entire* failure list was read, not just skimmed for
known-baseline matches.

1. **`nanobot/agent/runner.py`'s `_execute_tools`/`_run_tool` AskUserInterrupt
   3-tuple contract silently collapsed to upstream's 2-tuple.** Moeka's
   `ask_user` tool (`nanobot/agent/tools/ask.py`) relies on
   `_run_tool`/`_execute_tools` returning `(result, event, fatal_error)`
   so an `AskUserInterrupt` can propagate up and stop the turn cleanly.
   Upstream has no such concept at all (confirmed: `AskUserInterrupt`
   doesn't exist anywhere upstream). The merge's automatic resolution of
   a *nearby* conflict (the `fail_on_tool_error`-gated branches) somehow
   left `_execute_tools`'s signature/return at upstream's clean 2-tuple
   while the loop-body code still called `_run_tool` expecting 3 values
   in one place and 2 in another — inconsistent within the same function,
   which would have raised `TypeError`/`NameError` on the very first tool
   call. Restored the full 3-tuple contract end-to-end
   (`_execute_tools`/`_run_tool`/`_classify_violation`/the `fatal_error`
   unpack in the main iteration loop).
2. **`fail_on_tool_error` restoration itself was wrong, then wrong again
   in a different way.** First pass (in the merge commit `505fff2e`)
   treated the field's disappearance as an ordinary silent-merge casualty
   and "restored" it everywhere *except* forgetting to add the field to
   the `AgentRunSpec` dataclass itself — so `spec.fail_on_tool_error`
   raised `AttributeError` on every tool-error path, breaking ~40 tests.
   Investigating that `AttributeError` surfaced the *actual* story: this
   wasn't a silent regression at all, it was upstream's deliberate,
   tested removal (see "Dropped in favor of upstream" above). Second pass
   (commit `7136de6d`) fully reverted the restoration instead of fixing
   the missing field, matching upstream's decision.
3. **`nanobot/agent/tools/shell.py`: the command guard got silently gated
   behind `restrict_to_workspace`.** Pre-merge, `_prepare_command()`
   called `self._guard_command(...)` *unconditionally* — `restrict_to_workspace`
   was passed as a parameter *into* the guard (affecting what it checks:
   path-traversal detection), not whether the guard runs at all. Upstream
   added a new "full workspace access is an explicit trust decision, skip
   the guard entirely" feature this batch, wrapping the whole call in
   `if access.restrict_to_workspace:` — clean merge, no conflict markers,
   silently made deny_patterns/allow_patterns and even the hard SSRF
   guard (`contains_internal_url`) skippable via workspace-scope alone.
   This directly broke moeka's own pre-existing
   `tests/integration/test_exec_real.py` contract (opt-in deny_patterns
   must work regardless of workspace mode) and is a real security-relevant
   regression, not just a test mismatch — silently disabling the SSRF
   guard under "full access" is a meaningfully larger attack surface than
   moeka's documented "destructive commands permitted by default" stance.
   Reverted to moeka's unconditional guard call (commit `7136de6d`).
   Upstream added two new tests asserting the opposite behavior
   (`test_exec_full_access_skips_command_guard`,
   `test_exec_full_workspace_scope_skips_command_guard`); adapted both to
   assert moeka's contract instead, renamed to
   `test_exec_full_access_still_enforces_command_guard` /
   `test_exec_full_workspace_scope_still_enforces_ssrf_guard`, with a
   comment explaining the deviation. **Flagged for human review** — this
   is a genuine design disagreement with upstream (not just a "moeka
   default" preservation), see "Needs human review" below.

### Test-fixture staleness (not production bugs, but ~50 broken assertions)
Two moeka-only test files that don't exist upstream at all
(`tests/agent/test_runner.py`, `tests/core/test_moeka_core.py`) predate
this batch's new `LLMUsage` dataclass (replacing ad-hoc `dict[str, int]`
usage tracking everywhere — `AgentHookContext.usage`,
`AgentRunResult.usage`, `LLMResponse.usage` are all `LLMUsage | None` now)
and `AgentLoop._run_agent_loop()`'s return type collapsing from a 5-tuple
to `AgentRunResult` directly. Fixed ~50
`LLMResponse(..., usage={...})`/`usage={}` construction sites to
`LLMUsage.reported(...)`/`None`, fixed `result.usage["prompt_tokens"]`-style
dict-indexing assertions to attribute access
(`result.usage.input_tokens`), and fixed 5 `_run_agent_loop()` call sites
to build `request_context=RequestContext(channel=..., chat_id=...)` and
read fields off the returned `AgentRunResult` instead of tuple-unpacking.
Deleted 2 tests and adapted 1 whose entire premise was the now-removed
`fail_on_tool_error` fatal-error path (see above) —
`test_runner_returns_structured_tool_error` and
`test_runner_tool_error_sets_final_content` deleted outright (no
equivalent behavior exists anymore);
`test_runner_tool_error_preserves_tool_results_in_messages` kept (the
underlying orphan-tool_calls invariant from #2943 is still real) but its
`stop_reason` assertion changed from `"tool_error"` to `"max_iterations"`
since the same scenario now retries instead of stopping fatally.

Also fixed a genuine test/fixture bug in the new upstream test
`tests/webui/test_gateway_webui_smoke.py::test_gateway_restart_restores_a_completed_answer_without_replaying_model`:
it seeded/read recovery-checkpoint data through a bare
`SessionManager(workspace, sessions_root=tmp_path / "sessions")`, which
defaults to `JsonlSessionStore` at a `sessions_root` that doesn't match
`default_sessions_root(workspace)` either — neither matches what the real
gateway subprocess (spawned via `_start_gateway`, using
`build_default_session_manager`) actually persists to
(`SqliteSessionStore` at `default_sessions_root(workspace)`), so the
seeded checkpoint was invisible to the second gateway process and
recovery silently never fired within the test's 20s deadline. Fixed by
wiring both the seed and the read-back through `SqliteSessionStore` at
`default_sessions_root(workspace)`, matching `gateway_runtime.py`. Fixed
in commit `d16b3608`, separate from the main fixup commit since it was
only found by re-running the full suite after the fail_on_tool_error/shell
fixes landed.

Also fixed `tests/tools/test_tool_descriptions.py::test_exec_tool_descriptions_are_concise`
(new upstream test asserting the shortened stub description) to check
moeka's verbose description's prefix instead, with a comment explaining
why.

### Deviation verification (post-merge, on the final commit)
- `allow_sudo: bool = False` — default unchanged.
- `rm -rf`/`dd`/`mkfs`/`shutdown` absent from `_DEFAULT_DENY_PATTERNS`
  (only the fork-bomb pattern remains there); `_INTERNAL_DENY_PATTERNS`
  untouched.
- `nanobot/session/manager.py`/`sqlite_store.py` both reference
  `sessions.db` (WAL) — confirmed (manager.py itself has 0 literal
  references, expected — it's pure upstream `SessionStore` Protocol code;
  the concrete filename lives entirely in moeka's `sqlite_store.py`, 16
  references there).
- `ChannelManager._dispatch_with_watchdog` present and wired.
- `bg_shell`'s `enabled(ctx)` still hardcoded `return False`.
- `nanobot channels enable/disable` CLI commands present in
  `nanobot/cli/commands.py` (untouched by this batch).
- Telegram `drop_pending_updates: bool = True` — confirmed.
- No `CONTRIBUTING.md`, no `images/nanobot_logo.png`.
- `.agent/*`, `CLAUDE.md`, `bin/*` launchers untouched by this batch.
- `git diff f9d449ef6c6d3d307c663890ae55d1eac4d088f8 -- nanobot/session/manager.py`
  — empty, confirmed (rule 1 success criterion).

### `ruff check nanobot/ tests/` — clean, no new lint (final commit `d16b3608`).

### Docker test results
Three full-suite runs this batch, tracking down the regressions above:
1. First run (merge commit only, before `uv.lock` regen): build failure —
   `pyproject.toml` gained `httpx[socks]`/`setproctitle`, dropped
   `websocket-client`; `uv sync --locked` rejected the stale lockfile.
   Fixed by `uv lock` (commit `cdc7a5ae`).
2. Second run (after `uv.lock` fix, before the fail_on_tool_error/shell
   fixes): **100 failed, 6620 passed, 33 skipped.** Read in full (not just
   skimmed) — this is what surfaced all three silent-breakage items above.
3. Third run (after commit `7136de6d`): 5 failed, 6713 passed, 33 skipped
   — 4 known baseline + the gateway-smoke test fixture bug (found and
   fixed in `d16b3608`).
4. **Fourth/final run (after `d16b3608`): 4 failed, 6714 passed, 33
   skipped.** Matches the expected baseline exactly (6546/4/33 → +168
   passed from this batch's new upstream coverage, same 4 pre-existing
   failures, 0 new failure categories):
   `tests/tools/test_tool_validation.py::test_exec_guard_allows_public_urls`
   ×2 (no DNS/network egress in this sandbox) and
   `tests/webui/test_mcp_presets_api.py::test_test_mcp_preset_*` ×2 (no
   `npx`/playwright in the test image).

### ⚠️ Needs human review
- **Shell command-guard scope (item 3 above) is a genuine, deliberate
  design disagreement with upstream, not a "moeka default" preservation.**
  Upstream's new position: granting a channel/session "full" workspace
  access is an explicit trust decision that should also bypass the
  command guard (deny_patterns, allow_patterns, and the hard SSRF/internal-URL
  guard) entirely. moeka kept the guard always-on, on the reasoning that
  workspace-escape trust and network/destructive-command trust are
  orthogonal axes, and the SSRF guard in particular protects against
  something (cloud metadata endpoint / internal service exfiltration)
  that "the user granted this session full filesystem access" says
  nothing about. This is *not* the same axis as CLAUDE.md's documented
  "destructive commands permitted by default" deviation — it's new
  territory upstream opened this batch. Worth an explicit decision (and
  possibly a CLAUDE.md addition either way) rather than silently carrying
  it forward every future batch as an undocumented merge-conflict
  resolution.
- **Confirming this pattern (silent breakage in clean auto-merges) has now
  repeated 3 batches running (batch 3: two instances; batch 4: three
  instances, one of which is a real security-relevant behavior change,
  not just a broken reference).** Worth considering, for future batches,
  whether the "read the full Docker failure list, not just a skim" step
  should be promoted from implicit diligence to an explicit required step
  in the process — this batch's regressions were *only* caught because
  the entire 100-line failure list was read line-by-line rather than
  grep'd for "known" patterns.
- moeka's cron "only persist when a job ran" fix (`558fe35e`, dropped in
  batch 3) remains unresolved — not touched this batch, no new upstream
  commits landed in `nanobot/cron/service.py` in this window beyond the
  new `remove_system_job` method (additive, no conflict with the
  batch-3 finding).

## Batch 5 — 2026-09-17

Merge commit `<pending — see commit that carries this same notes update>`
into `merge/upstream-main-2026-08-05`, checkpoint
`d81aa5a4ab420877e43a2a878c26f5635bbef743` (2026-09-02, "refactor(agent):
let runner own context compaction (#5568)"), 76 commits since batch 4's
`f9d449ef`. Worked in worktree `/home/muk/projects/moeka-sync`; verified
`/home/muk/projects/moeka` stayed on `main` throughout (before and after).
One fixup commit on top of the merge commit for a small stray-artifact
cleanup mid-batch (see "Incident" note below) — everything else landed in
the merge commit itself since all breakage was found and fixed before the
first commit.

### `nanobot/session/manager.py` (rule 1)
Auto-merged with **zero conflict markers** — the file was already
byte-identical to upstream going into this batch, and this batch's
upstream commits applied cleanly on top. `git diff d81aa5a4... --
nanobot/session/manager.py` is empty — confirmed both immediately after
the merge and again after all post-merge fixes landed.

`SessionStore` Protocol shape unchanged this batch (still `load`/`save`/
`delete`/`read`/`read_metadata`/`update_metadata`/`list_sessions`, same as
batches 3-4) — no new method for `sqlite_store.py` to implement. The
`Session` dataclass did change internally (`last_archived` became a
property wrapping the same underlying `last_consolidated` storage field,
replacing the old `enforce_file_cap`-era direct attribute — auto-merged
clean, `sqlite_store.py`'s own `last_consolidated` column/serialization
was untouched and needed no changes).

### Conflicts resolved (6 files)
- `nanobot/agent/context.py` (3 hunks) — kept moeka's hybrid recency+
  semantic-retrieval "# Recent History" feature (`_build_history_section`/
  `_without_duplicate_session_summary`, VecStore-backed) end-to-end:
  restored `include_memory_recent_history`/`session_key`/`unified_session`
  params on `build_system_prompt` (upstream removed them this batch — see
  "silent breakage" below, this is the conflict-adjacent half of that same
  story) and threaded them through the new `build_transcript`/
  `TranscriptInput` machinery upstream introduced for the compaction
  refactor (`build_transcript(transcript, *, ..., include_memory_recent_history=
  True, session_key=None, unified_session=False)`, passed through from
  `build_messages` too). memory category, moeka's shape is the identity;
  upstream's genuine improvement here (typed `SessionSummary`, deferred
  transcript assembly) was kept underneath.
- `nanobot/agent/loop.py` (3 hunks) — import-list merge (moeka's
  `SqliteSessionStore`/`default_sessions_root` alongside upstream's new
  `SUMMARY_CONTINUATION_TEXT`/`SessionSummaryCheckpoint`); kept moeka's
  `MyTool` manual registration in `_register_default_tools`; combined
  moeka's `_maybe_plan` planning feature with upstream's new
  `_insert_summary_checkpoint`/`_validated_checkpoint_boundary` compaction
  helpers (both wanted, unrelated features landing in the same spot).
- `nanobot/agent/memory.py` — one conflict: upstream's own "remove
  background token consolidation" change (part of this batch, per the
  merge commit's own squashed subject list) deleted moeka's
  `maybe_consolidate_by_tokens`/`consolidation_ratio`/
  `_MAX_CONSOLIDATION_ROUNDS`. Verified this method had **zero production
  callers already** pre-merge (only two defensive `AsyncMock` stubs in
  `tests/agent/test_runner.py`, guarding against something that no longer
  exists) — moeka's own idle-consolidation path has been `AutoCompact`/
  `compact_idle_session` since a previous batch, this was already dead
  code. Took upstream's removal (deleted the whole dead block); not a
  functional regression.
- `nanobot/agent/runner.py` (6 hunks) — the substantial one, see
  "tool-execution-boundary" below.
- `tests/cli/test_commands.py` — one hunk: kept moeka's `**_kwargs`-
  tolerant `_FakeSessionManager.__init__(self, _workspace, **_kwargs)`
  signature (matches `build_default_session_manager`'s call shape).
- `tests/cron/test_cron_service.py` — pure additive import-list merge
  (moeka's `CronRunRecord`, upstream's `RUNTIME_CONTEXT_INPUT_META`).

### `nanobot/agent/tools/execution.py` — new upstream module, not adopted in `runner.py`
This batch's `e73cce70` ("extract tool execution boundary") moved tool
dispatch out of `AgentRunner` into a standalone `execute_tool_calls()`
function in a new file, `nanobot/agent/tools/execution.py`. Per the
plugin-abstraction rule (rule 2), evaluated switching `runner.py`'s tool
dispatch over to it — but moeka's own `_execute_tools`/`_run_tool` methods
carry two features the extracted module doesn't have:
1. The **`AskUserInterrupt` 3-tuple contract** (`(result, event,
   fatal_error)` vs upstream's 2-tuple `(result, event)`) — moeka's
   `ask_user` tool depends on this to interrupt a turn cleanly (flagged as
   critical in batch 4's notes; re-verified explicitly this batch since
   the coordinator's brief called it out by name).
2. The **`exec_guard_denial`/`repeated_exec_guard_error` classification
   branch** in `_classify_violation` — a moeka-specific escalation path
   for deny/allowlist violations (distinct from the SSRF/workspace-
   violation branches upstream's `execution.py` does have) that has no
   upstream equivalent at all.

Kept `runner.py`'s own `_execute_tools`/`_run_tool`/`_classify_violation`
wholesale (not the new module), with an inline comment at the call site
explaining why. `nanobot/agent/tools/execution.py` itself was still
auto-merged in unmodified (unconflicted, brand new file) and is exercised
by its own dedicated tests (`tests/agent/test_runner_errors.py`,
`tests/agent/test_runner_tool_execution.py`,
`tests/agent/test_runner_safety.py`,
`tests/agent/tools/test_subagent_tools.py` — all call
`execute_tool_calls`/`is_ssrf_violation` directly, backend-agnostic to
which of the two implementations `AgentRunner` itself uses) — it's simply
unused-by-`runner.py` dead-from-the-runner's-perspective code, present
and tested on its own merits, available for a future batch to reconsider
once/if the `exec_guard_denial` and `AskUserInterrupt` gaps are closed
upstream-side.

One upstream bugfix in this same file **did** need porting by hand since
it only touched `execution.py`, not `runner.py`'s duplicate:
`919e3d34` "fix(agent): add retry hint to tool exceptions" — a generic
tool-raised `Exception` didn't get the "[Analyze the error above...]"
retry hint appended (only lookup-errors, prep-errors, and
`is_tool_error_result` payloads did; the plain-exception branch had a
comment reading "Preserve legacy exception payloads without the retry
hint," which was itself the bug upstream fixed). Ported the one-line fix
into `_run_tool`'s exception branch. Found via a real test failure
(`test_runner_returns_tool_exception_to_model_for_recovery`), not
proactively — a reminder that keeping a duplicate of extracted code means
manually watching for future upstream fixes to the original, batch after
batch.

### Silent breakage in cleanly-merged code — hunt results (this batch: four real regressions, all found by the full Docker run + local pytest follow-up)

Per the brief's specific instruction to trace the compaction-ownership
change against moeka's `AutoCompact`/`Consolidator` wiring: `AutoCompact`
(`nanobot/agent/autocompact.py`) had a single one-line, unconflicted
change this batch (`session.last_consolidated` → `session.last_archived`,
a pure rename following the `Session` dataclass's own internal refactor,
verified via direct diff against the batch-4 checkpoint) and its
`prepare_session`/`check_expired` call sites in `loop.py` are unconflicted
and unchanged in shape. **AutoCompact does not overlap with or get
bypassed by the runner-owned compaction added this batch** — they operate
at different layers: `AutoCompact` decides *when* to proactively archive
an idle session (unchanged, still `loop.py`-driven, still calls into
`Consolidator.compact_idle_session`); the new `ContextGovernor`/
`ModelRequestState`/`ContextCompactionState` machinery in
`context_governance.py` (947 new lines, no conflicts) decides how to fit
*this specific request's* messages into the model's context window
in-flight, inside `AgentRunner.run()`. No duplication or bypass found.

`git diff HEAD@{1} -- nanobot/core/` (pre-merge vs. merged): empty, same
as every batch since batch 3 — 0 upstream commits touch `nanobot/core` in
this window.

Four real regressions found (all from clean, non-conflicted auto-merges;
none had conflict markers):
1. **`nanobot/agent/context.py`'s "# Recent History" feature was silently
   deleted, not just its params.** The batch's `bb34b58f` ("make memory
   summaries cumulative") commit removed the whole recent-history-in-
   system-prompt block from upstream's `build_system_prompt` (upstream's
   own reasoning: with cumulative checkpoint summaries, the raw message
   list itself now already carries sufficient recent context, so a
   redundant "# Recent History" text block was pure duplication for
   upstream's stock feature set). But moeka's version of the same block is
   not just recency — it does genuine **semantic retrieval** via VecStore
   over history that has *already scrolled out* of the raw message list
   entirely (a materially different, still-valuable feature). The 3-way
   merge of the *signature* (moeka added `query`, upstream deleted
   `include_memory_recent_history`/`session_key`/`unified_session`)
   resolved fine on its own, but a **naive first-pass "keep HEAD" resolution
   of the conflict markers around the method *bodies*** accidentally
   dropped the entire `_build_history_section`/`_without_duplicate_session_summary`/
   `_behavioral_guidelines` method definitions and the `return` statement
   that ends `build_system_prompt` — self-inflicted during resolution, not
   an upstream artifact, but exactly the kind of "second-order" breakage
   the "read every failure, don't skim" policy is meant to catch (`ruff`
   didn't flag it because `self._behavioral_guidelines()` was still a
   valid-looking call, just to a name that no longer existed — a plain
   `python -m py_compile` *did* eventually flag the missing `def`s as an
   `AttributeError` at runtime, but only once exercised). Fully restored
   both methods and the `return` statement verbatim; also had to restore
   `nanobot/agent/memory.py`'s `read_recent_history_for_prompt`/
   `_is_internal_history_session`/`_INTERNAL_HISTORY_SESSION_KEYS`/
   `_INTERNAL_HISTORY_SESSION_PREFIXES` (also cleanly deleted by the same
   upstream commit, since `MemoryStore` is the layer `_build_history_section`
   reads from) and `nanobot/agent/context.py`'s `truncate_text_to_tokens`
   import + `_MAX_HISTORY_TOKENS` class constant (same fate). Verified via
   `python -c "import nanobot.agent.context"` and the full Docker run.
2. **`loop.py`'s `_run_agent_loop` calling convention changed from "a
   pre-built message list" to "a `TranscriptInput`", and moeka's own
   `_maybe_plan` planning feature + two moeka-authored tests still called
   it the old way.** Upstream's `_build_initial_messages` (built the full
   message list eagerly in `loop.py`) was replaced by
   `_build_transcript_input` (builds a `TranscriptInput` — raw parts only)
   plus a `transcript_builder = partial(self.context.build_transcript,
   channel=..., workspace=..., include_memory=...)` callback the runner
   invokes lazily, possibly more than once, for compaction. This dropped
   moeka's `include_memory_recent_history`/`session_key`/`unified_session`
   wiring from the call site entirely (not passed to the partial at all) —
   **a second, independent path to the same regression as #1**: without
   `session_key`, `_build_history_section`'s underlying
   `read_recent_history_for_prompt(session_key=None, ...)` call returns
   *every session's* history entries unfiltered, a real cross-session
   privacy leak for the semantic-history feature, not just a broken
   reference. Fixed by passing `include_memory_recent_history=not
   ephemeral`, `session_key=session.key`, `unified_session=
   self._unified_session` into the `transcript_builder` partial. Separately,
   moeka's `_maybe_plan(initial_messages: list)` (opt-in plan-then-execute
   step, appends a planning note as an extra message) had nothing to
   append to anymore — there is no eagerly-built message list in scope at
   its call site post-refactor. Changed its signature to
   `_maybe_plan(transcript_input: TranscriptInput)`, extracting the user
   text from `transcript_input.current_message` and appending the planning
   note to `transcript_input.history` instead (safe: `TranscriptInput.history`
   is a fresh per-turn list, a slice of `session.messages`, not a live
   reference — confirmed against `Session.get_history()` — so mutating it
   never leaks into persisted session history). This changes exactly where
   the planning note lands in the transcript (previously strictly after
   the current user message as a second same-role message; now
   immediately before it, in `history`) — a deliberate, documented,
   behavior-preserving-in-spirit adaptation to the new architecture, not a
   silent behavior change moeka chose to accept quietly. Two
   moeka-authored tests in `tests/agent/test_runner.py`
   (`test_loop_injected_followup_preserves_image_media`,
   `test_pending_queue_preserves_overflow_for_next_injection_cycle`) called
   `loop._run_agent_loop([{"role": "user", ...}], ...)` with a raw list
   directly (pre-dating this batch's refactor); updated both to build a
   `TranscriptInput(history=[], current_message="hello")` instead, matching
   the pattern already used by `tests/agent/test_runner_injections.py`'s
   (already-upstream-shaped) equivalents. Three tests in
   `tests/agent/test_thought_process.py` that called `loop._maybe_plan(...)`
   directly with a raw list were updated to construct/inspect a
   `TranscriptInput` instead.
3. **`nanobot/agent/runner.py`'s `_partition_tool_batches` method was
   deleted along with the parts of it that *did* need deleting.** The
   `AgentRunner._merge_message_content` static helper (used only by
   `_append_injected_messages`, which upstream itself simplified this
   batch from a complex runtime-context-aware merge down to a plain
   `messages.extend(injections)` — confirmed via `git blame`, both the
   complex version and its simplification are upstream-authored, not
   moeka's; no functional loss for moeka) was correctly gone with no
   dangling references. But `_partition_tool_batches` (batches tool calls
   for concurrent execution, still called from moeka's own kept
   `_execute_tools`, see above) also disappeared in the same clean merge —
   it moved into `nanobot/agent/tools/execution.py`'s private
   `_partition_tool_batches` as part of the same extraction, but
   `runner.py`'s own copy (which `_execute_tools` still needs, since
   `_execute_tools` was kept per the tool-execution-boundary decision
   above) was never restored automatically. Restored it verbatim after
   `_append_model_error_placeholder`. Found via `AttributeError` at first
   Docker run, confirmed by a scripted before/after method-name diff
   against the batch-4 checkpoint across `context.py`/`loop.py`/
   `memory.py`/`runner.py` (the same technique used to find #1's missing
   methods) — this technique is now worth keeping as a standard step for
   future batches wherever "keep moeka's side wholesale" is the
   resolution, since it catches exactly this class of "the kept region
   silently depended on something the discarded region used to provide."
4. **Two mock-response test helpers needed a new field.**
   `tests/agent/test_thought_process.py`'s `_response()` `SimpleNamespace`
   helper was missing `provider_compaction_applied` (new `LLMResponse`
   field this batch, read unconditionally by
   `context_governance.ProviderCompactionConsolidator`/`runner.py`'s
   `_record_request_usage`/compaction accounting at lines 1143/1376) —
   added with a `False` default, matching the real dataclass. Same
   category as batch 3's `generation_ms`/`ttft_ms` fix and batch 4's
   `LLMUsage` fixture staleness — a recurring pattern worth naming
   explicitly: **any batch that adds a new `LLMResponse`/`LLMUsage` field
   read unconditionally by production code will break every
   `SimpleNamespace`-based response mock in the suite that predates it**,
   and grepping for `SimpleNamespace(\s*content=` (or similar) across
   `tests/` before running the suite would likely catch these faster than
   waiting for the failure.

### New upstream test needing backend-specific adaptation
`tests/agent/test_dream.py::TestEphemeralDirect::
test_dream_turn_injects_memory_files_once_and_persists_session` — a
brand-new upstream test (doesn't exist in the batch-4 checkpoint at all)
asserting a dream turn persists its session even with `ephemeral=True`.
Its final assertion, `loop.sessions._get_session_path(session_key).exists()`,
is a `JsonlSessionStore`-specific helper (`SessionManager._get_session_path`
delegates to `self._jsonl_store.get_session_path()`, the *internal*,
always-constructed-but-otherwise-unused `JsonlSessionStore` instance every
`SessionManager` carries per the ADR-0001 conversion — not the store
`AgentLoop` actually persists through). Since moeka's `AgentLoop` defaults
to `SqliteSessionStore`, that jsonl path is never written to, so the
assertion failed even though the session *was* correctly persisted (to
`sessions.db`). Same "new upstream test assumes jsonl backing" pattern
documented in batches 1 and 3. Fixed by asserting through
`SessionManager.read_session_metadata()` (store-agnostic) instead, with a
comment explaining why.

### Deviation verification (post-merge, on the final commit)
- `allow_sudo: bool = False` — default unchanged (verified: 2 occurrences,
  both False; `nanobot/agent/tools/shell.py` untouched by any conflict
  this batch).
- `_DEFAULT_DENY_PATTERNS` still only the fork-bomb pattern; `rm -rf`/`dd`/
  `mkfs`/`format`/`shutdown` absent. `_INTERNAL_DENY_PATTERNS` untouched.
- The shell command-guard call (`self._guard_command(...)` in
  `_prepare_command`) is still unconditional — re-verified explicitly per
  the coordinator's brief (batch 4's fix, `# moeka: unlike upstream, the
  command guard always runs regardless of restrict_to_workspace`); this
  batch's upstream commits did not touch `nanobot/agent/tools/shell.py` at
  all (0 diff lines against the batch-4 checkpoint), so nothing to
  re-apply — confirmed the comment/guard call survived untouched.
- `grep -n "sessions.db" nanobot/session/manager.py nanobot/session/sqlite_store.py`
  — `manager.py` has 0 literal references (pure upstream `SessionStore`
  Protocol code, expected); `sqlite_store.py` has 16, unchanged shape.
- `ChannelManager._dispatch_with_watchdog` present and wired
  (`nanobot/channels/manager.py`, untouched by this batch).
- `bg_shell`'s `enabled(ctx)` still hardcoded `return False`.
- `nanobot channels enable/disable` CLI present in `nanobot/cli/commands.py`
  (untouched by this batch) — also runtime-exercised in Docker
  (`tests/cli/test_commands.py::test_channels_enable_*`/`test_channels_disable_*`,
  all passing; these need the `telegram` extra, which only Docker's
  `--all-extras` install provides — a local `.venv` run without that extra
  fails these with "Unknown channel: telegram", a false alarm matching the
  brief's warning about local-venv authority).
- Telegram `drop_pending_updates: bool = True` — confirmed
  (`nanobot/channels/telegram/runtime.py`, untouched by this batch).
- No `CONTRIBUTING.md`, no `images/nanobot_logo.png`;
  `images/GitHub_README.png` present.
- moeka `CLAUDE.md`, everything under `.agent/`, `bin/*` launchers — 0
  upstream commits touch any of these paths this batch (`git log
  f9d449ef..d81aa5a4... -- bin .agent CLAUDE.md images` — empty).
- `tools.exec.allowPatterns` whitelist-only semantics — re-exercised by
  `tests/tools/test_exec_allow_patterns.py`, all passing, no upstream
  changes to that file this batch.
- `git diff d81aa5a4ab420877e43a2a878c26f5635bbef743 -- nanobot/session/manager.py`
  — empty, confirmed (rule 1 success criterion), re-checked after all
  post-merge fixes landed too.

### `ruff check nanobot/ tests/` — clean, no new lint (final commit).

### Docker test results
Two full-suite runs:
1. First run (merge commit content, before the 4 silent-breakage fixes and
   the retry-hint port above): **15 failed, 6711 passed, 33 skipped.** 11
   of the 15 were the real regressions documented above (some tests
   surfaced more than one assertion failure per root cause); the other 4
   were the expected baseline. Read in full, not skimmed — this is what
   surfaced items 1-4 above (the two silent-breakage items in `context.py`/
   `loop.py` were *also* independently confirmed by direct code reading
   before the Docker run even finished, per the coordinator's explicit
   "hunt for silent breakage in cleanly-merged code" instruction — Docker
   then caught the two implementation slips in resolving them, plus items
   3 and 4 which reading alone hadn't caught).
2. Second run (after all fixes): **4 failed, 6722 passed, 33 skipped** —
   matches the 6714/4/33 baseline exactly on the known-failure side
   (+8 passed net from this batch's new upstream coverage after
   accounting for 3 removed batch-5 test files —
   `tests/agent/test_consolidate_offset.py`, `test_consolidation_ratio.py`,
   `test_session_retention.py`, all deleted by upstream's own "remove
   consolidation ratio"/"decouple archival" commits alongside the
   `maybe_consolidate_by_tokens` removal above), 0 new failure categories:
   `tests/tools/test_tool_validation.py::test_exec_guard_allows_public_urls`
   ×2 (no DNS/network egress in this sandbox) and
   `tests/webui/test_mcp_presets_api.py::test_test_mcp_preset_*` ×2 (no
   `npx`/playwright in the test image) — same two pre-existing environment
   artifacts as every prior batch.

### Incident — stray test-artifact leak during local verification, caught before commit
While running a local (non-Docker) `pytest tests/agent tests/session
tests/tools tests/cli tests/cron tests/webui` sweep to iterate faster on
the fixes above, one test constructed a `SqliteSessionStore` against an
unconfigured `MagicMock().parent` path, which resolved to a literal
`MagicMock/mock.parent.__truediv__()/<id>/<hash>/` directory tree under
the **repo root** (not `~/.nanobot` — a different, narrower leak than the
2026-09-17 incident documented earlier in this file) containing empty
`.workspace`/`sessions.db` files. Caught by `git status` before commit (a
`git add -A` briefly staged ~40 of these as new files), never committed.
Root-caused to a `git reset` (no args) I ran to recover from an unrelated
mistake (see below) accidentally clearing `.git/MERGE_HEAD`/`MERGE_MSG` —
recovered by writing the checkpoint sha back to
`.git/worktrees/moeka-sync/MERGE_HEAD` directly so `git commit` would
still produce a proper two-parent merge commit; unrelated to the
MagicMock leak itself, just flagging that a plain `git reset` mid-merge is
destructive to merge state even though it looks read-only. Deleted the
stray directory (`rm -rf MagicMock`) and re-verified `git status` was
clean of anything outside the intended merge diff before staging for
real.

### ⚠️ Needs human review
- **The tool-execution-boundary duplication (this batch's central design
  tension) is now two batches deep.** `runner.py` carries its own
  `_execute_tools`/`_run_tool`/`_classify_violation`, functionally
  overlapping upstream's extracted `nanobot/agent/tools/execution.py`
  module, diverging only in the `AskUserInterrupt` 3-tuple contract and
  the `exec_guard_denial` classification branch. This batch had to
  manually port one upstream bugfix (`919e3d34`) into the duplicate by
  hand after a test failure revealed it was missing; nothing guarantees
  the next batch's upstream fix to `execution.py` will announce itself
  the same way. Worth a real design decision: either (a) extend
  `execute_tool_calls()` upstream-side (i.e. in moeka's fork of
  `execution.py`) with an optional `AskUserInterrupt`-aware return mode
  and the `exec_guard_denial` branch, then switch `runner.py` over to call
  it and delete the duplicate, or (b) accept the duplication as a
  permanent moeka-specific layer and add a standing test that diffs the
  two implementations' shared logic (SSRF/workspace-violation handling,
  retry-hint behavior) to catch drift automatically instead of relying on
  incidental test failures.
- Same "silent breakage in clean auto-merges" pattern flagged in batches
  3-4 continues: 4 real regressions this batch (3-4 in prior batches
  each). The scripted before/after method-name diff (comparing `def `
  names between the pre-merge checkpoint and the merged file for every
  file `git merge` touched) used to find items #2's `_partition_tool_batches`
  and #1's missing `context.py`/`memory.py` methods this batch was
  effective enough that it's worth promoting from "improvised when
  something looked suspicious" to a standard, always-run step for every
  future batch, not just ones where a test happens to catch the gap.
- moeka's cron "only persist when a job ran" fix (`558fe35e`, dropped in
  batch 3) remains unresolved — no new upstream commits landed in
  `nanobot/cron/service.py` this batch beyond additive
  `RUNTIME_CONTEXT_INPUT_META` test coverage (unconflicted).

## Batch 6 — 2026-09-17

Merged `upstream/main` through `20f115bf4699bffcc786263cb999e7701986e179`
("feat(providers): send x-opencode-session header for OpenCode session
(#5662)", 2026-09-09, 87 commits) into `merge/upstream-main-2026-08-05`.

- Merge commit: `78a27292` (10 conflicted files, 288 non-conflicting files
  touched, 18604 insertions / 4131 deletions).
- Fixup commit: `a2bf2ec9` (repairs found by the full Docker test run —
  see below).

### Conflicts resolved (10 files)

- `Dockerfile`, `SECURITY.md` — kept deleted. Both were intentionally
  removed by moeka commits (`75220bfe` moved SECURITY.md content into
  `.agent/security.md`; `59e231b1` "new approach for uv" dropped the
  bespoke Dockerfile entirely in favor of the venv/uv-based `bin/`
  launchers). Upstream's modified versions were discarded.
- `tests/agent/test_runner_persistence.py` — restored upstream's version
  in the merge commit itself, since its prior deletion (`b1505b88`, an
  unrelated exec-guard-throttle commit) looked like accidental collateral
  damage, not a deliberate moeka removal. **This was wrong** — see the
  fixup section below; it got re-deleted.
- `tests/session/test_session_location.py` — kept deleted, consistent
  with batch 5's documented decision not to adopt upstream's ADR-0001
  out-of-workspace session relocation. Still no new upstream commits in
  this batch's range touched `sqlite_store.py` to reconsider that.
- `nanobot/agent/context.py` — kept moeka's semantic "Recent History"
  section wiring (`_build_history_section` / `_MAX_HISTORY_TOKENS`)
  intact; took upstream's `session_summary["text"] != "(nothing)"` guard
  and its dead-code cleanup of the unused `_RUNTIME_CONTEXT_TAG`/`_END`
  class attributes (verified zero other references repo-wide before
  dropping them).
- `nanobot/agent/runner.py` — kept moeka's `AskUserInterrupt`-aware
  `_execute_tools`/`_run_tool` fork (imports `nanobot.agent.tools.ask` +
  `is_tool_error_result`, not upstream's `execute_tool_calls` from
  `nanobot/agent/tools/execution.py`) and the inline `<think>`-tag
  reasoning-content copy; adopted upstream's `_request_model` ->
  `(response, usage)` tuple return plus the new `round_usages`/`events`
  plumbing on top.
- `nanobot/channels/manager.py` — kept both moeka's dispatcher watchdog
  (`_dispatch_with_watchdog`, `_dispatch_restart_count`) and upstream's
  new bounded outbound queue/cancel machinery
  (`_queue_outbound`/`_cancel_outbound`, `_outbound_slots`/`_outbound_sends`
  semaphores) — two independent additions to the same `__init__`/method
  region with zero actual logical overlap; both kept in full.
- `nanobot/utils/helpers.py` — took upstream's `maybe_persist_tool_result`
  simplification wholesale (`content: str` instead of `content: Any`,
  hardcoded `.txt` suffix, `logger.exception` instead of
  `logger.warning` on cleanup failure, new `max_chars`-aware
  `_render_tool_result_reference` that falls back to a short
  `"[truncated: {path}]"` form when the full reference would itself
  exceed the budget). The removed `content: Any` / JSON-list branch was
  itself plain upstream scaffolding from an earlier checkpoint (blame:
  `fbedf7ad`, not a moeka commit), and its only caller
  (`context_governance.py`'s `persist_text`) already pre-stringifies, so
  nothing moeka-specific was lost. This surfaced two stale test
  assertions — see fixups below.
- `tests/cli/test_commands.py` (7 identical occurrences) — took
  upstream's `message_bus=MessageBus` (the real class, already imported
  in the file, no-arg constructor) over moeka's `lambda: object()` stub
  (itself plain upstream scaffolding from an earlier checkpoint, not a
  moeka-authored difference); kept moeka's
  `session_manager=lambda _workspace, **_kwargs: ...` (accepts the
  `sessions_root=`/`store=` kwargs moeka's `SqliteSessionStore` wiring in
  `AgentLoop`/`AgentLoop.from_config` passes) over upstream's stricter
  single-arg lambda.
- `tests/utils/test_webui_workspaces.py` — dropped both sides' import
  line (`import json` / `from unittest.mock import MagicMock`); neither
  is referenced anywhere in the file post-merge. The rest of the file's
  body had already converged on moeka's SQLite-backed `SessionManager`
  rewrite of this test from an earlier batch (a live re-read instead of
  a `MagicMock(spec=SessionStore)` — moeka's SQLite store has no
  metadata cache to fake) with zero further conflict.

`nanobot/session/manager.py` taken wholesale from upstream, as every
prior batch: `git diff 20f115bf... -- nanobot/session/manager.py` is
empty post-merge (verified below too).

### Protocol-shape / sqlite_store.py

No changes. `nanobot/session/sqlite_store.py` and the `SessionStore`
Protocol definition in `manager.py` are both byte-identical to the
checkpoint-5 state this batch (`git diff --stat` across the batch-5..
batch-6 upstream range shows zero touches to either); nothing to port.

### Silent-breakage hunt (mandatory checks)

- `git diff` of the upstream batch-5..batch-6 range against
  `nanobot/core/`: empty — untouched this batch.
- `nanobot/agent/tools/execution.py`: untouched this batch (no upstream
  commits in range touch it) — nothing to port into `runner.py`'s kept
  duplicate `_execute_tools`/`_run_tool`. The two-batches-deep
  duplication-drift concern flagged at the end of batch 5's notes still
  stands and is unresolved (no new information this batch either way).
- `_build_history_section`/`read_recent_history_for_prompt`/
  `_behavioral_guidelines`/`_without_duplicate_session_summary` in
  `context.py`/`memory.py`: confirmed present, still wired end-to-end
  (`session_key`/`unified_session`/`include_memory_recent_history`
  threaded through `loop.py` -> `context.py` -> `memory.py` unchanged).
  This is the one place the hunt *did* find breakage this batch, but via
  a clean (non-conflicted) file, not context.py/memory.py themselves —
  see the `turn_delivery.py` fixup below.
- Shell command guard (`nanobot/agent/tools/shell.py`): confirmed still
  called unconditionally regardless of `restrict_to_workspace` (comment
  marker at line ~488 intact, guard call unconditional in the diff).
- Grepped `nanobot/agent/loop.py` and `nanobot/session/sqlite_store.py`
  call sites against `manager.py`'s surface: no shape mismatches: the two
  `SessionManager(...)` construction sites in `loop.py`
  (`__init__`/`from_config`) both still pass `sessions_root=`/`store=`
  matching `sqlite_store.py`'s unchanged `SessionStore` construction.

### Fixups (commit `a2bf2ec9`, found by the full Docker run, not by conflict markers)

The first full Docker run after the merge commit alone was **17 failed,
7115 passed, 70 skipped** against the 6722/4/33 baseline — 13 failures
beyond the 4 known pre-existing ones. All 13 traced to two mistakes made
during conflict resolution plus one genuinely new silent breakage in a
cleanly (non-conflict) auto-merged file:

1. **Restoring `tests/agent/test_runner_persistence.py` from upstream was
   itself the bug.** Its assertions assume upstream's nested
   `<workspace>/.nanobot/tool-results/...` path layout, but moeka's
   workspace *is* the flat `~/.nanobot` state home (no nested
   `.nanobot` — see CLAUDE.md's "Flat workspace layout" deviation), so
   `_TOOL_RESULTS_DIR` resolves to `<workspace>/tool-results` directly.
   Re-deleted the file. The coverage it would add already exists,
   moeka-adapted (flat paths), in `tests/agent/test_runner.py` from an
   earlier batch's test restructure (`99cc6ee8`) — confirmed by
   comparing the two files' near-identical test bodies.
2. Two of those already-flat-adapted tests in `test_runner.py`
   (`test_persist_tool_result_prunes_old_session_buckets`,
   `test_persist_tool_result_logs_cleanup_failures`) still asserted the
   pre-batch-6 `"[tool output persisted]"` message text and patched
   `logger.warning`; upstream's `maybe_persist_tool_result`
   simplification (taken wholesale per above) now emits the short
   `"[truncated: ...]"` form when the max_chars budget is smaller than
   the full reference message, and logs cleanup failures via
   `logger.exception`. Updated both assertions/patches.
3. `test_loop_stream_filter_handles_think_only_prefix_without_crashing`
   (`test_runner.py`) called `AgentLoop._run_agent_loop(on_stream=,
   on_stream_end=)` — the pre-refactor callback kwargs. Upstream's
   events-unification refactor (`192e2e90`, auto-merged clean with zero
   conflict markers since moeka hadn't touched that region) replaced
   those kwargs with `events=<EventSink>`. Ported the test to
   `events=output_events(on_stream=..., on_stream_end=...),
   streaming=True` using the `nanobot.utils.progress_events.output_events`
   adapter upstream added for exactly this legacy-callback case (used
   elsewhere already, e.g. `tests/agent/test_loop_progress.py`).
4. `tests/command/test_compact_command.py` (brand-new upstream test file
   this batch) —
   `test_checkpoint_continues_through_reloaded_session` compares the
   live turn's system prompt against a second, hand-recomputed
   `build_system_prompt()` call, but didn't pass `session_key`. Moeka's
   "# Recent History" section dedupes a history entry against
   `session_summary` only when `entry["session_key"] == session_key`; the
   live call passes `session_key=<key>` (matches, entry deduped/dropped)
   while the test's bare recompute call defaulted to `session_key=None`
   (doesn't match, entry kept) — so the two prompts differed by exactly
   the "# Recent History" block. This is upstream test code interacting
   with a moeka-only feature it doesn't know about; threaded
   `session_key=key` through the recompute call to make the comparison
   apples-to-apples.
5. **Real silent breakage, not a resolution mistake**: upstream's new
   `nanobot/channels/notification_routes.py` (a pure data-only helper,
   `notification_metadata()`) is imported at module level by
   `nanobot/agent/turn_delivery.py`, which is auto-merged clean (no
   conflict) and sits on `nanobot/agent/loop.py`'s always-imported chain.
   Because `nanobot/channels/__init__.py` eagerly imports `BaseChannel`
   (and therefore the full channel runtime, including
   `nanobot.pairing`), this broke moeka's embeddable
   `from nanobot.core import MoekaCore` promise of zero
   channel/gateway/pairing imports
   (`tests/core/test_import_boundary.py::test_core_import_has_no_runtime_deps`).
   Made the import local to `TurnRoute.remember_session_route()` (its one
   call site) instead of module-level; left a comment pointing at the
   test so a future batch doesn't reintroduce a module-level import of
   anything under `nanobot.channels`/`nanobot.pairing` from
   `nanobot/agent/`.
6. Two new upstream test files this batch
   (`tests/webui/test_notification_contract.py`) read
   `packages/client-events/fixtures.json` and `exec()` 
   `tui/scripts/package-release.py` as a module directly off disk —
   neither `packages/` nor `tui/` was previously `COPY`'d into
   `Dockerfile.test` (Python tests hadn't needed the TS/Bun source trees
   before). Added `COPY packages/ packages/` and `COPY tui/ tui/`. Both
   directories are small (16K / 796K) with no `node_modules` to exclude.

After the fixup commit, full Docker run: **7114 passed, 4 failed, 70
skipped**. The 4 failures are exactly the two known pre-existing
categories (2x `test_exec_guard_allows_public_urls` — no DNS/network
egress in the sandbox; 2x `test_mcp_presets_api.py` — no npx/playwright
in the test image), confirmed by name against this batch's own full
run. Net gain over the 6722/4/33 baseline (392 more passing, 37 more
skipped) tracks this batch's ~87 new upstream commits' worth of new
tests (many newly `skipif`'d: macOS Seatbelt native tests, Windows-only
tests, etc.).

### Unresolved judgment calls / carried-forward items

- The `runner.py` `_execute_tools`/`execution.py` duplication-drift
  concern (batch 5) is now three batches deep with no new information —
  still worth the real design decision flagged there.
- moeka's cron "only persist when a job ran" fix (`558fe35e`, dropped in
  batch 3) remains unresolved; no upstream commits in
  `nanobot/cron/service.py` this batch either.
- Item #5 above (`turn_delivery.py`'s module-level channels import) is
  the kind of thing the "scripted before/after method-name diff" idea
  from batch 5's notes would *not* have caught (no method added/removed,
  just a new module-level import statement) — worth extending that
  script (if it gets built) to also diff each touched file's top-level
  `import`/`from` lines against the pre-merge checkpoint for exactly this
  class of regression.

## Batch 7 — 2026-09-17 (final batch of the 7-batch plan)

Merge commit `96896461` into `merge/upstream-main-2026-08-05`, checkpoint
`2fb16593988b9e85131e02f395bb9a5108e220e7` (2026-09-16, "fix(tui): keep
input responsive during agent output (#5791)"), 86 commits since batch 6's
`20f115bf`. All conflict resolution, silent-breakage fixes, and
Docker-surfaced fixups landed in this single merge commit (nothing was
committed before every issue found by three successive full Docker runs
was fixed) — no separate fixup commit this batch. Worked in worktree
`/home/muk/projects/moeka-sync`; `/home/muk/projects/moeka` verified on
`main` both before and after.

This is the last batch of the original 7-batch plan (`.agent/upstream-sync-plan.md`).
`upstream/main`'s tip has moved past this checkpoint since the plan was cut
(current tip is past `2fb16593`) — expected, not chased.

### `nanobot/session/manager.py` (rule 1)
Auto-merged with **zero conflict markers** — the file was already
byte-identical to upstream going into this batch (confirmed against
batch 6's checkpoint too), and this batch's upstream commits to it (none
touched the file at all — `git diff --stat` across the batch-6..batch-7
upstream range shows zero touches) applied cleanly. `git diff
2fb16593988b9e85131e02f395bb9a5108e220e7 -- nanobot/session/manager.py` —
empty, confirmed both immediately after the merge commit and again on the
final commit. `nanobot/session/sqlite_store.py` likewise untouched by this
batch's upstream range (zero diff) — no Protocol-shape drift to reconcile.

### Conflicts resolved (9 files)
- `.gitignore` — pure additive merge (moeka's MagicMock-pollution guard +
  upstream's `.playwright-cli/`); kept both.
- `CONTRIBUTING.md` — modify/delete (upstream modified, moeka deleted);
  kept deleted (moeka policy, always-keep).
- `pyproject.toml` — one hunk: `[project]` name/version/description; kept
  moeka's (`moeka`, `0.2.1`, server-management description) over upstream's
  (`nanobot-ai`, `0.3.5`). Everything else in the file (deps,
  optional-dependencies) auto-merged clean.
- `tests/agent/test_runner_persistence.py` — modify/delete; kept deleted
  (moeka policy since batch 6 — its assertions assume upstream's nested
  `<workspace>/.nanobot/tool-results/...` layout, incompatible with
  moeka's flat-workspace deviation; the coverage already exists,
  flat-adapted, in `tests/agent/test_runner.py`).
- `webui/bun.lock` — regenerated via `bun install` (network available in
  this worktree) rather than hand-resolving the lockfile conflict markers;
  0 conflict markers remain, `webui/package.json` had already auto-merged
  clean with no conflicts of its own.
- `nanobot/agent/loop.py` (1 hunk) — `SubagentManager(...)` construction:
  kept moeka's `tools_allow`/`tools_deny`/`inline_skills` kwargs; dropped
  the `llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(...)`
  kwarg (see "llm_timeout_s removal" below — upstream's own feature,
  upstream's own removal this batch, not a moeka deviation).
- `nanobot/agent/subagent.py` (1 hunk) — same shape: kept moeka's
  `tools_allow`/`tools_deny`/`inline_skills` constructor params, dropped
  `llm_wall_timeout_for_session`.
- `nanobot/agent/runner.py` (2 hunks) — see "llm_timeout_s removal" and
  "tool-execution-boundary" below, the two substantial ones this batch.
- `nanobot/cron/service.py` (2 hunks) — see "cron timer/job-result" below.

### `llm_timeout_s` removal — upstream's own feature, upstream's own deprecation, not a moeka deviation
This batch's `73410e02` ("fix: stream internal model calls with idle
timeouts (#5730)") removed `AgentRunSpec.llm_timeout_s`,
`AgentRunner._resolve_llm_timeout_s`, and the `NANOBOT_LLM_TIMEOUT_S`
env-var-driven outer `asyncio.wait_for` wrapper entirely, replacing it with
per-stream-event idle timeouts inside `chat_stream_with_retry` itself
(`resolve_stream_idle_timeout_s()`, already the mechanism `NANOBOT_STREAM_IDLE_TIMEOUT_S`
used). This also removed `nanobot.session.goal_state.runner_wall_llm_timeout_s`
(a *previously-merged* upstream feature — commit `e804f2fd`, an earlier
batch's checkpoint, "align LLM wall timeout with sustained goals" — not
moeka-authored, confirmed via `git blame`/author metadata) and its
`SubagentManager(llm_wall_timeout_for_session=...)` plumbing. Verified via
`git diff` across the batch-6..batch-7 upstream range that `loop.py`,
`subagent.py`, `goal_state.py`, and `runner.py` all consistently drop this
cluster together — a clean, self-contained upstream removal, not something
moeka needed to preserve. Took upstream's removal wholesale in the
conflicting hunks; the non-conflicting parts of the same removal (import
lines, the `AgentRunSpec(llm_timeout_s=...)` construction site in
`loop.py`, `goal_state.py` itself) had already auto-merged clean. Also
deleted the now-upstream-removed `tests/agent/test_loop_goal_wall_timeout.py`
(upstream's own commit deleted it; not a moeka file) and its counterpart
assertions in `tests/session/test_goal_state.py` auto-merged clean to
upstream's trimmed version. `docs/configuration.md`'s `NANOBOT_LLM_TIMEOUT_S`
row already documents it as "Unused" post-removal — matches.

Separately, this same commit changed `AgentRunner._request_model`'s
non-streaming path (`wants_streaming=False`) from
`provider.chat_with_retry(...)` to `provider.chat_stream_with_retry(...)`
— providers now bound every stream event's wait uniformly regardless of
whether the caller wants incremental deltas. This wasn't part of any
conflict (auto-merged clean) but broke ~40 test mocks across
`tests/agent/test_runner.py`, `tests/agent/test_runner_safety.py`, and
`tests/core/test_moeka_core.py` that configured `provider.chat_with_retry`
without also configuring `provider.chat_stream_with_retry` on a bare
`MagicMock()` — see "Silent breakage" below.

### `nanobot/agent/tools/execution.py` — new upstream feature ported into runner.py's kept duplicate
This batch's execution.py picked up a genuine, well-tested upstream
improvement: read-result deduplication now checks not just whether a file
is unchanged on disk, but whether the *original read's result text* is
still actually present in the live model context (`file_read_context`/
`FileStates.is_unchanged(..., content_hash=)`/`record_read(..., result=)`
in `nanobot/agent/tools/file_state.py`, entirely rewritten this batch) —
guards against provider-native compaction silently dropping a tool result
from context while `record_read`'s in-memory cache still claims dedup is
safe, which would previously have let a stale `[File unchanged since last
read]` stub through even though the model could no longer see the original
content. Per rule 2 (moeka's `_execute_tools`/`_run_tool` fork is kept for
the `AskUserInterrupt` 3-tuple contract and `exec_guard_denial`
classification, still no upstream equivalent), ported this feature by hand
into the duplicate rather than switching over:
- `_execute_tools` gained `model_messages`/`compacted_tool_results` params
  (threaded from the call site's `messages_for_model`/
  `request_state.compacted_tool_results`, both already in scope) and the
  same `@cache`-memoized `read_results()` closure execution.py uses.
- `_run_tool` gained a `read_results: Callable[[], dict[str, str]] | None`
  param, threaded through both the concurrent (`asyncio.gather`) and serial
  call sites in `_execute_tools`.
- The actual tool-execute call in `_run_tool` is now wrapped in
  `file_read_context(tool_call.id, read_results) if tool_call.name ==
  "read_file" and read_results is not None else nullcontext()`, matching
  execution.py's own gating exactly (`tool_call.name == "read_file"`,
  confirmed against `ReadFileTool.name` in
  `nanobot/agent/tools/filesystem.py`).
- Required new imports in `runner.py`: `functools.cache`,
  `contextlib.nullcontext`, `nanobot.agent.tools.file_state.file_read_context`.

Verified via upstream's own new test file,
`tests/agent/test_runner_file_reads.py` (16 tests, all parametrized around
`AgentRunner().run(...)` — i.e. exercising the kept duplicate, not
`execute_tool_calls` directly) — all 16 pass unmodified against the port,
both locally and in Docker. `nanobot/agent/tools/filesystem.py`'s
`ReadFileTool._read()` itself needed no changes (auto-merged clean,
already calls `record_read(..., content_hash=..., result=result)`
matching the new signature).

The tool-execution-boundary duplication itself (flagged in batches 5-6) is
now three batches deep with a fourth manual port required — the design
tension (extend `execute_tool_calls()` upstream-side with an
`AskUserInterrupt`-aware mode and delete the duplicate, vs. accept the
duplication permanently and add a drift-detecting test) remains
unresolved, carried forward again.

### Cron timer/job-result (2 hunks in `nanobot/cron/service.py`)
Both hunks pit a moeka-authored fix already present pre-merge (not from a
prior batch's upstream — `git blame` traces both to moeka's own
post-batch-6 commits on this branch) against a genuinely new,
independently-motivated upstream simplification this batch:
- `_arm_timer()`: moeka's pre-existing fix only guards against
  self-cancellation (`self._timer_task is not current` before calling
  `.cancel()`) — protects the case where `_on_timer` itself (running
  inside the timer task) triggers a re-arm via a job's own agent turn
  mutating the store. Upstream's new guard (`if self._active_executions:
  return`) is broader — it skips rearming entirely (no cancel, no
  reschedule attempt) for the *whole duration* any job is executing,
  which also covers a case moeka's guard doesn't: `_arm_timer()` called
  from a genuinely different task (e.g. a WebUI job edit landing while a
  job is mid-execution) would, under moeka's guard alone, still cancel and
  reschedule the actively-running timer task, since `current !=
  self._timer_task` from that other task's perspective. Combined both:
  upstream's `_active_executions` early-return runs first (handles the
  cross-task case), moeka's never-cancel-self check kept underneath as
  defense-in-depth for whatever edge case isn't fully covered by
  `_active_executions` bookkeeping alone.
- `_execute_job()`: moeka's pre-existing fix bounds `self.on_job(job)` in
  `asyncio.wait_for(..., timeout=self.job_timeout_s)` (the timer task is
  the only scheduler and awaits jobs inline, so an unbounded job blocks
  every other job forever) but didn't capture the return value. Upstream's
  side captures `result = await self.on_job(job)` (needed for
  `CronRunRecord.run_id`) but has no timeout bound. Combined:
  `result = await asyncio.wait_for(self.on_job(job), timeout=self.job_timeout_s)` —
  both the bound and the capture.

### Silent breakage in cleanly-merged code — hunt results
- `git diff HEAD@{1} -- nanobot/core/` (pre-merge vs. merged): **empty** —
  0 upstream commits touch `nanobot/core` this batch, same as every batch
  since batch 3.
- `nanobot/agent/context.py`: **empty diff** against pre-merge state — 0
  upstream commits in this batch's range touch the file at all (confirmed
  via `git diff --stat` on the raw upstream range too); the semantic
  "# Recent History" feature (`_build_history_section`/
  `_without_duplicate_session_summary`/`_MAX_HISTORY_TOKENS`/`session_key`/
  `unified_session`/`include_memory_recent_history` threaded through
  `loop.py` -> `context.py` -> `memory.py`) reconfirmed present and wired
  end-to-end by direct grep, unchanged from batch 6.
- `nanobot/agent/memory.py`: clean auto-merge (86 lines changed, not a
  conflict) — entirely upstream's own Dream-archiver retry-on-erroneous-
  tool-call improvement (`MemoryArchiver`'s new `ProviderConversationStateController`-
  based retry loop when the model calls a tool during archival instead of
  returning the checkpoint text) plus a `chat_with_retry` ->
  `chat_stream_with_retry` swap matching the runner.py change above.
  Verified moeka's SQLite-specific `prune_dream_sessions`,
  `read_recent_history_for_prompt`, `_is_internal_history_session`,
  `_INTERNAL_HISTORY_SESSION_KEYS`/`_PREFIXES` all still present and
  untouched by this diff.
- Shell command guard (`nanobot/agent/tools/shell.py`): **empty diff**
  against pre-merge state — 0 upstream commits touch the file this batch.
  The unconditional-guard comment/call (batch 4's fix) survived untouched,
  reverified at line ~488.
- `nanobot/agent/tools/registry.py`, `nanobot/agent/tools/bg_shell.py`,
  `nanobot/session/sqlite_store.py`, `nanobot/session/manager.py`: all
  **empty diff** against pre-merge state — 0 upstream commits touch any of
  them this batch.
- `nanobot/agent/turn_delivery.py`: **empty diff** — the batch-6 fix
  (lazy, function-scoped `from nanobot.channels.notification_routes import
  notification_metadata` inside `TurnRoute.remember_session_route()`,
  keeping `nanobot.channels`/`nanobot.pairing` off `nanobot/agent/loop.py`'s
  transitive import chain) untouched. Grepped every file
  `nanobot/agent/loop.py` transitively imports for new top-level
  `nanobot.channels`/`nanobot.pairing`/`nanobot.gateway`/`nanobot.cli`
  imports this batch — none found; `tests/core/test_import_boundary.py::
  test_core_import_has_no_runtime_deps` passes in Docker (also passed
  locally once `rapidfuzz` was installed via `uv run`, confirming the
  earlier bare-`.venv` `ModuleNotFoundError` was a local-venv artifact, not
  a boundary leak).
- `nanobot/agent/loop.py` and `nanobot/session/sqlite_store.py` call sites
  against `manager.py`'s surface: `SessionManager(...)` construction in
  both `AgentLoop.__init__`/`AgentLoop.from_config` still pass
  `sessions_root=`/`store=`, matching `sqlite_store.py`'s unchanged
  `SessionStore` construction — no shape drift.
- No new `LLMResponse`/`LLMUsage`/`AgentRunResult` fields this batch
  (`git diff` on `nanobot/providers/base.py` across the upstream range
  shows only the `asyncio.wait_for(..., timeout=resolve_stream_idle_timeout_s())`
  wrapping change, no new dataclass fields) — the batch-3/4/5 "stale
  `SimpleNamespace` mock" failure class didn't recur this batch.
- Scripted before/after `def `-name diff (the batch-5 technique) run
  across every file this merge touched (conflicted or not): no methods
  silently disappeared from the kept side of any resolution this batch,
  beyond the ones intentionally removed above (`_resolve_llm_timeout_s`,
  `runner_wall_llm_timeout_s`).

### Docker-surfaced fixups (folded into the merge commit, three full runs)
1. **First full run** (merge commit content only, before any fixups):
   **6 failed** (2 new categories beyond the 4-failure baseline):
   - `tests/channels/test_channel_setup.py::test_every_runtime_channel_field_has_a_webui_contract`
     — new upstream test this batch, iterates `discover_plugins()` and
     calls `plugin.load_channel_class()` for *every* channel, including
     Matrix, whose `runtime.py` unconditionally imports `matrix-nio`/
     `mistune`/`nh3`/`aiohttp` and raises `ImportError` if missing.
     `Dockerfile.test` builds via `uv sync --all-extras`, but Matrix's
     deps aren't a pyproject extra at all — they're declared in
     `nanobot/channels/matrix/manifest.py`'s `ChannelPlugin.dependencies`
     tuple, installed on demand via `nanobot plugins enable matrix` /
     `scripts/install_channel_dependencies.py` (added upstream at an
     earlier checkpoint, `8423cf3e`). `.github/workflows/ci.yml` already
     runs `python -m scripts.install_channel_dependencies --all-channels`
     as its own CI step ("Install channel dependencies") — `Dockerfile.test`
     never wired this in. Fixed by adding the equivalent `RUN` step after
     the source-layer `uv sync`.
   - `tests/webui/test_settings_runtime.py::test_every_exposed_runtime_setting_has_a_frontend_use`
     — new upstream test this batch, reads
     `webui/src/components/settings/system/runtime-config-fields.ts`
     directly off disk to cross-check every backend runtime setting has a
     frontend editor field; `Dockerfile.test`/`.dockerignore` only ship
     one unrelated `webui/src/tests/fixtures/...` file (Python tests don't
     normally need the frontend source tree). Fixed by adding a targeted
     `COPY`/`.dockerignore` negation for this one file, same pattern as
     the existing fixture exception.
2. **Second full run** (after wiring in `install_channel_dependencies`,
   before the `tests/__init__.py`-collision investigation): the container
   failed at collection entirely —
   `ModuleNotFoundError: No module named 'tests._home_guard'` from
   `conftest.py`'s own real-$HOME-guard import. Root-caused to
   `linkpreview` (a transitive dependency of one of the newly-installed
   channels) shipping a **top-level `tests/` package** in its sdist (a
   packaging bug upstream of moeka — missing
   `packages=find_packages(exclude=["tests"])`). moeka's own `tests/` has
   no `__init__.py` (a namespace package, by design — many files under
   `tests/agent/*` rely on bare `from agent.x import y` imports that only
   resolve because pytest's conftest-driven `sys.path` insertion puts
   `tests/` itself on the path, not `/app`; adding `tests/__init__.py`
   would flip that insertion point to `/app` and break every such import
   across the suite). Per Python's PEP 420 namespace-package resolution, a
   **regular** package (site-packages' `tests/__init__.py`, from
   `linkpreview`) found anywhere on `sys.path` wins outright over a
   namespace portion found earlier, silently shadowing moeka's own
   `tests/` entirely. Considered and rejected adding `tests/__init__.py`
   (correct in isolation, but would have broken the bare-import pattern
   used throughout the suite) in favor of the lower-risk fix: strip the
   stray site-packages `tests/` directory as a one-line `rm -rf` appended
   to the same `RUN` step that installs the channel dependencies. Verified
   no other newly-installed dependency ships a top-level package
   colliding with moeka's own `agent`/`tools`/`cli`/`channels`/`core`/
   `session`/`providers` test-helper module names (scripted check across
   every installed distribution's `RECORD`).
3. **Third full run** (after the `tests/` collision fix, before the
   `INTERNAL_CHANNEL_FIELDS["telegram"]` fix): **13 failed** — the
   WebUI-contract test now actually exercised every channel's
   `load_channel_class()` (Matrix included, no more ImportError) but
   failed differently: `AssertionError: telegram runtime fields missing
   from WebUI contract: ['dropPendingUpdates']` — moeka's own
   `drop_pending_updates` deviation (`TelegramConfig.drop_pending_updates`,
   defaults `True`) has no WebUI setup-dialog field, correctly so (it's a
   fixed operational default, not meant to be user-configurable), but this
   new upstream test doesn't know that. Fixed by adding
   `"telegram": {"dropPendingUpdates"}` to `INTERNAL_CHANNEL_FIELDS`,
   matching the existing pattern for feishu/signal/weixin/whatsapp/
   websocket's own internal-only fields. Also surfaced, as a side effect
   of `dingtalk-stream` now actually being installed for the first time
   in this Docker image (previously the whole
   `nanobot/channels/dingtalk/tests/test_dingtalk_channel.py` file was
   skipped via `importorskip` — confirmed via the *first* Docker run's
   log, which shows exactly one `SKIPPED ... DingTalk dependencies not
   installed`), **8 pre-existing test failures unrelated to anything this
   batch touched** (the file has a literal zero-line diff against the
   batch-6 checkpoint) — see "Needs human review" below, not fixed here.
4. **Fourth/final run** (after the `INTERNAL_CHANNEL_FIELDS` fix): **12
   failed, 7957 passed, 56 skipped.** 4 are the long-standing known
   baseline (`test_exec_guard_allows_public_urls` x2, no DNS/network
   egress in this sandbox; `test_mcp_presets_api.py::test_test_mcp_preset_*`
   x2, no npx/playwright in the test image). The other 8 are the
   newly-surfaced (not newly-broken) dingtalk SSRF-redirect tests — see
   below. Net vs. the batch-6 baseline (7114/4/70): +843 passed (this
   batch's ~86 new upstream commits' worth of tests, plus dingtalk/matrix/
   other previously-`importorskip`-skipped channel test files now
   actually running now that their deps install), +8 failed (all traced
   to the pre-existing DNS-egress limitation, see below), -14 skipped
   (mostly the dingtalk file no longer skipping as a whole, offset by some
   new `skipif`s elsewhere).

### `ruff check nanobot/ tests/` — clean, no new lint.

### Deviation verification (post-merge, on the final commit)
- `allow_sudo: bool = False` — 2 occurrences, both False, `shell.py`
  untouched by this batch (0 diff).
- `_DEFAULT_DENY_PATTERNS` still only the fork-bomb pattern; `rm -rf`/`dd`/
  `mkfs`/`format`/`shutdown` absent. `_INTERNAL_DENY_PATTERNS` untouched.
- Shell command guard unconditional (`# moeka: unlike upstream, the
  command guard always runs regardless of restrict_to_workspace` comment
  at line ~488 intact; `shell.py` had 0 diff this batch, nothing to
  re-apply).
- `sessions.db` referenced 16x in `sqlite_store.py` (0 in `manager.py`,
  expected — pure upstream `SessionStore` Protocol code).
- `ChannelManager._dispatch_with_watchdog` present and wired
  (`nanobot/channels/manager.py`, untouched by this batch's upstream
  range).
- `bg_shell`'s `enabled(ctx)` still hardcoded `return False`.
- `nanobot channels enable/disable` CLI present in `nanobot/cli/commands.py`.
- Telegram `drop_pending_updates: bool = True` — confirmed, and (per the
  fixup above) now also correctly excluded from the WebUI setup contract
  as an internal-only field.
- No `CONTRIBUTING.md`, no `images/nanobot_logo.png`, no `SECURITY.md`, no
  root `Dockerfile` (all absent, matching batch 6's confirmation);
  `images/GitHub_README.png` present.
- `.agent/*`, `CLAUDE.md`, `bin/*` launchers — untouched by this batch (no
  upstream commits in range touch any of these paths).
- `tools.exec.allowPatterns` whitelist-only semantics — untouched, no
  upstream changes to `nanobot/agent/tools/shell.py` this batch.
- Lazy `Config`/`ToolsConfig` `model_rebuild` — untouched (0 diff on
  `nanobot/config/schema.py`/`loader.py` this batch).
- `git diff 2fb16593988b9e85131e02f395bb9a5108e220e7 -- nanobot/session/manager.py`
  — empty, confirmed (rule 1 success criterion), both right after the
  merge and again on the final commit.

### Final `nanobot/core/` byte-identical check (closing verification, per the sync plan)
`git diff ea51bdd0 -- nanobot/core/` (moeka `main`'s tip immediately before
batch 1 started, i.e. `8ba7a3db`'s first parent) against this batch's final
commit: **not byte-identical** — one real, already-documented diff in
`nanobot/core/core.py`:
```
+from nanobot.agent.tools.registry import ToolRegistry
...
-loop = AgentLoop.from_config(config, **extra)
+loop = AgentLoop.from_config(config, tool_registry=ToolRegistry(), **extra)
```
This is **batch 2's own fix** (not a regression introduced by any later
batch, and not newly discovered here) — flagged in batch 2's notes as the
"`AgentLoop.from_config()` incident": upstream's `from_config()` classmethod
started requiring an explicit `tool_registry` argument at some earlier
checkpoint, and `MoekaCore.create()` needed a one-line compatibility fix to
keep building a default `ToolRegistry()` explicitly. This is a legitimate,
necessary, already-reviewed moeka-side adaptation to an upstream API
change, not scope creep — the plan's own closing-verification step
anticipated this kind of result ("if it's NOT byte-identical, that's not
necessarily wrong... report exactly what changed and why"). No other diff
found; the rest of `nanobot/core/` (11 other files) is untouched across
all 7 batches.

### ⚠️ Needs human review
- **8 newly-surfaced `nanobot/channels/dingtalk/tests/test_dingtalk_channel.py`
  failures, same root cause as the accepted DNS-egress baseline, not
  fixed here.** All 8 exercise `DingTalkChannel._read_media_bytes`/
  `_fetch_remote_media_bytes`/`_send_media_ref`'s SSRF-redirect guard
  (`nanobot.security.network.validate_url_target`, which performs a real
  DNS resolution via `socket.getaddrinfo` to check whether a URL's
  resolved IP is private) against `https://example.com/...`-style test
  URLs. Directly confirmed in the Docker image:
  `socket.getaddrinfo('example.com', 443)` raises
  `socket.gaierror: [Errno -2] Name or service not known` — this sandbox
  has no DNS resolution at all, the identical root cause already accepted
  for `test_exec_guard_allows_public_urls`'s 2 known failures. The
  dingtalk file itself has a **zero-line diff** against the batch-6
  checkpoint (confirmed via `git diff HEAD@{1}`) — nothing this batch (or
  any conflict resolution here) touched it. It was **previously entirely
  skipped** in every prior batch's Docker run (`SKIPPED ... DingTalk
  dependencies not installed (dingtalk-stream)`, confirmed in this
  batch's own first Docker run's log) because `dingtalk-stream` was never
  installed — the `install_channel_dependencies` fixup above is what
  first made this file's tests actually run in Docker at all, incidentally
  surfacing a pre-existing environment-limitation exposure that was always
  there, just never exercised. Not fixed here: giving the sandbox real DNS/
  network egress is a bigger, riskier infra change clearly out of this
  batch's scope, and mocking `socket.getaddrinfo` inside these specific
  tests isn't a batch-7 merge concern (the test file predates this batch
  entirely). Recommend either (a) accepting these as a permanent addition
  to the "known sandbox limitation" baseline (parallel to the existing 2x
  `test_exec_guard_allows_public_urls`), or (b) auditing whether any other
  channel's test file was *also* silently skipped in every prior batch for
  the same missing-dependency reason and would surface similar
  DNS-dependent failures once `install_channel_dependencies` runs for it
  too (a spot-check across this run's full pass count suggests no other
  channel hit this — feishu/discord/qq/slack/wecom/weixin/whatsapp/matrix
  all show clean full runs in the final Docker log — but this wasn't
  exhaustively verified test-by-test).
- **The tool-execution-boundary duplication (`runner.py`'s
  `_execute_tools`/`_run_tool` vs. `nanobot/agent/tools/execution.py`'s
  `execute_tool_calls`) is now four batches deep**, with this batch's
  manual port of the read-dedup feature being the largest single port yet
  (a new module-level closure, a new context-manager wrap, two new
  threaded-through parameters). The design tension flagged since batch 5
  (extend `execute_tool_calls()` with an `AskUserInterrupt`-aware mode and
  the `exec_guard_denial` branch, then delete the duplicate; or accept the
  duplication permanently and add an automated drift-detector) remains
  unresolved. This was the last batch of the current plan — worth a
  deliberate decision now rather than carrying it into whatever sync
  cadence follows.
- moeka's cron "only persist when a job ran" fix (`558fe35e`, dropped in
  batch 3) remains unresolved; no upstream commits in
  `nanobot/cron/service.py`'s conflicting hunks this batch addressed the
  same clobbering scenario (the two hunks resolved this batch are
  unrelated: timer-rearm guarding and job-timeout/result-capture).
- `docs/releasing.md` (new upstream file this batch, auto-merged in
  unconflicted) links to `../CONTRIBUTING.md#release-packaging-contract`,
  which doesn't exist on moeka (deleted). Cosmetic — no test references
  this file's content, and it's prose-only — but worth a follow-up doc
  fix (either point at moeka's own release process doc, if one exists, or
  drop the file) rather than leaving a dead link in the tree indefinitely.

### Status: all 7 batches of the original plan are now merged
`merge/upstream-main-2026-08-05` carries batches 1 through 7 of
`.agent/upstream-sync-plan.md`'s original 744-commit survey, closing the
gap between moeka's pre-sync `main` and `upstream/main` as of the
2026-09-16 checkpoint (`2fb16593`). `nanobot/session/manager.py` is
upstream-identical at every checkpoint along the way (rule 1, reverified
this batch). `nanobot/core/` carries exactly one deliberate, previously-
reviewed line of drift (batch 2's `ToolRegistry()` compatibility fix) and
is otherwise untouched across all 7 batches. This branch has not been
pushed and no PR has been opened — that's the owner's next step.
