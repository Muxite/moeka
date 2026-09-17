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
