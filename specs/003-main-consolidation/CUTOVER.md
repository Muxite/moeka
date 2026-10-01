# Cutover runbook: consolidated branch becomes `main`

**Status**: prepared 2026-10-01, NOT executed. Nothing here has been run against the live checkout, the
service, `main`, `core-slim` or any remote. The owner runs it.

Candidate: branch `consolidate/new-main` (worktree `/home/muk/projects/moeka-newmain`), tip recorded in the
"Verified state" block. It contains both `main` (`54069bb2`) and `core-slim` (`c382d0c9`) as ancestors, so
`main` fast-forwards to it: no rename, no force-push, no rewritten commit, and `core-slim` stays at its pin.

## Verified state (before cutover)

- Full suite via `scripts/test-docker.sh`: 9607 passed, 0 failed, 59 skipped (6 min 28 s, image built from this tree).
- Offline smoke (`scripts/smoke-gateway.sh`): `import moeka` ok, no host module loaded by the kernel import;
  `nanobot gateway` health `{"status":"ok","process":"alive","ready":true,"websocket":"running"}`; one mock
  chat turn through the CLI agent and through the HTTP API server (`MOCK-REPLY: smoke ok`).
- Live checkout at preparation time: `~/projects/moeka` on `main` `54069bb2`, `moeka.service` untouched.
- Not verified here: a real channel turn (Telegram/Discord) and the WebUI bundle (no bun/npm in the test image).

## Preconditions

1. Decide the open items in "Owner decisions" below.
2. `git -C ~/projects/moeka status`: the live checkout has an UNTRACKED `docs/core-map/` (two files). The new
   `main` tracks that directory, so the fast-forward would refuse to overwrite it. Move it aside first:
   `mv docs/core-map /tmp/core-map.live-untracked`.
3. Back up state (the new code adds columns to `llm_usage.sqlite3`; session DB schema is unchanged):
   `cp -a ~/.nanobot/llm_usage.sqlite3* ~/.nanobot/config.json /tmp/moeka-precutover/` (stop the service first so
   the WAL is quiescent), and note `sqlite3 <sessions.db> "select count(*) from sessions; select count(*) from messages;"`.

## Steps

All in `~/projects/moeka` (the live checkout), as the owner, outside any agent run.

1. Mark the rollback point (a tag is a new ref, nothing moves): `git tag pre-consolidation-main 54069bb2`.
2. Stop the service: `systemctl --user stop moeka.service`.
3. Fast-forward `main`:
   `git merge --ff-only consolidate/new-main`
   (the branch is already in this repository because worktrees share refs; `--ff-only` fails loudly if `main`
   moved). The working tree now holds the consolidated tree.
4. Dependencies: `uv sync --locked` (the lock is `main`'s, unchanged: no package added or removed by the kernel).
   Then re-enable channel runtime deps, which `uv sync` removes (known gotcha, see `.agent/deploy-runbook.md`):
   `.venv/bin/nanobot plugins list`, then `.venv/bin/nanobot plugins enable telegram` / `discord` for each enabled
   channel, and `.venv/bin/python -c "import telegram, discord, moeka, nanobot.cli.commands; print('imports OK')"`.
5. Unit and launcher: no change. `moeka.service` keeps `WorkingDirectory=%h/projects/moeka` and
   `ExecStart=.../bin/moeka.sh run`; `bin/moeka.sh` is byte-identical to `main`'s.
6. Optional dry check before starting: `bin/moeka.sh doctor`, and `.venv/bin/nanobot status`.
7. Start: `systemctl --user start moeka.service`; `systemctl --user status moeka.service`.
8. Publish (only after the live bot is verified): `git push origin main` is a fast-forward. Do NOT push with
   `--force`. Leave `core-slim` and `origin/core-slim` as they are.

## What to verify after starting

- `journalctl --user -u moeka.service -n 100`: no `ModuleNotFoundError`, "Registered NN tools" includes `message`,
  `cron`, `run_cli_app`; "Channels enabled: ..." lists the real channels; `Cron: ... system job 'dream'`.
- Health: `curl -s http://127.0.0.1:<gateway.port>/health` returns `"ready": true`.
- A real message on Telegram with prior history gets a reply that has the old context (the history check in the
  deploy runbook), a `/status` and a `/dream` command work, a scheduled cron job still fires.
- WebUI loads (`nanobot/web/dist` is git-ignored: `cd webui && bun run build` if it is missing or stale).
- Usage surface records the turn: `sqlite3 ~/.nanobot/llm_usage.sqlite3 "select count(*) from llm_calls"`
  grows.
- `.venv/bin/python -c "import moeka; print(moeka.Kernel)"`.

## Rollback to the old `main`

The old `main` commit `54069bb2` is untouched and tagged in step 1.

1. `systemctl --user stop moeka.service`
2. `git -C ~/projects/moeka reset --hard pre-consolidation-main` (or `git checkout -B main pre-consolidation-main`);
   if `origin/main` was already pushed, restoring it needs an owner-approved force push; avoid by rolling back
   locally only until sure.
3. `uv sync --locked`, re-run `nanobot plugins enable ...`, start the service.
4. The only persistent change the new code makes is additive columns in `llm_usage.sqlite3` (`PRAGMA user_version` 2).
   The old code does not read them and writes `user_version = 1` again; no restore is needed. Restore the backup copy
   only if the old binary errors on that file.

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
