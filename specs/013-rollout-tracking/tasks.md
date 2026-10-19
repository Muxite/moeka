# Tasks: rollout tracking across the kernel and the RSI harness (spec 013)

Repo prefix: `[moeka]` tasks land here; `[rsi]` tasks land in moeka-rsi after its `core/` pin includes the moeka
tasks they depend on.

## Phase 1: Attribution

- [ ] T001 [moeka] FR-003 "Rollout tags" section in `schemas/ATTRIBUTION.md` (keys, meaning, `rollout` equals the
      session suffix)
- [ ] T002 [moeka] FR-003 `tests/kernel/test_rollout_attribution.py`: span tags reach usage records for a
      completion, a retried call and an agent run (fake provider)
- [ ] T003 [moeka] FR-004 finish `001` T009 (memory, Dream and router calls bind the active attribution); extend
      T002 with a Dream call
- [ ] T004 [rsi] FR-001/002/003 `runner.py`: `Kernel(..., consumer="rsi-harness")`, `experiment` and `core_sha`
      tags, session convention asserted
- [ ] T005 [rsi] FR-005 `rsi.ids.new_rollout_id` used by rollout, author, seed-check and vmspool paths; origin
      recorded

## Phase 2: Trusted usage

- [ ] T006 [moeka] FR-013 `schemas/rollout-record.v1.schema.json`, `validate.py` registration, example, frozen copy
- [ ] T007 [rsi] FR-006 meter writes `usage-record.v1` (`producer = rsi-meter`); legacy fields move to `tags`;
      validated against the schema in tests
- [ ] T008 [rsi] FR-007 `rollouts` table and migration in `db.py`; row created before start, finalised after
      collection
- [ ] T009 [rsi] FR-008 checkpoint at start, verify and reconcile at end; the five statuses; SC-002 tests
- [ ] T010 [rsi] NFR-001/003 fail-open writes with counters; reconcile timing test

## Phase 3: Evidence links

- [ ] T011 [moeka] FR-010 `schemas/capability-cert.v2.schema.json`; `cert_schema_problems` v1+v2; SC-004 tests;
      core-map doc 03 updated
- [ ] T012 [rsi] FR-009 signals list rollouts; `author_tasks` stores them
- [ ] T013 [rsi] FR-010 judge issues capcert v2 with lineage from reconciled rollouts only
- [ ] T014 [rsi] FR-011 `submission`/`cert` payload fields, `heldout-score` type, promotion records carry rollouts
- [ ] T015 [rsi] FR-014 `rsi rollouts show|trace|export`; SC-003 test

## Phase 4: Held-out binding

- [ ] T016 [moeka] FR-012 `heldout-report` 1.1: `tree_sha256`, worktree `commit` + `dirty`, `experiment_id`,
      refusal without either identity; SC-005 test
- [ ] T017 [rsi] FR-011 judge appends `heldout-score` for reports it acts on

## Close-out

- [ ] T018 [moeka] SC-006 frozen copies and compatibility tests for every new or changed schema
- [ ] T019 [rsi] bump `core/` pin; moeka-rsi `specs/006-rollout-tracking` status updated
- [ ] T020 Targeted Docker suites, ruff, and the full suite in both repos
