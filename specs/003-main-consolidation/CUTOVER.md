# Cutover runbook: consolidated branch becomes `main`

**Status**: prepared 2026-10-01, NOT executed. Nothing here has been run against the live checkout, the
service, `main`, `core-slim` or any remote. The owner runs it.

Candidate: branch `consolidate/new-main` (worktree `/home/muk/projects/moeka-newmain`), tip recorded in the
"Verified state" block. It contains both `main` (`54069bb2`) and `core-slim` (`c382d0c9`) as ancestors, so
`main` fast-forwards to it: no rename, no force-push, no rewritten commit, and `core-slim` stays at its pin.

## Verified state (before cutover)

- Full suite via `scripts/test-docker.sh` at `25e66fa7` (after merging `review/usage`, `review/arch`,
  `rsi-harness-spec`, `feat/setup-harness`): 9699 passed, 0 failed, 59 skipped (6 min 44 s, image built from
  this tree). Earlier, at `bc86a31f` before those merges: 9607 passed. Gate G1 compares against `25e66fa7`;
  the commit that records this line changes only this file.
- Offline smoke (`scripts/smoke-gateway.sh`, re-run 2026-10-01 at `25e66fa7`): `import moeka` ok, no host module loaded by the kernel import;
  `nanobot gateway` health `{"status":"ok","process":"alive","ready":true,"websocket":"running"}`; one mock
  chat turn through the CLI agent and through the HTTP API server (`MOCK-REPLY: smoke ok`).
- Live checkout at preparation time: `~/projects/moeka` on `main` `54069bb2`, `moeka.service` untouched.
- Not verified here: a real channel turn (Telegram/Discord) and the WebUI bundle (no bun/npm in the test image).

## Gates G0-G7 (from `docs/reviews/2026-10-01-architecture-review.md` section 7)

Every gate has a pass criterion. A missing result is a NO-GO, not a skip. G0-G3 and G6 run before anything
changes; G4 is the canary; G5 is the cutover; G7 is the soak. Spec: FR-012 to FR-016, SC-007 to SC-009.

| Gate | Check | Pass |
|---|---|---|
| G0 | `git -C ~/projects/moeka rev-parse HEAD` is `54069bb2`, branch `main`, status shows only `docs/core-map/`; no other agent or shell is using that checkout | exact match, recorded with a timestamp |
| G1 | the tip to cut equals the tip that was tested (the "Verified state" block), or `scripts/test-docker.sh` is re-run on the new tip. `main` and `core-slim` are ancestors; `git diff --stat main consolidate/new-main -- nanobot/{cron,gateway,triggers,channels,bus} bin` is empty | all true; 0 failed |
| G2 | parity probes (`docs/reviews/2026-10-01-parity/`) re-run on the exact tip: same tool set (plus `defer_action` only), same exec env keys, same plugin list, live config validates, request body diff limited to tools and prompt | no other difference |
| G3 | with the service STOPPED: SQLite-backup-API copy (never `cp`) of the session database to two physical disks (root volume and `/mnt/arteta`), plus `memory/`, `cron/jobs.json`, `config.json`, `llm_usage.sqlite3`, the workspace markdown files; `check` each copy and compare session and message counts with the live file; checksum list | counts equal, integrity ok, two disks |
| G4 | canary: a throwaway bot token, a COPY of the workspace (different `MOEKA_WORKSPACE`, port and `--config`), the candidate code in its own worktree; a history turn, a tool call, `/status`, one heartbeat | replies within 60 s, history used, no ERROR lines, heartbeat fires |
| G5 | cutover: "Steps" below; watch `journalctl --user -u moeka.service -f` for 10 minutes | "Registered N tools" contains `message`, `cron`, `run_cli_app`; "Channels enabled" lists telegram and discord; `/health` ready; no traceback |
| G6 | rollback rehearsed BEFORE G5 in a scratch clone against a COPY of state, one turn answered | under 5 minutes, written down |
| G7 | soak 48 hours: a real history turn, a cron fire, a Dream run, one `/new` and one rewind, usage row count grows; then decide on the public push | no regression; owner approves the push |

Automatic NO-GO: any unverified backup; any tool-set difference beyond `defer_action`; the service is not ready
within 60 s of start; a reply without prior context on a chat that has history.

The gateway tool set is also guarded in the suite: `tests/gateway/test_toolset_parity.py` fails if a gateway
path builds `AgentLoop` without `host_tools=True` (review risk R4).

## Preconditions

1. Decide the open items in "Owner decisions" below.
2. `git -C ~/projects/moeka status`: the live checkout has an UNTRACKED `docs/core-map/` (two files). The new
   `main` tracks that directory, so the fast-forward would refuse to overwrite it. Move it aside (do not delete,
   it holds the only copy of two older drafts): `mv docs/core-map /tmp/core-map.live-untracked`.
3. Backup (gate G3). The `sqlite3` command-line tool is NOT installed on this host; use the Python backup API
   through the existing ops script, never a file copy of a live database:
   - Already done once: a verified backup taken 2026-10-01 (service running, WAL-consistent via the backup API,
     `quick_check` ok on every copy) exists in two places:
     `/mnt/arteta/backups/moeka-workspace/20261001T025034Z/` and `~/quarantine/moeka-backup-20261001T025034Z/`.
     It holds `db.tar.zst` (the session DB, `memory/vec.db`, `llm_usage.sqlite3`, the legacy per-channel session
     DBs), `state.tar.zst` (the rest of `~/.nanobot` and `~/.nanobot-sessions` without DBs and logs, plus the
     checkout's `.env`), `git/*.bundle` (moeka, awork-resume, awork, all branches) and `SHA256SUMS`, `MANIFEST.md`
     (restore steps), `quick_check.txt`. Secrets (`config.json`, `keys.env`) are in `secrets/` on the root disk
     only, never on arteta (STANDARDS: no secrets on the Samba export).
   - Just before G5, re-take it with the service stopped (so nothing is written after the copy):
     `python3 ~/.nanobot/ops/sqlite-backup.py backup <live.db> <dest.db>` for each of the session DB
     (`~/.nanobot-sessions/<workspace-id>/sessions.db`), `~/.nanobot/memory/vec.db` and
     `~/.nanobot/llm_usage.sqlite3`, then `python3 ~/.nanobot/ops/sqlite-backup.py check <dest.db>` on every
     copy (argument order per the script's own help), and copy `memory/`, `cron/jobs.json`, `config.json` and the
     workspace markdown files beside them. Record session and message counts of the live file and the copy with
     Python, not the CLI:
     `python3 -c "import sqlite3,sys; c=sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True); print([c.execute(f'select count(*) from {t}').fetchone()[0] for t in ('sessions','messages')])" <db>`.
4. The service: the owner keeps `moeka.service` DISABLED (not enabled at boot) by decision; it is currently
   running. Nothing in this runbook enables it. A reboot leaves the bot down until someone starts it, so the
   rollback rehearsal (G6) must not depend on a reboot.

## Steps

All in `~/projects/moeka` (the live checkout), as the owner, outside any agent run, after G0-G4 and G6 pass.

1. Mark the rollback point (a tag is a new ref, nothing moves): `git tag pre-consolidation-main 54069bb2`. Also
   write the sha into the operator notes.
2. Stop the service: `systemctl --user stop moeka.service`.
3. Fast-forward `main`:
   `git merge --ff-only consolidate/new-main`
   (the branch is already in this repository because worktrees share refs; `--ff-only` fails loudly if `main`
   moved). The working tree now holds the consolidated tree.
4. Dependencies: NONE. There is deliberately no `uv sync --locked` step (review D-M2, spec FR-015). `uv.lock` is
   byte-identical to old `main`'s, so there is nothing to install; the live venv installs moeka as an editable
   package whose `.pth` points at the repository root, so the new `moeka` and `nanobot.kernel` packages import
   from the checkout without a reinstall (the only `pyproject.toml` change against `main` is wheel/sdist
   packaging: `moeka` added to the packaged sources, which an editable install does not use). A sync would only do harm: it removes the channel runtime
   dependencies (telegram, discord) that `nanobot plugins enable` installed outside the lock, which is what made
   the 2026-09-28 deploy crash-loop (`.agent/deploy-runbook.md` step 5b), and `git reset` does not undo it on
   rollback. Check only: `.venv/bin/python -c "import telegram, discord, moeka, nanobot.cli.commands; print('imports OK')"`.
   If the lock ever changes in a later cutover, a sync is followed by `nanobot plugins enable` for every enabled
   channel and the same import check, before the service starts.
5. Unit and launcher: no change. `moeka.service` keeps `WorkingDirectory=%h/projects/moeka` and
   `ExecStart=.../bin/moeka.sh run`; `bin/moeka.sh` is byte-identical to `main`'s.
6. Optional dry check before starting: `bin/moeka.sh doctor`, and `.venv/bin/nanobot status`.
7. Start: `systemctl --user start moeka.service`; `systemctl --user status moeka.service` (G5). Do not
   `enable` it; the owner keeps it disabled.
8. Publishing is NOT part of the cutover (gate G7, FR-016): only after the 48 hour soak and an owner approval,
   `git push origin main` as a fast-forward. `origin` is public. Never `--force`. Leave `core-slim` and
   `origin/core-slim` as they are.

## What to verify after starting

- `journalctl --user -u moeka.service -n 100`: no `ModuleNotFoundError`, "Registered NN tools" includes `message`,
  `cron`, `run_cli_app`; "Channels enabled: ..." lists the real channels; `Cron: ... system job 'dream'`.
- Health: `curl -s http://127.0.0.1:<gateway.port>/health` returns `"ready": true`.
- A real message on Telegram with prior history gets a reply that has the old context (the history check in the
  deploy runbook), a `/status` and a `/dream` command work, a scheduled cron job still fires.
- WebUI loads (`nanobot/web/dist` is git-ignored: `cd webui && bun run build` if it is missing or stale).
- Usage surface records the turn (only after a real model turn; heartbeat and dream record nothing):
  `python3 -c "import sqlite3,os; print(sqlite3.connect('file:'+os.path.expanduser('~/.nanobot/llm_usage.sqlite3')+'?mode=ro', uri=True).execute('select count(*) from llm_calls').fetchone())"`
  grows.
- `.venv/bin/python -c "import moeka; print(moeka.Kernel)"`.

## Rollback to the old `main`

The old `main` commit `54069bb2` is untouched and tagged in step 1. Rehearsed beforehand (G6).

1. `systemctl --user stop moeka.service`
2. `git -C ~/projects/moeka reset --hard pre-consolidation-main` (or `git checkout -B main pre-consolidation-main`).
   Because nothing is pushed before G7, no force push is ever needed for a rollback.
3. No dependency step (the venv was not touched by the cutover); run the import check from step 4, start the
   service.
4. The only persistent change the new code makes is additive columns in `llm_usage.sqlite3` (`PRAGMA user_version` 2).
   The old code does not read them and writes `user_version = 1` again; no restore is needed. A rewind on the new
   code deletes stale archive entries in `history.jsonl`; restore from the G3 backup only if that matters, or if
   the old binary errors on a file (stop the service, copy the backup DB into place, `check` it).

## Consumers (awork, RSI harness)

- Pins move once, from `core-slim` `6f80c392` to the new `main` tip (after the owner fast-forwards it). Both
  pins stay valid until then; nothing rebases `core-slim`.
- The `moeka` package, `nanobot.kernel`, `nanobot.api.complete` (deprecated `MoekaCore`) and `docs/python-sdk.md` are
  unchanged by the consolidation: the same code as at `core-slim` `c382d0c9`, plus the gateway packages beside it.
- Differences a consumer can notice:
  - Base dependencies grew back to `main`'s set (`websockets`, `qrcode`, `croniter`, `questionary`, `packaging`;
    the `core` extra gains `croniter`; new `api` extra). A consumer lock must re-resolve.
  - The build hook (`hatch_build.py`) runs a WebUI build on a NON-editable wheel/sdist build. A consumer that
    installs moeka as an editable path dependency is unaffected; otherwise set `NANOBOT_SKIP_WEBUI_BUILD=1`.
  - `Config` accepts `channels`, `gateway`, `api`, `heartbeat`, `transcription` again (they were stripped on
    `core-slim`). Kernel-only hosts never set them.
  - `import nanobot.agent.loop` and `import moeka` still load no gateway module (guarded by
    `tests/core/test_import_boundary.py`); a bare `AgentLoop` or `moeka.Kernel` agent never registers `message`,
    `cron` or `run_cli_app` (the `host_tools` seam). The builtin slash router still lists `/trigger`, `/pairing`
    and `/evaluator-prompt` for any loop.
- awork: bump the submodule to the new `main` tip, `uv lock`/sync, run its gate command; RSI harness: bump the
  submodule, run its pin check. Retire the old pin only after both pass (tasks T015-T017, not done here).

## Owner decisions

1. Fast-forward `main` to `consolidate/new-main` (this runbook) versus renaming branches. Recommended: fast-forward.
2. `.github/workflows/ci.yml` is `main`'s (floor `NANOBOT_MIN_TESTS=5400`; the suite is now about 9600) and it still
   runs `basedpyright` strict, which reports 791 errors on this tree (289 on old `main`; the kernel adds about 500
   under strict). Raise the floor, and either fix or scope the pyright step, before relying on CI.
3. The gateway stays on the legacy `AgentLoop.from_config` path (see `plan.md`, "Stage 2 remaining"); approve
   stage 2 as separate work.
4. Delete the stray untracked `docs/core-map/` in the live checkout (it predates and is superseded by the tracked one).
