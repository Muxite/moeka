---
description: "Task list for 005-multi-instance"
---

# Tasks: Multi-Instance moeka (no singleton)

**Input**: `/specs/005-multi-instance/spec.md`, `/specs/005-multi-instance/plan.md`

**Prerequisites**: M1 (`review/usage` merged into `consolidate/new-main`) has landed; the implementer works in its
own worktree cut from post-M1 `consolidate/new-main`, never in `~/projects/moeka`.

**Tests**: test-first (constitution, gap-closing work). The implementer writes its own tests under `tests/`.
Held-out tests exist in `~/projects/.heldout/moeka/005-multi-instance/`; the implementer MUST NOT read that
directory. Feedback arrives as failing FR ids with counts only.

**Organization**: by user story; each story is independently testable.

## Implementation status (implementer, 2026-10-01)

- Branch `feat/multi-instance` (worktree `~/projects/moeka-impl-005`), cut from `consolidate/new-main` at
  `09d7fb94` (M1 = `4f5a7471 merge review/usage into new main` is in the base), plus `spec/005-multi-instance`.
- Baseline before any change (T001): `scripts/test-docker.sh` 9699 passed, 59 skipped.
- All tasks below are implemented with tests under `tests/` (the files each task names). Results of the final
  runs are in T039's note. Interpretations of open points are listed at the end of this file.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency)
- **[Story]**: US1..US8 from spec.md

## Phase 1: Setup

- [x] T001 Confirm the base: `git log` shows the M1 merge on `consolidate/new-main`; record the base sha in the
  implementer report. Run `scripts/test-docker.sh` once and record the green baseline.
- [x] T002 [P] Add test helpers in `tests/_multi_instance.py`: temp `HOME` fixture, fake `nanobot` script (records
  argv, sleeps, exits on TERM), fake `systemctl`/`loginctl` scripts (log argv to a file; `is-active` exit 3
  unless a marker file says active), and a helper to run `bin/moeka.sh` with these env vars (spec Test Harness
  Contract).
- [x] T003 [P] Register a `docker` pytest marker in `pyproject.toml` (skip when `docker` is absent).

---

## Phase 2: Foundational (blocks all stories)

- [x] T004 Create `nanobot/config/instances.py` (stdlib only): `instance_kind(root, home)`, `unit_for(root, home)`,
  `discover(home) -> list[dict]` (default, `~/.moeka-*`, registry), `used_ports(instances)` with schema defaults,
  `choose_port_base(...)`, `run_dir()`; a `__main__` with `list --json` and `ports --json`. Unit tests in
  `tests/config/test_instances.py`. (FR-011, FR-016, FR-017)
- [x] T005 Add `nanobot/config/instances.py` to the reviewed host ambient allow-list in
  `tests/kernel/test_no_ambient_reads.py` and keep the import-boundary test green.
- [x] T006 Create `nanobot/kernel/instance_lock.py` (`acquire_instance_lock`, `InstanceLock`, `InstanceLockedError`,
  in-process registry, `.instance.json` holder record) and re-export the errors plus `ReadOnlyKernelError` from
  `moeka/errors.py`. Tests in `tests/kernel/test_instance_lock.py`, including cross-process and SIGKILL release.
  (FR-033..FR-036)

**Checkpoint**: discovery, ports and locks exist as tested building blocks.

---

## Phase 3: User Story 1 - Several gateway instances side by side (P1) MVP

**Goal**: `moeka.sh` acts on one instance; `new` gives unique ports and a Unix socket; unexpanded workspace fails.

**Independent Test**: spec US1.

- [x] T007 [P] [US1] Tests first in `tests/cli/test_moeka_sh_instances.py` for FR-001..FR-012 (stop leaves others
  running, PID reuse not signalled, status exit codes and `--json`, env load order, telegram-pair target, run PID +
  lock + `--workspace`, start idempotence, list).
- [x] T008 [P] [US1] Tests first in `tests/cli/test_moeka_sh_new.py` for FR-013..FR-020 (name validation, absolute
  workspace, port base rules, concurrent `new`, socket length, keys.env mode).
- [x] T009 [US1] Rewrite instance resolution, env loading (FR-001..FR-003, Q3), `stop`/`status`/`doctor`/`logs`
  (FR-004..FR-007, no `pkill`/`pgrep`, `/proc` only), `run` (FR-008), `start` (FR-009), unit mapping (FR-012) in
  `bin/moeka.sh`; route systemctl through `MOEKA_SYSTEMCTL`; honour `MOEKA_NANOBOT_BIN` and
  `MOEKA_STOP_TIMEOUT_S`.
- [x] T010 [US1] Implement `new` (FR-013..FR-020) and `list` (FR-011) in `bin/moeka.sh` using
  `nanobot/config/instances.py`, with the `<run dir>/new.lock` serialisation.
- [x] T011 [US1] `telegram-pair` writes `<root>/keys.env` (FR-010); `scripts/telegram_pair.py` creates it 0600.
- [x] T012 [P] [US1] Tests first in `tests/config/test_unexpanded_workspace.py` (FR-025, FR-026), then raise
  `UnexpandedWorkspaceError` in `nanobot/config/schema.py` `Config.workspace_path`; map it to exit 2 in
  `nanobot/cli/commands.py` and `nanobot/cli/gateway.py` before any directory is created; update the existing tests
  that asserted the fallback.
- [x] T013 [US1] FR-002 guard in `bin/moeka.sh` (exit 2, no fallback).

**Checkpoint**: three instances run side by side under fakes; SC-002 and SC-007 measurable.

---

## Phase 4: User Story 2 - One writer per state, one poller per token (P1)

**Independent Test**: spec US2.

- [x] T014 [P] [US2] Tests first in `tests/kernel/test_kernel_attach.py` for FR-034..FR-038 (second writer in-process
  and cross-process, symlinked path, read-only surface, no files created, missing state dir).
- [x] T015 [US2] Wire the lock into `nanobot/kernel/kernel.py` (`attach="write"|"read_only"`, lock before any
  directory creation, release on close) and add read-only guards in `nanobot/kernel/sessions.py`,
  `nanobot/kernel/memory.py`, `nanobot/kernel/epistemics.py`, and a read-only open path in
  `nanobot/session/sqlite_store.py` and `nanobot/session/manager.py`.
- [x] T016 [US2] Take the instance lock in `nanobot gateway`, `nanobot serve`, one-shot and `--classic`
  `nanobot agent` (`nanobot/cli/commands.py`, `nanobot/cli/gateway.py`, `nanobot/cli/gateway_runtime.py`); exit 3 on
  refusal; read-only commands untouched (FR-033, FR-039). Tests in `tests/cli/test_instance_lock_cli.py`.
- [x] T017 [P] [US2] Tests first in `tests/channels/test_token_lock.py` (FR-041, FR-042), then create
  `nanobot/channels/token_lock.py` and integrate it into `nanobot/channels/manager.py` (`state="locked"`).
- [x] T018 [P] [US2] Tests first in `tests/channels/test_telegram_conflict.py` (FR-043), then implement
  `handle_polling_error`, `polling_state`, `conflict_retry_s`/`conflictRetryS` and the conflict restart path in
  `nanobot/channels/telegram/runtime.py`; map `conflict` in `ChannelManager.get_status()`.
- [x] T019 [US2] Extend the floor for lock/PID/run files (FR-040) in `nanobot/security/protected_paths.py`.

**Checkpoint**: SC-003 and SC-006 (lock part) measurable.

---

## Phase 5: User Story 3 - One budget and one ledger across processes (P1)

**Independent Test**: spec US3.

- [x] T020 [US3] Factor `CapBudget`'s admit/settle/release arithmetic into pure functions in
  `nanobot/kernel/budget.py` with no behaviour change (existing `tests/kernel/test_budget.py` stays green); state
  "in-memory, per process" in the `CapBudget` docstring (FR-050).
- [x] T021 [P] [US3] Tests first in `tests/kernel/test_shared_budget.py`: parity with `CapBudget` on a scripted
  sequence (same refusals and figures), cap mismatch and `reset_caps` (FR-049), lock timeout ->
  `budget_unavailable` (FR-048), lease expiry and `budget.expire` (FR-047), late settle/release true-up (FR-046),
  4-process concurrency invariant (FR-045, SC-004), SIGKILL holder (SC-006).
  Erratum (head agent, 2026-10-01): US3-1's "exactly 50 admitted" contradicted FR-045; with CapBudget's
  float arithmetic the $0.50 / $0.01 case admits 49 or 50 (49 x 0.01 = 0.49000000000000027). FR-045 governs;
  tests assert 49 <= admitted <= 50 and the cap invariant, never a rounding tweak.
- [x] T022 [US3] Implement `nanobot/kernel/budget_shared.py` (`SharedCapBudget`, tables via the usage store
  migration in `nanobot/llm_usage/store.py`, retry queue, `flush()`), export it from `moeka/budget.py`, and call
  `budget.flush()` from `Kernel.close()` when present (FR-044..FR-050).
- [x] T023 [US3] Close-time flush of pending usage writes in `nanobot/llm_usage/store.py` (FR-051), with a
  multi-process test in `tests/llm_usage/test_multiprocess_store.py` (SC-005).

**Checkpoint**: SC-004 and SC-005 measurable.

---

## Phase 6: User Story 4 - Many agent cores (P2)

- [x] T024 [P] [US4] Tests first in `tests/kernel/test_many_kernels.py` (FR-052..FR-055): two Kernels in one process,
  shared data dir per-consumer totals, close one keep the other, memory dirs per key, `kernel.memory_key_shared`.
- [x] T025 [US4] Add `data_dir` to `Environment.for_host` in `nanobot/kernel/hostenv.py` with the strict overlap
  check (FR-053).
- [x] T026 [US4] Emit `kernel.memory_key_shared` in `Kernel.agent` (`nanobot/kernel/kernel.py`) (FR-055).

---

## Phase 7: User Story 5 - Per-instance systemd units (P2)

- [x] T027 [P] [US5] Tests first in `tests/scripts/test_install_service.py` (FR-021..FR-024): rendered unit, dry
  run, fake systemctl log, `moeka.service` bytes unchanged.
- [x] T028 [US5] Add `scripts/moeka@.service` and the `<name>` mode of `scripts/install-service.sh`
  (`--no-enable`, `--dry-run`, `MOEKA_SYSTEMCTL`, `MOEKA_LOGINCTL`); wire `enable`/`disable` in `bin/moeka.sh`
  (FR-024).

---

## Phase 8: User Story 6 - Containers (P2)

- [x] T029 [P] [US6] Static tests in `tests/scripts/test_container_files.py` (FR-056, FR-058) and docker-marked tests
  in `tests/integration/test_containers.py` (UID 1000, missing secret exit 2, seeded config, two compose projects
  plus one host instance, SC-001).
- [x] T030 [US6] Add `Dockerfile`, `scripts/container-entrypoint.sh` and `compose.yaml` (FR-056..FR-058); keep
  `.dockerignore` excluding `.venv`, secrets and local state.
- [x] T031 [US6] Rewrite the Docker sections of `docs/deployment.md` (FR-059) and add a doc test in
  `tests/docs/test_deployment_doc.py` that fails on the stale names and on any referenced file that does not exist.

---

## Phase 9: User Story 7 - Isolation floor (P2)

- [x] T032 [P] [US7] Tests first in `tests/security/test_floor_instances.py` (FR-060, FR-061, SC-008 matrix).
- [x] T033 [US7] Implement the `keys.env` basename rule, instance-root `.env`, `other_instance_roots` in
  `ProtectedFloor`/`from_paths`, and legacy discovery in `legacy_floor_extras` (`nanobot/security/protected_paths.py`,
  `nanobot/kernel/legacy.py`).

---

## Phase 10: User Story 8 - No hidden process-wide instance (P3)

- [x] T034 [P] [US8] Tests first in `tests/config/test_config_owned_paths.py` (FR-027..FR-031).
- [x] T035 [US8] `_legacy_paths` from `config.runtime_data_dir` (`nanobot/kernel/legacy.py`); default identity from
  `get_state_home()` (`nanobot/gateway/runtime.py`); launchd log dir (`nanobot/gateway/service.py`); `data_dir`
  helpers in `nanobot/config/paths.py` and their config-holding callers; `pin_config_path`/`reset_config_path`/
  `ConfigPathConflictError` in `nanobot/config/loader.py`; pin in the gateway command.

---

## Phase 11: Polish & Cross-Cutting

- [x] T036 [P] Rewrite `docs/multiple-instances.md` (FR-032, FR-062): moeka.sh flows, ports and sockets, locks,
  shared budget, Kernel rules, process globals, floor limits; mermaid only for diagrams.
- [x] T037 [P] Update `CLAUDE.md` deviation notes (unexpanded workspace now fails; instance lock; template unit) and
  `.agent/moeka-kernel-design.md` (instance lock, `SharedCapBudget`, read-only attach).
- [x] T038 Fold Q1-Q3 into `specs/CLARIFY-LOG.md` as `005-Q1..005-Q3` after M1's numbering is settled.
- [x] T039 Run `scripts/test-docker.sh`, `scripts/smoke-gateway.sh` and the docker-marked tests; confirm SC-009
  (live checkout `git status` and `~/.config/systemd/user/moeka.service` bytes unchanged); report the results with
  their numbers.
  Result 2026-10-01: `scripts/test-docker.sh` 9844 passed, 64 skipped (baseline 9699/59; the 5 new skips are
  the docker-marked tests, which passed on the host: `tests/integration/test_containers.py` 5 passed).
  `scripts/smoke-gateway.sh`: health ok, CLI and API mock turns ok, second writer refused with exit 3.
  SC-009: `~/.config/systemd/user/moeka.service` untouched (mtime 2026-06-02); the live checkout was never
  written (its `git status` shows a pre-existing untracked `docs/core-map/`).

---

## Dependencies & Execution Order

- Phase 1 then Phase 2 (T004 blocks US1, US5 and US7; T006 blocks US2 and US4).
- US1 (Phase 3) is the MVP; US2 and US3 are also P1 and can start after Phase 2 in parallel with US1.
- US4 depends on T015 (attach) and T022 (shared budget) only for its combined tests; its own changes are separate.
- US5 depends on T009 (unit mapping). US6 depends on T012 (fail fast) and T016 (gateway lock). US7 depends on T004.
- Polish runs last.

## Parallel Opportunities

- T002, T003; T007, T008, T012; T014, T017, T018; T021 with US1 work; T024, T027, T029, T032, T034 once their
  prerequisites land.

## Implementation Strategy

1. Phases 1-2, then US1 and validate SC-002/SC-007.
2. US2 and US3 (both P1), validate SC-003..SC-006.
3. US4..US8 in priority order; then polish and the full green run.
4. Hand the worktree to the runner for held-out tests; fix only the reported FR ids.

## Implementer notes: interpretations (2026-10-01)

- `.instance.json` `mode` is `"write"` for every writer; the CLI also records `role`
  (`gateway`/`serve`/`agent`). The CLI writers take the lock right after config load when the workspace already
  exists (so a second writer is refused before validation), else just before their first write (a command that
  fails validation still creates nothing).
- `Config.workspace_path` expands `${VAR}` references whose variable is set before checking for `${` (the
  "effective workspace string"), so a config holding `${MOEKA_WORKSPACE}` with the variable set keeps working;
  an unset or empty variable raises `UnexpandedWorkspaceError`. `nanobot gateway/serve/agent` also exit 2 when
  `MOEKA_WORKSPACE` itself is a literal `${...}` (US1-3).
- `status --json` `unit` is the instance's own unit name whether or not it is active (`null` only for a
  registered instance); `manager` says which mechanism is active.
- `restart` on an instance whose unit is active runs `systemctl --user restart <unit>` (FR-012), else stop+start.
- The read-only Kernel's `kernel.usage.*` reads an empty in-memory view when the data dir has no usage store yet,
  so a read-only attach creates no file; `kernel.epistemics` reads need existing fact/artifact stores.
- `budget.expire` events carry `call_id`, `charged_usd`, `charged_tokens` (aliases `usd`, `tokens`),
  `budget_id`, `holder`. A Kernel attaches its tracer to a `SharedCapBudget` it is given.
- Own-instance `.env` denial applies to instance-root-like dirs (state dir, data dir, and the work dir only in
  the flat layout); a kernel-native host's separated `work_dir/.env` stays readable. `keys.env` is denied
  everywhere.
- The upstream Render blueprint (`render.yaml`) is not maintained by this image; `docs/deployment.md` says so
  instead of describing it.
