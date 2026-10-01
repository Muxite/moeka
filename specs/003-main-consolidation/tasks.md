---
description: "Task list for main consolidation (executed 2026-10-01 up to the cutover; cutover not performed)"
---

# Tasks: Main Consolidation

**Input**: `specs/003-main-consolidation/spec.md`, `plan.md`
**Status**: executed in worktree `/home/muk/projects/moeka-newmain` on branch `consolidate/new-main` (direction
changed 2026-09-30 by the owner: `core-slim` is the base, the gateway is carried over from `main`, not the reverse).
Cutover, consumer pins and the live deploy are NOT done; see `CUTOVER.md`.

## Phase 1: Setup

- [x] T001 Live checkout recorded before the work: `~/projects/moeka` on `main` `54069bb2`, plus an untracked `docs/core-map/`; `origin/core-slim` is `6f80c392`. Re-checked unchanged after the work.
- [x] T002 Baselines (bind-mounted image run): `main` 7999 passed / 4 failed (3 `tests/cli/test_commands.py` WebUI-foreground tests need bun/npm, 1 `test_dream` git-config test; both environment-specific), `core-slim` 6223 passed / 1 failed (same dream test). Under `scripts/test-docker.sh` the consolidated tree is 9607 passed, 0 failed.
- [x] T003 Q8: executed as option A (consolidate first with the gateway on the legacy loop path, migrate later), following the owner's 2026-09-30 direction; the owner still confirms A for stage 2 (noted in `CLARIFY-LOG.md`).

## Phase 2: Foundational

- [x] T004 Worktree `/home/muk/projects/moeka-newmain`, branch `consolidate/new-main` from `core-slim` (not `consolidate/kernel-on-main` from `main`).
- [x] T005 Direction measured instead of dry-run: 951 `main`-only files restore by `git checkout main --`; only about 25 shared files had gateway hooks removed (loop, schema, recovery, builtin, cli, templates, ...). `core-slim` base needed far fewer hand hunks than a `main` base (201 modified shared files).
- [x] T006 `docs/consolidation-record.md` written (per top-level path).

## Phase 3: Consumer Story 1 - one branch (P1)

- [x] T007 Kernel-owned paths are the base (never touched): `nanobot/kernel`, `moeka`, `nanobot/llm_usage`, `nanobot/security`, kernel tests, `docs/core-map`, `.agent/` design docs, specs.
- [x] T008 Shared files reconciled: gateway hooks restored behind seams (see `plan.md`, "Seams as built"); each decision in `docs/consolidation-record.md`.
- [x] T009 `nanobot/api/{runtime,server}.py` restored; `complete.py` kept (legacy until `002` T014).
- [x] T010 `CLAUDE.md`, `AGENTS.md`, `docs/README.md`, `docs/my-tool.md` reconciled; `.agent/deploy-runbook.md` taken from `main`.
- [x] T011 Both suites run under `scripts/test-docker.sh` (now with a wall-clock cap): 9607 passed, 0 failed, 59 skipped. Intentional test changes: dropped slim "retired section" tests (`test_dream_config_legacy`, `test_retired_cli_apps_config`, three in `test_removed_keys`), replaced the slim CLI assertions in `tests/cli/*` with `main`'s, extended `test_builtin_registry`, `test_registered_tool_names` (host tools only for host loops), prompt-leak test (host-owned files excluded, `tool_contract.md` checked as rendered for a kernel agent), `host_tools=True` in the loop-construction fixtures of three gateway tests.

## Phase 4: Consumer Story 2 - gateway consumes kernel (P1)

- [x] T012 Import boundary: `tests/core/test_import_boundary.py` (already probes `moeka` and `nanobot.kernel`) is green and stricter than `main`'s: the loop, session recovery and config import no `webui`, `cron`, `triggers` or `apps` module.
- [x] T013 Reviewed host-side allow-list `HOST_AMBIENT_ALLOWLIST` in `tests/kernel/test_no_ambient_reads.py` (9 entries, reason each, guard tests that it never covers kernel code and that every entry still needs it).
- [ ] T014 [US2] Gateway stage 2: move the loop path onto `moeka.Kernel` behind a switch in `nanobot/gateway/`. NOT started; see `plan.md`, "Stage 2 remaining".

## Phase 5: Consumer Story 3 - pins (P2)

- [ ] T015 [US3] awork: bump its submodule to the consolidated `main`; run the awork gate command. Waits for the cutover.
- [ ] T016 [US3] RSI harness: bump its submodule; run its pin check. Waits for the cutover.
- [x] T017 `core-slim` `c382d0c9` / `origin/core-slim` `6f80c392` still resolve; no branch was rebased, renamed, deleted or pushed. `consolidate/new-main` contains `core-slim` and `main` as ancestors.

## Phase 6: Story 4 - live service (P1)

- [ ] T018 [US4] Cutover: owner only, per `CUTOVER.md` (not performed; the live checkout and `moeka.service` were not touched).
- [x] T019 Offline smoke test in an isolated container (`scripts/smoke-gateway.sh`): `moeka` import, gateway health, mock chat turn through the CLI agent and the HTTP API server.

## Phase 7: Cutover gates (added by review 2026-10-01; see docs/reviews/2026-10-01-architecture-review.md)

Numbering: the review wrote these as T019-T025; T019 was already taken by the smoke test, so they are T020-T026
here (review T0nn = T0nn+1). Gates G0-G7 are in `CUTOVER.md`.

- [ ] T020 Verified two-disk backup of live state with the service stopped; counts and integrity recorded (FR-012, SC-007, gate G3). Partial: a verified backup taken 2026-10-01 with the SQLite backup API exists at `/mnt/arteta/backups/moeka-workspace/20261001T025034Z` and `~/quarantine/moeka-backup-20261001T025034Z`; G3 still re-takes it with the service stopped just before cutover.
- [ ] T021 Re-run the parity probes (`docs/reviews/2026-10-01-parity/`) on the exact tip to be cut; list approved differences (FR-013, SC-008, gate G2)
- [ ] T022 Canary on a throwaway bot credential and a workspace copy: history turn, tool call, heartbeat (FR-014, gate G4)
- [ ] T023 Rehearse rollback on a copy; record the time (SC-009, gate G6)
- [x] T024 Gateway test asserting the registered tool set (guards the `host_tools` default): `tests/gateway/test_toolset_parity.py` (host loops register `message`, `cron`, `run_cli_app`; kernel agents do not; every gateway `AgentLoop` construction site passes `host_tools=True`).
- [x] T025 Fix `CUTOVER.md`: session DB and memory in the backup, `ops/sqlite-backup.py` in place of the `sqlite3` command-line tool, dependency sync dropped (FR-015), gates G0-G7.
- [ ] T026 After a 48 hour soak (gate G7) and owner approval only: publish `main` (FR-016)

## Dependencies

- T011 precedes T015 and T016. T018 requires explicit owner approval and precedes T015 and T016. T020-T023 precede T018; T026 follows T018 by the soak period.
