# Deploy runbook: cutting `moeka.service` over to this branch

Written 2026-09-17, as part of the review-fixes pass on
`merge/upstream-main-2026-08-05` (see `.agent/review-fixes-report.md`).
Rewritten 2026-09-18 after an independent review found the original
rollback section factually wrong (see `.agent/deploy-blockers-report.md`).
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

## Before you start: `nanobot` on your PATH is the `main` install, not this branch

`which nanobot` on this host resolves to `~/.local/bin/nanobot` — the
**`main`** install, which has no `sessions migrate` subcommand at all
(`nanobot sessions migrate` fails with "No such command 'sessions'" run
as-is). Every command below must instead invoke *this branch's own* entry
point explicitly, from this worktree:

```bash
cd /home/muk/projects/moeka-sync
./bin/moeka.sh exec sessions migrate --from ... --to ... [flags]
```

`bin/moeka.sh exec <ARGS...>` runs `<ARGS...>` through `$VENV_DIR/bin/nanobot`
(this worktree's own `.venv`, already installed) — do **not** prefix the
args with a second `nanobot`; `exec` already supplies it. Verify this
resolves before running anything real:

```bash
cd /home/muk/projects/moeka-sync && ./bin/moeka.sh exec sessions --help
# must list `migrate` and `restore-workspace` as subcommands
```

If that fails, `bin/moeka.sh install` first to (re)build this worktree's
`.venv`. Every `nanobot sessions migrate ...` command below is shorthand
for `./bin/moeka.sh exec sessions migrate ...` run from
`/home/muk/projects/moeka-sync` — substitute the real invocation, the
short form is just for readability.

## Procedure

Run every step from a shell on the host. Do not skip the dry run. Do not
skip verification.

### 1. Stop the service

```bash
systemctl --user stop moeka.service
systemctl --user status moeka.service   # confirm it's actually stopped, not just "stopping"
```

This is not optional and the migration tool cannot verify it for you (see
above). Stopping first also means step 3's `--force` should never actually
be needed — if the in-use check still fires after you've confirmed the
service is stopped, treat that as a signal to investigate (a stray process
still has the file open?), not as a reason to reach for `--force`.

### 2. Dry run the migration

```bash
cd /home/muk/projects/moeka-sync
./bin/moeka.sh exec sessions migrate \
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
`--no-dry-run` will simply refuse to overwrite it (see step 4) rather than
silently merging.

### 3. Optional but recommended: take an independent safety copy

The migration tool's own copy-then-delete guarantee (step 4) protects
against a *failed* migration, but once it succeeds it deletes the source —
there is no built-in "keep both copies" mode, and no automatic backup. If
you want a copy that survives regardless of what happens in step 4 (e.g.
to diff against later, or as a belt-and-suspenders in case this runbook
itself has a bug), take one now, with a plain, boring file copy — not the
migration tool, so its lifecycle is completely independent of it:

```bash
cp -a ~/.nanobot/sessions.db ~/sessions.db.pre-migration-backup-$(date +%Y%m%d)
# copy -wal/-shm too if they exist:
cp -a ~/.nanobot/sessions.db-wal ~/sessions.db-wal.pre-migration-backup-$(date +%Y%m%d) 2>/dev/null || true
cp -a ~/.nanobot/sessions.db-shm ~/sessions.db-shm.pre-migration-backup-$(date +%Y%m%d) 2>/dev/null || true
```

If you skip this step, that's fine — step 4 is still safe (source is never
touched until the destination is independently verified) — you just won't
have anything to fall back on except re-running the rollback procedure
below, which moves data rather than duplicating it.

### 4. Perform the migration

```bash
cd /home/muk/projects/moeka-sync
./bin/moeka.sh exec sessions migrate \
  --from ~/.nanobot/sessions.db \
  --to ~/.nanobot-sessions/fc4b6ee5fa04bad9e8978d18e7237043/sessions.db \
  --no-dry-run --yes
```

Both `--no-dry-run` and `--yes` are required together — this is deliberate
two-key arming so a single fat-fingered flag can't trigger a real move
(see `nanobot/cli/commands.py`'s `sessions_migrate` docstring).

This copies the triplet to the destination, runs `PRAGMA integrity_check`
against the copy, and **only then deletes the source files** — the
original at `~/.nanobot/sessions.db` is never touched until an
independently verified copy exists at the destination (see
`migrate_session_database`'s docstring in `nanobot/session/sqlite_store.py`
for the copy-then-delete rationale). **Be clear about what "success" means
here: it deletes the source.** If it prints `[green]Migrated[/green] ...`,
the destination is verified good and the source is gone — that is by
design, not a side effect, and it is why step 3's independent backup (if
you took one) lives outside this tool's control. If it fails with an error
instead, **the source is guaranteed untouched** — read the error, fix
whatever it names (usually: destination already existed, or the in-use
check fired), and re-run from step 2.

### 5. Set the sessions directory's permissions

The ADR-0001 workspace-identity subdirectory
(`~/.nanobot-sessions/fc4b6ee5fa04bad9e8978d18e7237043/`, the one that now
holds `sessions.db`) is **not** currently mode `0700` — verified via
`ls -ld` as part of the review that produced this runbook revision
(`drwxrwxr-x`, group+other readable). `SqliteSessionStore.__init__` only
`chmod 0700`s the parent `sessions_root`
(`~/.nanobot-sessions` itself, which is already correctly `0700`) — it
never tightens an existing workspace-id subdirectory it didn't just
create. Fix this explicitly, once, after the migration lands the database
there:

```bash
chmod 700 ~/.nanobot-sessions/fc4b6ee5fa04bad9e8978d18e7237043
ls -ld ~/.nanobot-sessions/fc4b6ee5fa04bad9e8978d18e7237043   # confirm drwx------
```

(This step is documentation only — nothing in this repository's automation
touches `~/.nanobot-sessions` on your behalf; the person running this
runbook must run it by hand.)

### 5b. Reinstall dependencies into the LIVE checkout's venv

**This step was missing from the first version of this runbook and the
2026-09-18 deploy crash-looped because of it.** The service runs
`/home/muk/projects/moeka/bin/moeka.sh run`, which uses *that checkout's*
`.venv` -- not this worktree's. Moving the code 770 commits forward brings
new dependencies (`rapidfuzz` was the one that bit) that the live venv does
not have, so the gateway dies at import with `ModuleNotFoundError` and
`Restart=on-failure` turns it into a crash loop.

**Do not trust `./bin/moeka.sh install` for this.** It printed
`install complete` while installing nothing (its own output said
`moeka : unknown`). Use uv directly and verify by importing:

```bash
cd /home/muk/projects/moeka
uv sync --locked
.venv/bin/python -c "import rapidfuzz, nanobot.cli.commands; print('imports OK')"
.venv/bin/nanobot sessions --help | grep migrate   # deployed code has the new CLI
```

Expect `uv sync` to *remove* stale packages as well as add new ones; a long
removal list means the venv had drifted, which is normal here.

**`uv sync --locked` also removes every channel's runtime dependencies**
(`python-telegram-bot`, `discord-py`, ...): they are not in `uv.lock` but
installed on demand from each channel's manifest by `nanobot plugins enable`.
Starting the service without them breaks those channels. Before step 7,
re-enable every channel the config enables (`.venv/bin/nanobot plugins list`
shows them) and check the imports; this bit the 2026-09-28 deploy:

```bash
.venv/bin/nanobot plugins enable telegram
.venv/bin/nanobot plugins enable discord
.venv/bin/python -c "import telegram, discord; print('channel deps OK')"
```

### 6. Verify before starting the service

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

### 7. Start the service and confirm history loads

```bash
systemctl --user start moeka.service
systemctl --user status moeka.service
```

Send the agent a message on a channel with known prior history (e.g.
Telegram) and confirm it has context from before the migration — not just
that it responds. A running-but-context-free agent is exactly the silent
failure mode this runbook exists to prevent, and it will not show up as a
service failure.

## Rollback (if step 7 shows no history)

**Read this whole section before running anything — the original version of
this runbook was wrong about what rollback actually does, in two ways that
matter:**

1. **The migrated database is *not* untouched at the new location once you
   roll back.** Rollback here means running the same migration tool again,
   in reverse — and that tool deletes its *source* once the destination is
   verified, same as the forward direction. Rolling back therefore deletes
   the ADR-0001 copy, leaving only the copy back at
   `~/.nanobot/sessions.db`. There is no free "verified copy to compare
   against" left over afterward **unless you took the independent backup
   in step 3** — if you did, it's still sitting wherever you put it,
   completely unaffected by anything below. If you didn't, there is
   exactly one copy of the data after rollback, same as before you ever
   started.
2. **Moving the database back is not enough on its own.** This branch's
   code only ever reads sessions from the ADR-0001 location
   (`nanobot/agent/loop.py`'s `build_default_session_manager` call site,
   `nanobot/session/sqlite_store.py`'s `default_sessions_root`) — never
   from `<workspace>/sessions.db` directly. If `moeka.service` is still
   running this branch's code after you move the database back to
   `~/.nanobot/sessions.db`, it will look in the (now empty again)
   ADR-0001 location and come up with **zero history a second time** —
   moving the file back accomplishes nothing by itself unless the code
   actually reading it also reverts. `moeka.service`'s `ExecStart` runs
   `/home/muk/projects/moeka/bin/moeka.sh run` — i.e. whatever branch
   `/home/muk/projects/moeka` (a separate checkout from this worktree) is
   on. Rollback must revert *that* checkout to `main`, not just move data.

Rollback, done correctly:

```bash
# 1. Stop the service first — same reasoning as the forward path's step 1.
systemctl --user stop moeka.service

# 2. Move the database back, using this branch's own migrate tool (this
#    worktree still has it regardless of what /home/muk/projects/moeka is
#    on). This DELETES the ADR-0001 copy once verified -- see point 1
#    above. If you took a step-3 backup, this is redundant with it, which
#    is fine.
cd /home/muk/projects/moeka-sync
./bin/moeka.sh exec sessions migrate \
  --from ~/.nanobot-sessions/fc4b6ee5fa04bad9e8978d18e7237043/sessions.db \
  --to ~/.nanobot/sessions.db \
  --no-dry-run --yes

# 3. Revert the LIVE checkout's code, not just the data. /home/muk/projects/moeka
#    is a separate checkout the service actually runs from -- confirm what
#    it's on, and if it was switched to this branch (or a merge of it) for
#    this deploy attempt, put it back on the pre-deploy main commit. This
#    step is intentionally not spelled out as a copy-pasteable command:
#    the exact mechanism (git checkout of a specific sha, a merge revert,
#    however the deploy actually moved that checkout off main) depends on
#    how the deploy itself was performed, which this runbook doesn't
#    control. Whatever that mechanism is, run it now, in reverse.
git -C /home/muk/projects/moeka branch --show-current   # sanity check before AND after this step

# 4. Only once /home/muk/projects/moeka is confirmed back on the pre-deploy
#    main commit:
systemctl --user start moeka.service
```

After this, `~/.nanobot/sessions.db` is back, and `moeka.service` is
running code that actually reads from that location again — the two
things have to happen together, or you're back to exactly the failure mode
this runbook exists to prevent. Once confirmed healthy, investigate what
step 7 actually revealed (most likely: a config/workspace path mismatch
causing the service to compute a different `sessions_root`/workspace-id
than expected — check `nanobot sessions migrate`'s dry-run output for the
*exact* paths it resolved, and compare against what `moeka gateway`'s
startup logs report for `default_sessions_root()`, before assuming the
data itself is the problem) using the step-3 backup (if you took one) or
the now-restored `~/.nanobot/sessions.db`, without deploy-day time
pressure.
