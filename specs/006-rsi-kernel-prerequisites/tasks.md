---
description: "Task list for 006-rsi-kernel-prerequisites (harness K1-K6)"
---

# Tasks: RSI Kernel Prerequisites

**Input**: `specs/006-rsi-kernel-prerequisites/spec.md`, `plan.md`. Paths as of 2026-10-01 (`cd03700e`).

**Prerequisites**: implementer works in its own worktree cut from `consolidate/new-main` (never
`~/projects/moeka`). Held-out tests live in `~/projects/.heldout/moeka/006-rsi-kernel-prerequisites/`; the
implementer MUST NOT read that directory. Feedback arrives as failing FR/SC ids with counts only.

**Tests**: test-first for every item (constitution, gap-closing work). The implementer writes its own tests under
`tests/`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency)
- **[Story]**: US1 fingerprint, US2 limits and digest, US3 strict sampling, US4 parameter descriptions, US5 skill reads

## Phase 1: Setup

- [ ] T001 Record the base sha and a green `scripts/test-docker.sh` baseline in the implementer report.
- [ ] T002 [P] Tester: write the hidden suite from spec.md only, one file per K item, every test marked
  `@pytest.mark.fr(...)`, plus `SPEC-MAP.md` and `RUN.md`, under `~/projects/.heldout/moeka/006-rsi-kernel-
  prerequisites/` (mode 0700). Baseline it against the unchanged tree (expected: most K tests fail).

## Phase 2: User Story 2 - digest and tool-error ceiling (P1)

- [ ] T003 [P] [US2] K3 tests in `tests/kernel/test_trace_spans.py`: canonicalisation cases of FR-015 (dict order,
  JSON string, unparsable string, `None`, `NaN`, non-JSON type), presence on all five `tool.call` kinds (FR-017),
  equality with `moeka.trace.args_digest` (FR-016), no raw arguments (FR-018).
- [ ] T004 [US2] K3 implementation: `args_digest()` in `nanobot/kernel/trace_hook.py`, wired in `_tool_event`;
  `EVENTS["tool.call"]` in `nanobot/kernel/trace.py`; export from `moeka/trace.py` (FR-014 to FR-019, FR-040).
- [ ] T005 [P] [US2] K4 tests in `tests/kernel/test_agent.py`: replace `test_max_tool_errors_not_implemented`
  (`:137`); cases for FR-020 to FR-026 (counting kinds, ask_user exclusion, per-run reset, all calls of the
  response executed, precedence, exact error string, stream `run.failed`, `None` unchanged).
- [ ] T006 [US2] K4 implementation: `AgentRunSpec.max_tool_errors` and counter in `nanobot/agent/runner.py`;
  plumbing in `nanobot/agent/loop.py` and `nanobot/kernel/agent.py` (`_build`, near `max_policy_denials` at `:824`);
  remove the raise at `:694-697`.

## Phase 3: User Story 3 - strict sampling (P1)

- [ ] T007 [P] [US3] K5 tests in `tests/kernel/test_agent.py` and `tests/kernel/test_llm_engine.py`: raise/drop,
  zero provider calls, `fields` order, defaults quiet under raise, pass-through fails closed under raise only,
  fallback primary rule, `ModelSpec.unsupported_sampling` on both agent and `kernel.llm.complete` paths, validation.
- [ ] T008 [US3] K5 implementation: `AgentSpec.on_unsupported` (`nanobot/kernel/agent.py`), `_request_extras`;
  `ModelSpec.unsupported_sampling` (`nanobot/kernel/hostenv.py`); fail-closed pass-through in
  `nanobot/providers/base.py` `_apply_request_extras`; kernel-side pre-check in `nanobot/kernel/llm.py` (FR-028 to
  FR-033). Pending owner answer to the FR-033 marker; the recommended default is to build it.
- [ ] T009 [P] [US3] Docs for FR-034: `AgentSpec` docstring and `docs/python-sdk.md` state the undetectable case.

## Phase 4: User Story 1 - skills in the fingerprint (P1)

- [ ] T010 [P] [US1] K1 tests in `tests/kernel/test_variants.py`: exact recomputation of FR-003/FR-004 on a temp
  tree, location independence, exclusions (`__pycache__`, dot paths, `.pyc`), include/exclude filters, inline skills,
  unreadable file raises `OSError`, no `skill.read` during fingerprint.
- [ ] T011 [US1] K1 implementation in `nanobot/kernel/variants.py` (`fingerprint`, `Fingerprint` docstring) (FR-001
  to FR-006).

## Phase 5: User Story 4 - parameter descriptions (P2)

- [ ] T012 [P] [US4] K2 tests in `tests/kernel/test_variants.py`: grammar valid/invalid lists of FR-008, resolution
  and "only descriptions change" (FR-009), visibility in `Agent.tools()` and the provider request (FR-010), two
  kernels isolated, unknown tool ignored and bad path raises `VariantError` (FR-011), MCP/actions untouched (FR-012),
  validation unchanged (FR-013).
- [ ] T013 [US4] K2 implementation: `Variant.tool_param_descriptions` (`nanobot/kernel/variants.py`), application in
  `nanobot/agent/tools/loader.py` `_apply_variant`, per-instance parameters override in
  `nanobot/agent/tools/base.py`, `VariantError` in `moeka/errors.py`.

## Phase 6: User Story 5 - skill reads (P3)

- [ ] T014 [P] [US5] K6 tests in `tests/kernel/test_trace_spans.py` (extend `test_skill_read_from_read_file`, `:525`):
  `read_file` success and failure, `grep` content vs other modes, one event per (call, file), ordering before
  `tool.call`, `call_id` join, non-skill files.
- [ ] T015 [US5] K6 implementation: tool-call-id context for every tool call in `nanobot/agent/runner.py`;
  `nanobot/agent/tools/filesystem.py` (emit after success, `via`, `call_id`); `nanobot/agent/tools/search.py` grep;
  `EVENTS["skill.read"]` lower-bound text (FR-035 to FR-040).

## Phase 7: Polish and acceptance

- [ ] T016 Docs: `.agent/moeka-kernel-design.md` I5 lists the tool-error ceiling (FR-027); K1-K6 status in
  `specs/004-rsi-harness/plan.md` and tasks T003-T008 ticked; the harness design status lines on its own branch only
  by the owner's instruction (`004` T012).
- [ ] T017 Run `scripts/test-docker.sh` and `scripts/run-heldout.sh 005-multi-instance` (FR-043, SC-007).
- [ ] T018 Head: run the hidden suite with `heldout-run` (spec 007) or the 005 RUN.md procedure; triage failures
  against this spec; tester bugs go back to the tester; round cap 4. On acceptance commit the suite to
  `tests/heldout/006-rsi-kernel-prerequisites/` and check `scripts/run-heldout.sh 006-rsi-kernel-prerequisites`.

## Dependencies

- T003-T004 and T010-T011 unblock harness M4; T007-T008 M3; T005-T006 and T012-T013 M5; T014-T015 optional for M5.
- Items are independent of each other except T015 (call-id context) touches `runner.py` like T006: land T006 first.
