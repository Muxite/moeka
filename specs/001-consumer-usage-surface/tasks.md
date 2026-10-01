---
description: "Task list for consumer usage surface"
---

# Tasks: Consumer Usage Surface

**Input**: `specs/001-consumer-usage-surface/spec.md`, `plan.md`
**Tests**: required (constitution: tests first for gap-closing work).
**Format**: `[ID] [P?] [Story] Description` with exact paths. Paths verified as of 2026-09-30.

## Phase 1: Setup

- [ ] T001 Add the usage-surface requirement-to-test map to `tests/kernel/README.md` or this file's notes
  Status 2026-10-01: not done
- [x] T002 [P] Add `moeka/usage.py` stub re-exporting nothing yet, with an import test in `tests/kernel/test_public_api.py` (create if absent)
  Status 2026-10-01: `moeka/usage.py`; surface snapshot in `tests/moeka/test_public_surface.py`

## Phase 2: Foundational (blocks all stories)

- [x] T003 Decide CLARIFY-LOG Q2 (typed attribution) and Q3 (waste label placement); record answers in `spec.md`
  Status 2026-10-01: Q2 A, Q3 C, Q4 A recorded in `spec.md` Decisions
- [x] T004 Add `SCHEMA_VERSION` and stamp it in `LedgerEvent.to_trace` in `nanobot/kernel/ledger.py`; snapshot test in `tests/kernel/test_usage_schema.py` (FR-007, SC-006)
  Status 2026-10-01: `SCHEMA_VERSION = "1.0"` in `ledger.py`, stamped by `to_trace`; field snapshot in `tests/schemas/test_schemas.py`
- [x] T005 Make the trace event the single source: write `LLMUsageStore` rows from the same `LedgerEvent` in `nanobot/kernel/ledger.py` and `nanobot/llm_usage/store.py` (FR-008)
  Status 2026-10-01: `LLMUsageStore.record_event` projects the row from the same `LedgerEvent`; `cache.hit`/`budget.refuse`/`call.waste` projected by `StoreProjection` in `nanobot/kernel/usage.py`

## Phase 3: Consumer Story 2 - every token attributable (P1)

**Independent Test**: attribution-completeness test over all provider entry points.

- [~] T006 [P] [US2] Failing test `tests/kernel/test_usage_attribution.py`: every `model.call` from the LLM layer, agent loop, sub-agent, memory and router carries required fields (FR-002, FR-009, SC-002)
  Status 2026-10-01: `tests/kernel/test_usage_surface.py` covers the LLM layer, agent loop (with and without budget) and direct provider calls; sub-agents, memory/dream and router are NOT covered
- [x] T007 [US2] Add typed attribution fields to `CallAttribution` and `LedgerEvent` in `nanobot/kernel/ledger.py` (FR-002)
  Status 2026-10-01: `Attribution`, `CallAttribution.attribution`, `LedgerEvent` fields, `GenerateOptions.attribution`, `Kernel(consumer=)`
- [~] T008 [US2] Bind attribution in the agent turn path `nanobot/agent/runner.py` and `nanobot/agent/subagent.py` (G11, FR-009)
  Status 2026-10-01: agent run binds consumer/agent/session via `bind_attribution`; sub-agents inherit the parent's context, with no own agent name
- [ ] T009 [P] [US2] Bind attribution for memory and dream calls in `nanobot/agent/memory.py`, `nanobot/agent/dream.py` and the router `nanobot/kernel/router.py`
  Status 2026-10-01: not done: memory, dream and router calls take the ambient attribution only
- [~] T010 [US2] Add cache-write and reasoning tokens to the event and pricing in `nanobot/kernel/ledger.py` (FR-003; see `.agent/kernel-p3-followups.md`)
  Status 2026-10-01: cache-write tokens on the event and store; `tokens_reasoning` is null (providers do not report it); cache writes are still priced at the input price
- [x] T011 [US2] Emit a usage event for a cache hit with saved tokens in `nanobot/kernel/llm.py` (`_cache_lookup`) (FR-003)
  Status 2026-10-01: `cache.hit` carries attribution, `saved_tokens_in/out`, `saved_cost_usd`; stored in `llm_usage_events`

## Phase 4: Consumer Story 1 - awork shows usage from moeka (P1)

**Independent Test**: consumer script with only `moeka` imports.

- [x] T012 [P] [US1] Failing test `tests/kernel/test_usage_query.py`: query by consumer, agent, session, role, model, time, trace id, with totals and group-by (FR-001, FR-005)
  Status 2026-10-01: `tests/kernel/test_usage_surface.py`
- [x] T013 [US1] Query layer over the projection in `nanobot/llm_usage/store.py`; export through `moeka/usage.py` (FR-001)
  Status 2026-10-01: `nanobot/llm_usage/query.py`; `kernel.usage.totals/total/records`; `moeka.usage`
- [x] T014 [US1] Budget snapshot (limit, spent, reserved, remaining) on `budget.admit` and `budget.refuse` and a query in `nanobot/kernel/budget.py` (FR-004)
  Status 2026-10-01: `CapBudget.snapshot()`; `budget.admit/refuse` carry `budget-event.v1` figures; `kernel.usage.budget()`; a custom `Budget` without `snapshot()` reports null figures
- [x] T015 [US1] Refusal event carries attribution in `nanobot/kernel/budget.py` (FR-004)
  Status 2026-10-01: refusal carries attribution and a typed `refusal.code`; stored as a `refusal` usage event
- [x] T016 [US1] Document the surface in `docs/python-sdk.md` (FR-001)
  Status 2026-10-01: `docs/python-sdk.md` Usage section

## Phase 5: Consumer Story 3 - live observation (P2)

- [~] T017 [P] [US3] Failing test `tests/kernel/test_usage_failopen.py`: raising, blocking, lagging subscriber and failing sink/store (FR-013, SC-008)
  Status 2026-10-01: raising, blocked (bounded queue) subscribers and a failing store are tested; the legacy sync `Tracer.subscribe` is unchanged
- [x] T018 [US3] Bounded queue and drop counter for subscribers in `nanobot/kernel/trace.py` (FR-006)
  Status 2026-10-01: `kernel.usage.subscribe` (own thread, bounded queue, `dropped`/`errors`, `loss()`)
- [ ] T019 [US3] Cross-process stream interface and one host-supplied transport in `nanobot/kernel/trace.py` (FR-006)
  Status 2026-10-01: not done: no cross-process stream

## Phase 6: Consumer Story 4 - waste and replay (P2)

- [~] T020 [P] [US4] Failing fixtures `tests/kernel/test_usage_waste.py` for retry, failover, avoidable miss, discarded draft, post-admission refusal (FR-010, SC-004)
  Status 2026-10-01: retry and discarded draft are tested; failover, avoidable cache miss and post-admission refusal are NOT labelled
- [~] T021 [US4] Waste label and linked event in `nanobot/kernel/ledger.py`; label retries and failovers in `nanobot/kernel/llm.py` (FR-010)
  Status 2026-10-01: `waste_label` on `model.call` (retry = attempt > 1) and `kernel.usage.label_waste` -> `call.waste`; failover and post-admission labels pending
- [ ] T022 [US4] Protected host-owned append-only usage sink and file-floor coverage for `llm_usage.sqlite3` in the flat layout in `nanobot/kernel/floors.py` (FR-005, SC-007)
  Status 2026-10-01: not done: protected host-owned sink and flat-layout file floor (SC-007 unproven)
- [ ] T023 [US4] Replay recorder and replayer keyed by `kernel.llm.request_key` in `nanobot/kernel/llm.py` (FR-011, SC-005)
  Status 2026-10-01: not done: replay recorder (SC-005 unproven)

## Phase 7: Consumer Story 5 - stable schema (P3)

- [x] T024 [P] [US5] Totals-agreement property test `tests/kernel/test_usage_totals.py` (FR-008, SC-003)
  Status 2026-10-01: 100 randomised filters, stream equals stored (calls, tokens, cost, cache hits, saved); in `test_usage_surface.py`
- [~] T025 [US5] OpenTelemetry GenAI mapping in `nanobot/kernel/ledger.py`; supported-versions query (FR-007)
  Status 2026-10-01: `schema_versions()` done; OpenTelemetry GenAI export NOT done (mapping documented in `schemas/ATTRIBUTION.md`)

## Phase N: Polish

- [~] T026 Keep `usage_sink` agreement test with the new surface in `tests/api/test_complete_usage_agreement.py` (FR-014)
  Status 2026-10-01: token-figure equivalence incl. cache write tested in `test_usage_surface.py::test_usage_sink_figures_agree_with_the_event` (not via the `nanobot.api` path)
- [x] T027 Update `.agent/moeka-kernel-design.md` section 3b "Current state" and gap list after each closed gap
  Status 2026-10-01: design section 3b updated
- [x] T028 Run the full suite (`scripts/test-docker.sh`) and the awork gate command
  Status 2026-10-01: `scripts/test-docker.sh pytest -n4`: 6259 passed, 53 skipped (2026-10-01); the awork gate command was not run

## Dependencies

- Phase 2 blocks all stories. US2 (attribution) precedes US1 queries by attribution fields. US4 needs T021
  before T023. T005 precedes T013 and T024.
- Parallel: T006, T009, T012, T017, T020, T024 are independent files.
