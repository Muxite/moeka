# Deploy runbook: cutting `moeka.service` over to this branch

Written 2026-09-17, as part of the review-fixes pass on
`merge/upstream-main-2026-08-05` (see `.agent/review-fixes-report.md`).
**Read this before deploying this branch (or any descendant of it) to the
host currently running `moeka.service`.**

## Why this exists

This branch adopted ADR-0001: sessions now live *outside* the workspace, at
`<sessions_root>/<workspace-id>/sessions.db`, not at
`<workspace>/sessions.db`. On this host that resolves to:

- Legacy (current, live) location: `~/.nanobot/sessions.db` (1.8 MB, the
  real production history).
- New (ADR-0001) location: `~/.nanobot-sessions/fc4b6ee5fa04bad9e8978d18e7237043/sessions.db`.

That destination directory **already exists** (it holds a `.workspace`
identity marker, created by something that already constructed a
`SqliteSessionStore` against this host's real workspace) but **contains no
database yet**. `SqliteSessionStore.__init__` no longer auto-migrates a
legacy in-workspace database on its own — it only logs a warning
(`_warn_if_legacy_db_in_workspace`, in `nanobot/session/sqlite_store.py`).

**The consequence, stated plainly: if you start this branch's `moeka
gateway` (or `serve`) against this host's real config without first running
the migration below, the agent comes up with a brand-new, empty
`sessions.db` at the ADR-0001 location. All 1.8 MB / four sessions / 1,738
messages of real history at `~/.nanobot/sessions.db` are still sitting
there, untouched and intact, but the running agent will not see any of it.**
Nothing is deleted by this failure mode — the old file is simply not where
the new code looks — but from the user's perspective it is indistinguishable
from history loss until someone notices and runs the migration after the
fact. This is the single likeliest way to lose (apparent) history with this
branch, and it is a deploy-procedure gap, not a code defect — hence a
runbook instead of a further code change.

## Before you start: read `_is_sqlite_db_in_use`'s docstring

`nanobot/session/sqlite_store.py`'s `_is_sqlite_db_in_use` — the check
behind `sessions migrate`'s "source appears to be open by a running
process" refusal — **cannot detect an idle connection**. `moeka.service`
between turns holds `sessions.db` open in WAL mode with no active write
transaction; that state does not trip a `BEGIN IMMEDIATE` write-lock probe.
**The migration tool reporting "source not in use" is not proof the service
is safe to migrate around.** The only reliable precondition is stopping the
service first, which is why that is step 1 below, not a courtesy.

## Procedure

Run every step from a shell on the host, with this branch's code available
(e.g. via `uv run nanobot ...` from this worktree, or however this branch
is actually installed for the deploy). Do not skip the dry run. Do not
skip verification.

### 1. Stop the service

```bash
systemctl --user stop moeka.service
systemctl --user status moeka.service   # confirm it's actually stopped, not just "stopping"
```

This is not optional and the migration tool cannot verify it for you (see
above). Stopping first also means step 2's `--force` should never actually
be needed — if the in-use check still fires after you've confirmed the
service is stopped, treat that as a signal to investigate (a stray process
still has the file open?), not as a reason to reach for `--force`.

### 2. Dry run the migration

```bash
nanobot sessions migrate \
  --from ~/.nanobot/sessions.db \
  --to ~/.nanobot-sessions/fc4b6ee5fa04bad9e8978d18e7237043/sessions.db
```

(`--dry-run` is the default — this is a dry run without needing to say so
explicitly.) This prints the files it would copy (`db`, and `-wal`/`-shm`
if present), their sizes, whether the destination already exists (it
should *not* — the directory exists, but no `sessions.db` inside it yet),
and whether the source looks open. **Review this output before
continuing.** If the destination already contains a `sessions.db`, stop —
something already migrated this host, or you have the wrong path, and
`--no-dry-run` will simply refuse to overwrite it (see step 3) rather than
silently merging.

### 3. Perform the migration

```bash
nanobot sessions migrate \
  --from ~/.nanobot/sessions.db \
  --to ~/.nanobot-sessions/fc4b6ee5fa04bad9e8978d18e7237043/sessions.db \
  --no-dry-run --yes
```

Both `--no-dry-run` and `--yes` are required together — this is deliberate
two-key arming so a single fat-fingered flag can't trigger a real move
(see `nanobot/cli/commands.py`'s `sessions_migrate` docstring).

This copies the triplet to the destination, runs `PRAGMA integrity_check`
against the copy, and only then deletes the source files — the original at
`~/.nanobot/sessions.db` is never touched until an independently verified
copy exists at the destination (see `migrate_session_database`'s docstring
in `nanobot/session/sqlite_store.py` for the copy-then-delete rationale).
If it prints `[green]Migrated[/green] ...`, the destination is verified
good and the source has been removed. If it fails with an error instead,
**the source is guaranteed untouched** — read the error, fix whatever it
names (usually: destination already existed, or the in-use check fired),
and re-run from step 2.

### 4. Verify before starting the service

```bash
sqlite3 ~/.nanobot-sessions/fc4b6ee5fa04bad9e8978d18e7237043/sessions.db \
  "SELECT count(*) FROM sessions; SELECT count(*) FROM messages;"
```

Confirm the counts look right (this host: 4 sessions, 1,738 messages, as of
the 2026-09-17 incident writeup — check
`.agent/upstream-sync-notes.md` if that number needs re-confirming). Also
confirm the old file is gone (or, if source cleanup failed and was logged —
see `migrate_session_database`'s loud-logging behavior — that you've
reviewed and manually removed it once you're confident the new location is
in use):

```bash
ls -la ~/.nanobot/sessions.db   # should not exist after a successful migration
```

### 5. Start the service and confirm history loads

```bash
systemctl --user start moeka.service
systemctl --user status moeka.service
```

Send the agent a message on a channel with known prior history (e.g.
Telegram) and confirm it has context from before the migration — not just
that it responds. A running-but-context-free agent is exactly the silent
failure mode this runbook exists to prevent, and it will not show up as a
service failure.

## Rollback (thirty seconds, if step 5 shows no history)

The migrated database is still fully intact at the new location — nothing
in this rollback discards it. This just points the service back at the old
path so you can investigate without downtime pressure:

```bash
systemctl --user stop moeka.service
nanobot sessions migrate \
  --from ~/.nanobot-sessions/fc4b6ee5fa04bad9e8978d18e7237043/sessions.db \
  --to ~/.nanobot/sessions.db \
  --no-dry-run --yes
systemctl --user start moeka.service
```

This is the same tool, run in reverse — same copy-verify-delete guarantee,
same "source untouched until destination is verified" safety property.
After this, `~/.nanobot/sessions.db` is back, `moeka.service` is running
against it exactly as it was before this deploy, and you have a verified,
intact copy of the ADR-0001-location migration to compare against once you
figure out what step 5 actually revealed (most likely: a config/workspace
path mismatch causing the service to compute a different
`sessions_root`/workspace-id than expected — check `nanobot sessions
migrate`'s dry-run output for the *exact* paths it resolved, and compare
against what `moeka gateway`'s startup logs report for
`default_sessions_root()`, before assuming the data itself is the problem).
