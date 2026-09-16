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

## Next steps for batch 2
Base off `merge/upstream-main-2026-08-05` (this branch) or off a fresh
branch from its tip; merge checkpoint `0c684c5a99ff9cfa6f33b1a6172b5a189741f249`
(2026-08-12, 102 more commits). Re-check whether the `SessionStore`
Protocol split gets built on further upstream — if later commits start
consuming it elsewhere, the "defer" decision here should be revisited.
