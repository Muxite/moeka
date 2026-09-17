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
