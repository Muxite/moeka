---
description: "Task list for main consolidation (not executed)"
---

# Tasks: Main Consolidation

**Input**: `specs/003-main-consolidation/spec.md`, `plan.md`
**Status**: recorded plan; none of these tasks have started. All work happens in worktrees.

## Phase 1: Setup

- [ ] T001 Record the live checkout branch and HEAD, and `git ls-remote` of `core-slim`, in the consolidation record (SC-003, SC-004)
- [ ] T002 [P] Run the kernel suite on `core-slim` and the gateway suite on `main` in Docker (`scripts/test-docker.sh`) as the baseline (SC-001)
- [ ] T003 Decide CLARIFY-LOG Q8 (gateway migration and step order); record in `spec.md`

## Phase 2: Foundational

- [ ] T004 Create a worktree and branch `consolidate/kernel-on-main` from `main` (FR-007, FR-008)
- [ ] T005 Dry-run both directions (main-based and core-slim-based) and count manual hunks; pick the lower (plan step 4)
- [ ] T006 Write `docs/consolidation-record.md` skeleton with every top-level path (FR-011, SC-006)

## Phase 3: Consumer Story 1 - one branch (P1)

- [ ] T007 [US1] Bring kernel-owned paths from `core-slim`: `nanobot/kernel`, `moeka`, `nanobot/llm_usage`, `nanobot/security`, `tests/kernel`, `tests/moeka`, `docs/core-map`, `.agent/` design docs (FR-002)
- [ ] T008 [US1] Reconcile each shared file under `nanobot/agent/`, `nanobot/agent/tools/`, `nanobot/providers/`, `nanobot/config/`, `nanobot/core/`, `nanobot/cli/`, `nanobot/command/builtin.py`, `nanobot/nanobot.py`; log each in the record (FR-005)
- [ ] T009 [US1] Keep `nanobot/api/{runtime,server}.py`; keep the legacy `complete.py` until `002` T014 (FR-010)
- [ ] T010 [US1] Reconcile `CLAUDE.md`, `AGENTS.md`, `docs/README.md` and the "Branching Strategy" section into one description (FR-006)
- [ ] T011 [US1] Run both suites; fix regressions; list intentional drops (SC-001)

## Phase 4: Consumer Story 2 - gateway consumes kernel (P1)

- [ ] T012 [P] [US2] Extend `tests/core/test_import_boundary.py` to probe `import moeka` and `nanobot.kernel` (FR-003)
- [ ] T013 [US2] Add a reviewed host-side allow-list to `tests/kernel/test_no_ambient_reads.py` for gateway packages, with a reason each (FR-004, SC-002)
- [ ] T014 [US2] Gateway stage 2 (after T003): move the loop path onto `moeka.Kernel` behind a switch in `nanobot/gateway/`; usage visible in the `001` surface

## Phase 5: Consumer Story 3 - pins (P2)

- [ ] T015 [US3] awork: bump its submodule to the consolidated `main`; run the awork gate command (SC-005)
- [ ] T016 [US3] RSI harness: bump its submodule; run its pin check (SC-005)
- [ ] T017 [US3] Confirm `core-slim` `6f80c392` still resolves; do not delete or rebase it (SC-004)

## Phase 6: Story 4 - live service (P1)

- [ ] T018 [US4] After owner approval only: deploy per `.agent/deploy-runbook.md`; verify the live checkout state before and after (SC-003)

## Phase 7: Cutover gates (added by review 2026-10-01; see docs/reviews/2026-10-01-architecture-review.md)

- [ ] T019 Verified two-disk backup of live state with the service stopped; counts and integrity recorded (FR-012, SC-007)
- [ ] T020 Re-run the parity probes (`docs/reviews/2026-10-01-parity/`) on the exact tip to be cut; list approved differences (FR-013, SC-008)
- [ ] T021 Canary on a throwaway bot credential and a workspace copy: history turn, tool call, heartbeat (FR-014)
- [ ] T022 Rehearse rollback on a copy; record the time (SC-009)
- [ ] T023 Add a gateway test asserting the exact registered tool set (guards the `host_tools` default)
- [ ] T024 Fix `CUTOVER.md`: add session DB and memory to the backup, replace the `sqlite3` command-line calls, drop the dependency sync (FR-015)
- [ ] T025 After a 48 hour soak and owner approval only: publish `main` (FR-016)

## Dependencies

- T003 blocks T014. T005 precedes T007. T011 precedes T015 and T016. T018 requires explicit owner approval. T019-T022 precede T018; T025 follows T018 by the soak period.
