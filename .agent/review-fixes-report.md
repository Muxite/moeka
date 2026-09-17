# Review-fixes report — 2026-09-17

Addresses the independent review's four findings on the session-store
conversion (step 4 was already verified clean; this is on top of it, same
branch, before batch 3 resumes). Findings 1 and 2 were the priority (they
protect the batch work itself); 3 and 4 are done too, on this branch, per
the reviewer's "can follow the batches if you prefer, but do it on this
branch" — doing them now rather than deferring.

## 1. Test-isolation guard — fixed the "half-decorative" bug, extended coverage

**The bug**: `tests/conftest.py:130` computed `live = (Path.home() /
".nanobot").resolve()` at *fixture* time — after the root `conftest.py`'s
`pytest_configure` had already redirected `$HOME` to an ephemeral
directory. So this guard's `live` value was always the *fake* home, never
the real one; it could never fire. Confirmed by reading both files side by
side before touching anything.

**Fix**: added `tests/_home_guard.py`, a plain (non-conftest) module that
computes `REAL_HOME`/`REAL_NANOBOT_HOME`/`REAL_NANOBOT_SESSIONS_HOME` once,
at first import. Because Python caches modules, and the root `conftest.py`
is always imported first (before `pytest_configure` runs, before
`tests/conftest.py` is imported during collection), the root conftest's
import of this module captures the real, pre-redirect values, and every
later importer — including `tests/conftest.py` — gets back that same
cached, real value instead of recomputing against the now-redirected
`$HOME`. `tests/conftest.py`'s `_guard_live_workspace` fixture now imports
`REAL_NANOBOT_HOME` from there instead of calling `Path.home()` itself.
Verified this actually fixes the bug by running
`tests/test_real_home_guard.py` (see below) both locally and in Docker.

I put this shared module under `tests/` rather than at the repo root
specifically because `Dockerfile.test` copies `tests/` wholesale (`COPY
tests/ tests/`, line 58) but copies the root `conftest.py` as a named
single file (`COPY conftest.py ./`, line 54) — a new root-level file would
need a matching `Dockerfile.test` edit or it would silently not exist in
the image, which is exactly the class of bug `b013eba7` (in the earlier
session-store-conversion work) already had to fix once for the root
conftest itself. Putting the new file under `tests/` avoids reintroducing
that failure mode and needed no `Dockerfile.test` change.

**Coverage gap the reviewer flagged**: the surviving guard only wrapped
directory *creation* (`Path.mkdir`/`os.makedirs`/`os.mkdir`). The incident
itself was a `shutil.move` of an existing file into an already-existing
real `~/.nanobot` — no `mkdir` call anywhere in that path, so the original
guard would never have caught the actual incident even if the `Path.home()`
bug hadn't existed. Extended `conftest.py`'s `_install_real_home_guards`
(renamed from `_install_real_home_mkdir_guard`) to also wrap `shutil.move`,
`os.rename`, `os.replace`, and `sqlite3.connect` — checking both source and
destination for the two-argument operations, and the database path for
`sqlite3.connect` (since SQLite creates the file itself on first connect,
another path that never calls `mkdir`).

**Proof**: `tests/test_real_home_guard.py` (new, 11 tests) exercises each
guarded operation directly against a hardcoded real-home path
(`tests/_home_guard.py`'s constants) and asserts it raises `AssertionError`
*before* touching the filesystem — plus one test confirming unrelated
`tmp_path` operations are unaffected (the guard is scoped, not a blanket
lock on all of `$HOME`). All 11 pass locally and in Docker.

## 2. Legacy-import ordering — made explicit, added the regression test

**The bug** (confirmed real, as the reviewer described): `manager.py`'s
`JsonlSessionStore.__init__` (upstream, frozen — this branch cannot touch
it, see the "manager.py diff must stay empty" constraint) unconditionally
runs its own `_migrate_from_workspace`, which globs the same
`<workspace>/sessions/*.jsonl` files `SqliteSessionStore._import_legacy_jsonl`
imports, and *deletes* each source after copying it elsewhere. Today this
is safe only because every production call site constructs the
`SqliteSessionStore` (whose constructor runs the import) *before*
`SessionManager` exists at all — a fact that was true only by virtue of
Python evaluating a keyword argument (`store=SqliteSessionStore(...)`)
before entering the callee's body, not written down or enforced anywhere.

**Fix**: didn't touch `manager.py` (can't) or remove the useful
auto-import-on-construction behavior of `SqliteSessionStore.__init__`
(many existing tests — e.g. `tests/agent/test_session_atomic.py`'s whole
`TestLegacyJsonlImport` class — depend on it). Instead:

- `build_default_session_manager()` (added in step 4, in
  `nanobot/session/sqlite_store.py`) already builds the store and then
  calls `SessionManager` as two separate, explicit statements — this *is*
  the "make it explicit" fix for the two CLI/gateway call sites, and it
  already existed going into this review.
- `nanobot/agent/loop.py`'s two internal fallback-construction sites (used
  when no `session_manager=` is injected) still use the historical inline
  `store=SqliteSessionStore(...)` pattern — **not** routed through the
  shared factory, because I tried that first and it broke the test suite's
  `patch("nanobot.agent.loop.SessionManager")` /
  `patch("nanobot.agent.loop.SqliteSessionStore")` mocking seam (used by
  at least 11 test files: `test_dream.py`, `test_task_cancel.py`,
  `test_hook_composite.py`, `test_runner.py`,
  `test_loop_runner_integration.py`, `test_runner_injections.py`,
  `tests/agent/conftest.py`, `test_unified_session.py`,
  `test_loop_background_task_logging.py`, `test_runner_governance.py`,
  `test_stop_preserves_context.py`, plus `tests/cli/test_restart_command.py`)
  — patching `nanobot.agent.loop.SessionManager` has no effect on a
  factory function in a different module that does its own
  `from nanobot.session.manager import SessionManager` internally. First
  Docker-adjacent local run caught this immediately (3 failures,
  `AttributeError: 'method' object has no attribute 'return_value'` —
  `loop.sessions` came back as a real `SessionManager`, not the test's
  mock). Reverted `loop.py` to the original imports and instead split the
  single nested-expression construction into two explicit local
  statements (store built into a named variable first, then passed to
  `SessionManager`), with a comment pointing at the ordering invariant.
  Same "explicit, not implicit" outcome, zero test-seam breakage — all
  1635 tests in `tests/agent` pass.
- Updated `_import_legacy_jsonl`'s docstring in `sqlite_store.py` to
  explain the ordering dependency precisely and point at
  `build_default_session_manager` and the new regression test.

**Regression test**: added
`tests/agent/test_session_atomic.py::TestLegacyJsonlImport::test_jsonl_survives_via_build_default_session_manager`
— writes a legacy `.jsonl`, builds a `SessionManager` via
`build_default_session_manager()` (the actual production call path), and
asserts the session's content and metadata load correctly and the source
file was renamed to `.jsonl.imported` (not silently dropped). This is a
real regression test: it would fail if a future refactor reordered
`build_default_session_manager`'s two statements, or if
`SessionManager.__init__` were changed (in a way this branch would
otherwise have to blindly accept from upstream) to construct its internal
`JsonlSessionStore` any earlier than it already does.

## 3. Hardened `sessions migrate`

**The bug**: the handover's claim that "destination integrity is verified
before the source is released" was false as stated.
`migrate_session_database` did three `shutil.move`s (source → destination,
i.e. the source was already gone from its original location at that
point), *then* ran `PRAGMA integrity_check`, with the rollback-on-failure
path wrapped in `with suppress(OSError)` — so a rollback that itself failed
would fail silently, and worse, the window between the move and the check
(or between a failed check and its own rollback move completing) was a
window where **no complete, verified copy of the data existed anywhere**.

**Fix**: rewrote `migrate_session_database` (in
`nanobot/session/sqlite_store.py`) to copy (`shutil.copy2`) → verify
(`PRAGMA integrity_check`) → delete source, instead of move → verify →
move-back. The original is never touched until an independent copy at the
destination is verified good. Every cleanup path (a partial copy after a
failed copy; a leftover copy after a failed integrity check; the source
file after a successful, verified migration) now logs via `logger.error`
on failure instead of swallowing it through `suppress(OSError)`. Updated
the function's docstring to explain why copy-then-delete is strictly safer
than the original move-then-roll-back, and updated `_is_sqlite_db_in_use`'s
docstring to state plainly that it cannot detect an *idle* open connection
— exactly `moeka.service`'s state between turns — so a "not in use" result
is not sufficient justification to skip stopping the service first. Also
threaded that same caveat into the CLI's `--force` refusal message.

**Tests**: `tests/session/test_session_db_migrate.py` grew from 10 to 13
tests. New: `test_migrate_never_touches_source_until_destination_is_verified`
(asserts the source file's inode + mtime are byte-for-byte unchanged after
a failed integrity check — proof this is copy-then-delete, not
move-then-restore), `test_migrate_logs_loudly_if_destination_cleanup_fails`
(monkeypatches `Path.unlink` to fail during rollback and asserts a loguru
`ERROR` record is emitted, not swallowed), and
`test_migrate_logs_loudly_if_source_cleanup_fails_after_success` (same
pattern for the post-success source-deletion step). All 13 pass.

## 4. Deploy runbook

Written to `.agent/deploy-runbook.md`. Covers, in order: why the gap
exists (this branch resolves sessions to
`~/.nanobot-sessions/fc4b6ee5fa04bad9e8978d18e7237043/`, which exists but
holds only a `.workspace` marker — confirmed by inspecting the real
directory read-only, no database — while the live 1.8 MB history is still
at `~/.nanobot/sessions.db`, confirmed via `ls -la`/`du -h`); the
`_is_sqlite_db_in_use` idle-process caveat, stated up front as the reason
step 1 is "stop the service" and not optional; the exact `nanobot sessions
migrate --from ~/.nanobot/sessions.db --to
~/.nanobot-sessions/fc4b6ee5fa04bad9e8978d18e7237043/sessions.db` dry-run
and `--no-dry-run --yes` commands; a verification step (row counts via
`sqlite3`, confirming the old file is gone); starting the service and
confirming actual conversation context loads, not just that the service
starts; and a thirty-second rollback using the same migration tool run in
reverse.

## Docker numbers

```
scripts/test-docker.sh
```

→ **6348 passed, 4 failed, 31 skipped**, ~172s. The 4 failures are the
same two known environment-artifact categories as the step-4 baseline (no
DNS/network egress in this sandbox; no `npx`/playwright in the test
image) — unchanged. The 15 additional passes over the 6333 baseline are
this session's new tests (11 in `tests/test_real_home_guard.py`, 1 in
`tests/agent/test_session_atomic.py`, 3 in
`tests/session/test_session_db_migrate.py`).

## Verification

```
git diff 0c684c5a99ff9cfa6f33b1a6172b5a189741f249 -- nanobot/session/manager.py
```

→ empty (confirmed after all edits, before this commit — findings 1-4
touched `conftest.py`, `tests/conftest.py`, `tests/_home_guard.py`,
`tests/test_real_home_guard.py`, `nanobot/agent/loop.py`,
`nanobot/session/sqlite_store.py`, `tests/agent/test_session_atomic.py`,
`tests/session/test_session_db_migrate.py`, and
`.agent/deploy-runbook.md` — never `manager.py`).

```
git -C /home/muk/projects/moeka branch --show-current
```

→ `main`, working tree clean (live checkout untouched throughout).

`~/.nanobot` and `~/.nanobot-sessions` were only read (`ls -la`, `du -h`)
to confirm the deploy runbook's exact paths and sizes — no writes.

## Files touched

- `conftest.py` — real-home guard extended to `shutil.move`/`os.rename`/
  `os.replace`/`sqlite3.connect`; now sources its real-home constants from
  `tests/_home_guard.py` instead of computing its own.
- `tests/_home_guard.py` (new) — shared real-`$HOME` constants, computed
  once before the redirect.
- `tests/conftest.py` — `_guard_live_workspace` now uses
  `tests._home_guard.REAL_NANOBOT_HOME` instead of a fixture-time
  `Path.home()` recompute.
- `tests/test_real_home_guard.py` (new) — 11 tests proving every guarded
  operation raises before touching the real filesystem.
- `nanobot/agent/loop.py` — both internal `SessionManager`-fallback sites
  split into two explicit statements (store, then manager), same pattern
  as `build_default_session_manager`, without routing through it (to
  preserve the `patch("nanobot.agent.loop.SessionManager")` test seam).
- `nanobot/session/sqlite_store.py` — `_import_legacy_jsonl` and
  `build_default_session_manager` docstrings updated; `migrate_session_database`
  rewritten to copy→verify→delete with loud rollback logging;
  `_is_sqlite_db_in_use` docstring documents the idle-connection blind
  spot; CLI `--force` refusal message updated to match.
- `tests/agent/test_session_atomic.py` — new regression test for the
  legacy-import ordering via `build_default_session_manager`.
- `tests/session/test_session_db_migrate.py` — 3 new tests for
  copy-then-delete semantics and loud rollback/cleanup logging.
- `.agent/deploy-runbook.md` (new) — the ordered cutover procedure.
