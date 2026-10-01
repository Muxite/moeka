---
description: "Task list for consumer usage surface"
---

# Tasks: Consumer Usage Surface

**Input**: `specs/001-consumer-usage-surface/spec.md`, `plan.md`
**Tests**: required (constitution: tests first for gap-closing work).
**Format**: `[ID] [P?] [Story] Description` with exact paths. Paths verified as of 2026-09-30.

## Phase 1: Setup

- [ ] T001 Add the usage-surface requirement-to-test map to `tests/kernel/README.md` or this file's notes
- [ ] T002 [P] Add `moeka/usage.py` stub re-exporting nothing yet, with an import test in `tests/kernel/test_public_api.py` (create if absent)

## Phase 2: Foundational (blocks all stories)

- [ ] T003 Decide CLARIFY-LOG Q2 (typed attribution) and Q3 (waste label placement); record answers in `spec.md`
- [ ] T004 Add `SCHEMA_VERSION` and stamp it in `LedgerEvent.to_trace` in `nanobot/kernel/ledger.py`; snapshot test in `tests/kernel/test_usage_schema.py` (FR-007, SC-006)
- [ ] T005 Make the trace event the single source: write `LLMUsageStore` rows from the same `LedgerEvent` in `nanobot/kernel/ledger.py` and `nanobot/llm_usage/store.py` (FR-008)

## Phase 3: Consumer Story 2 - every token attributable (P1)

**Independent Test**: attribution-completeness test over all provider entry points.

- [ ] T006 [P] [US2] Failing test `tests/kernel/test_usage_attribution.py`: every `model.call` from the LLM layer, agent loop, sub-agent, memory and router carries required fields (FR-002, FR-009, SC-002)
- [ ] T007 [US2] Add typed attribution fields to `CallAttribution` and `LedgerEvent` in `nanobot/kernel/ledger.py` (FR-002)
- [ ] T008 [US2] Bind attribution in the agent turn path `nanobot/agent/runner.py` and `nanobot/agent/subagent.py` (G11, FR-009)
- [ ] T009 [P] [US2] Bind attribution for memory and dream calls in `nanobot/agent/memory.py`, `nanobot/agent/dream.py` and the router `nanobot/kernel/router.py`
- [ ] T010 [US2] Add cache-write and reasoning tokens to the event and pricing in `nanobot/kernel/ledger.py` (FR-003; see `.agent/kernel-p3-followups.md`)
- [ ] T011 [US2] Emit a usage event for a cache hit with saved tokens in `nanobot/kernel/llm.py` (`_cache_lookup`) (FR-003)

## Phase 4: Consumer Story 1 - awork shows usage from moeka (P1)

**Independent Test**: consumer script with only `moeka` imports.

- [ ] T012 [P] [US1] Failing test `tests/kernel/test_usage_query.py`: query by consumer, agent, session, role, model, time, trace id, with totals and group-by (FR-001, FR-005)
- [ ] T013 [US1] Query layer over the projection in `nanobot/llm_usage/store.py`; export through `moeka/usage.py` (FR-001)
- [ ] T014 [US1] Budget snapshot (limit, spent, reserved, remaining) on `budget.admit` and `budget.refuse` and a query in `nanobot/kernel/budget.py` (FR-004)
- [ ] T015 [US1] Refusal event carries attribution in `nanobot/kernel/budget.py` (FR-004)
- [ ] T016 [US1] Document the surface in `docs/python-sdk.md` (FR-001)

## Phase 5: Consumer Story 3 - live observation (P2)

- [ ] T017 [P] [US3] Failing test `tests/kernel/test_usage_failopen.py`: raising, blocking, lagging subscriber and failing sink/store (FR-013, SC-008)
- [ ] T018 [US3] Bounded queue and drop counter for subscribers in `nanobot/kernel/trace.py` (FR-006)
- [ ] T019 [US3] Cross-process stream interface and one host-supplied transport in `nanobot/kernel/trace.py` (FR-006)

## Phase 6: Consumer Story 4 - waste and replay (P2)

- [ ] T020 [P] [US4] Failing fixtures `tests/kernel/test_usage_waste.py` for retry, failover, avoidable miss, discarded draft, post-admission refusal (FR-010, SC-004)
- [ ] T021 [US4] Waste label and linked event in `nanobot/kernel/ledger.py`; label retries and failovers in `nanobot/kernel/llm.py` (FR-010)
- [ ] T022 [US4] Protected host-owned append-only usage sink and file-floor coverage for `llm_usage.sqlite3` in the flat layout in `nanobot/kernel/floors.py` (FR-005, SC-007)
- [ ] T023 [US4] Replay recorder and replayer keyed by `kernel.llm.request_key` in `nanobot/kernel/llm.py` (FR-011, SC-005)

## Phase 7: Consumer Story 5 - stable schema (P3)

- [ ] T024 [P] [US5] Totals-agreement property test `tests/kernel/test_usage_totals.py` (FR-008, SC-003)
- [ ] T025 [US5] OpenTelemetry GenAI mapping in `nanobot/kernel/ledger.py`; supported-versions query (FR-007)

## Phase N: Polish

- [ ] T026 Keep `usage_sink` agreement test with the new surface in `tests/api/test_complete_usage_agreement.py` (FR-014)
- [ ] T027 Update `.agent/moeka-kernel-design.md` section 3b "Current state" and gap list after each closed gap
- [ ] T028 Run the full suite (`scripts/test-docker.sh`) and the awork gate command

## Dependencies

- Phase 2 blocks all stories. US2 (attribution) precedes US1 queries by attribution fields. US4 needs T021
  before T023. T005 precedes T013 and T024.
- Parallel: T006, T009, T012, T017, T020, T024 are independent files.
