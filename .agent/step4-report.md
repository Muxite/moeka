# Step 4 report — factory consolidation + SessionManager asymmetry audit

2026-09-17. Picks up from `.agent/upstream-sync-handover.md` §5 ("What
remains" — step 4, not started). Scope: exactly the three items listed
there. No batch-3 work, no `manager.py` restructuring.

## 1. Factory seam for the three eager `SqliteSessionStore(...)` sites

Added two factory functions to `nanobot/session/sqlite_store.py` (new file
from the earlier conversion, not the frozen `manager.py`):

- `build_sqlite_session_store(workspace=None, *, sessions_root=None)` —
  single construction seam for the store alone.
- `build_default_session_manager(workspace=None, *, sessions_root=None)` —
  builds the store and the `SessionManager` wrapper together, using one
  shared `_resolve_workspace_and_sessions_root()` helper so the two values
  can never drift. `workspace` falls back to `get_state_home()`
  (`MOEKA_WORKSPACE`/`MOEKA_STATE`/`NANOBOT_HOME` chain) when omitted;
  `sessions_root` falls back to `default_sessions_root()` computed from
  that same resolved workspace.

`build_default_session_manager` imports `SessionManager` **locally, inside
the function**, not at module load time. First pass imported it at the top
of `sqlite_store.py` and that broke
`tests/cli/test_commands.py::test_heartbeat_empty_response_still_retains_recent_messages`
(`KeyError: 'session_key'`, 6332 passed / 5 failed on the first Docker
run): the test suite's `_patch_cli_command_runtime` helper does
`monkeypatch.setattr("nanobot.session.manager.SessionManager", ...)`,
which only works if the call site re-reads that attribute at call time. A
module-level `from nanobot.session.manager import SessionManager` in
`sqlite_store.py` binds the real class once, before the monkeypatch runs,
and never sees the fake. Fixed by moving the import inside the function
(the `SessionManager` symbol is otherwise only needed for the return-type
annotation, covered by a `TYPE_CHECKING`-guarded import). Second Docker run
came back at the exact baseline (6333/4/31 — see §4).

Updated call sites:

- `nanobot/cli/commands.py:357` (`serve`) — now
  `session_manager = build_default_session_manager(runtime_config.workspace_path)`.
- `nanobot/cli/commands.py:468` (`sessions restore-workspace`) — now
  `store = build_sqlite_session_store(runtime_config.workspace_path)`.
- `nanobot/cli/gateway_runtime.py:390` (`_run_gateway`) — now
  `session_manager = build_default_session_manager(config.workspace_path)`.

All three files' now-unused `SqliteSessionStore`/`SessionManager`/
`default_sessions_root` imports were removed; `ruff check` passes on all
four touched files.

## 2. `nanobot/gateway/service.py` — `_working_directory()` / `_working_directory_text()`

Both fell back to bare `Path.home()` when `options.workspace` was unset,
skipping `get_state_home()`'s override chain. Fixed: both now import
`get_state_home` from `nanobot.config.paths` and call it instead of
`Path.home()`. (Left `GatewayServiceManager.home` at line 57 — a different,
unrelated use of `Path.home()` — untouched; it wasn't in scope.) No test
in `tests/gateway/test_gateway_service.py` or `tests/cli/test_gateway_commands.py`
asserted the old `Path.home()` fallback value directly, so this was a pure
behavior fix with no test fallout.

## 3. `SessionManager(` call-site audit

**Enumerated** (excluding `ExecSessionManager`, which is unrelated), all
production (`nanobot/`, non-test) call sites — grepped
`SessionManager(` across `nanobot/` and hand-checked each:

1. `nanobot/agent/loop.py:414` — inside `AgentLoop.__init__`, the
   `session_manager is None` fallback branch.
2. `nanobot/agent/loop.py:524` — inside `AgentLoop.from_config`, same
   fallback shape.
3. `nanobot/cli/gateway_runtime.py:390` (now routed through
   `build_default_session_manager`, see §1).
4. `nanobot/cli/commands.py:357` (now routed through
   `build_default_session_manager`, see §1).

**Count: 4.** (Test-only call sites — `nanobot/channels/websocket/tests/*`
— were excluded per the task's scope; they were also checked and found
consistent: each either passes no `sessions_root` to either side, or
passes the identical value to both.)

**Asymmetry check, each of the 4:**

- `loop.py:414` — `_sessions_root = default_sessions_root(workspace)` is
  computed once into a local, then passed as `sessions_root=_sessions_root`
  to *both* the injected `SqliteSessionStore(...)` and the `SessionManager(...)`
  call around it. Symmetric.
- `loop.py:524` — same shape, `sessions_root = default_sessions_root(config.workspace_path)`
  computed once, passed to both. Symmetric.
- `gateway_runtime.py:390` and `commands.py:357` — previously the same
  "compute once, pass to both" pattern (symmetric already), now replaced
  by `build_default_session_manager()`, whose whole point is to make that
  guarantee structural instead of a per-call-site discipline. Symmetric,
  and now provably so.

**Finding: no asymmetry exists at any of the 4 real call sites today.**
Every one already computed `sessions_root` into a single local variable
and passed that same value to both the store and the manager — the
asymmetry the handover flagged as a risk (§6, item 1) was a *shape* of bug
that could occur if a future call site got this wrong, not a live defect
in the 4 sites that exist right now. Per the task's instructions ("fix it
only if the fix is obvious and local, otherwise report and stop"): there
was nothing live to fix here. What I *did* do — routing the two CLI/gateway
sites through `build_default_session_manager()` — closes off the class of
bug structurally for those two, since the shared resolver makes it
impossible to pass mismatched values by construction. `loop.py`'s two
sites were left untouched (out of scope per the task's explicit "three
eager construction sites" list, and already symmetric, so no risk to
close there right now).

## 4. Docker numbers

First run (before the `SessionManager` import-timing fix in §1):
**6332 passed, 5 failed, 31 skipped** — the extra failure was
`test_heartbeat_empty_response_still_retains_recent_messages`, a
regression from my own change, diagnosed and fixed as described above.

Second run (current state, full `scripts/test-docker.sh`):

```
6333 passed, 4 failed, 31 skipped, ~171s
```

Matches the baseline exactly. The 4 failures are the same two known
environment-artifact categories documented since batch 1/2 (no DNS/network
egress in this sandbox; no `npx`/playwright in the test image) — unchanged,
not touched by this work.

## 5. Verification

```
git diff 0c684c5a99ff9cfa6f33b1a6172b5a189741f249 -- nanobot/session/manager.py
```

→ empty (confirmed after all edits, before this commit).

```
git -C /home/muk/projects/moeka branch --show-current
```

→ `main` (live checkout untouched).

`~/.nanobot` was only read (`ls`, `find -newer`) to sanity-check nothing
had moved during this session — no writes, no `find -newer` hits against
the handover doc's timestamp.

## Files touched

- `nanobot/session/sqlite_store.py` — two new factory functions
  (`build_sqlite_session_store`, `build_default_session_manager`) plus
  `_resolve_workspace_and_sessions_root`.
- `nanobot/cli/commands.py` — `serve` and `sessions restore-workspace`
  routed through the new factories.
- `nanobot/cli/gateway_runtime.py` — `_run_gateway` routed through
  `build_default_session_manager`.
- `nanobot/gateway/service.py` — `_working_directory()` /
  `_working_directory_text()` now use `get_state_home()`.
