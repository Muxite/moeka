---
description: "Kernel-side task list for the RSI harness (pointer)"
---

# Tasks: RSI Harness (kernel prerequisites only)

**Input**: `specs/004-rsi-harness/spec.md`, `plan.md`. The harness milestones M1-M7 are tracked in
`rsi-harness-impl-plan.md` on branch `rsi-harness-spec` and are not copied here. Paths as of 2026-09-30.

## Phase 1: Setup

- [ ] T001 Decide CLARIFY-LOG Q1 (clarification-yield event or drop); record in `spec.md` (FR-013)
- [ ] T002 [P] Confirm the pin `6f80c392` is on `origin/core-slim` with `git ls-remote` before any bump

## Phase 2: Kernel prerequisites (each its own commit and test, tests first)

- [ ] T003 [P] [US1] K5: add `on_unsupported` to `AgentSpec`; run ends `stop_reason="error"` with `UnsupportedRequestError`; `nanobot/kernel/agent.py`, `tests/kernel/test_agent.py` (FR-005)
- [ ] T004 [P] [US2] K3: add `args_digest` (sha256 of canonical arguments JSON) to `tool.call` in `nanobot/kernel/trace_hook.py`; update `EVENTS` in `nanobot/kernel/trace.py`; test in `tests/kernel/test_trace_spans.py` (FR-003)
- [ ] T005 [US1] K1: add a `skills` component to the fingerprint in `nanobot/kernel/variants.py`; test in `tests/kernel/test_variants.py` (FR-001)
- [ ] T006 [P] [US3] K2: add `Variant.tool_param_descriptions`, rejecting structural keys, in `nanobot/kernel/variants.py` (FR-002)
- [ ] T007 [P] [US1] K4: implement `RunLimits.max_tool_errors` at `nanobot/kernel/agent.py:693` and the runner ceiling in `nanobot/agent/runner.py`; stop reason `tool_error`; update the I5 text in the design (FR-004)
- [ ] T008 [US2] K6 (optional): emit `skill.read` with `via=<tool>` from any `fs.read` check on a `SKILL.md` in `nanobot/kernel/gate.py` (FR-006)

## Phase 3: Observation the harness depends on

- [ ] T009 [US2] Track `001` tasks T020-T023 (waste labels, protected sink, replay) as blockers for harness M4

## Phase 4: Clarification yield (after T001)

- [ ] T010 [US2] If kept: emit `clarify.question` and `clarify.commit_ready` (paths and reasons, never values) from `nanobot/kernel/clarify.py` or the facade, with a "needed" mark; test in `tests/kernel/test_clarify.py`
- [ ] T011 [US2] If dropped: remove clarification yield from kernel design section 8 and `rsi-harness-design.md` on its branch via the owner's separate instruction

## Phase 5: Held-out spec testing (FR-016, SC-007)

- [ ] T013 Runner `heldout-run <repo> <feature> <worktree>`: copy the held-out suite into a throwaway copy of the tree, run under `timeout`, report per-requirement pass/fail counts only (FR-016)
- [ ] T014 Redaction test on the runner's feedback: no test names, assertion text, inputs or held-out paths; held-out dir not mounted into the implementer or mutator container (SC-007)
- [ ] T015 Candidate kernel helper (owner decision, see kernel design section 8): `heldout` evaluation that runs hidden checks in an isolated copy and returns redacted per-requirement results

## Phase N: Polish

- [ ] T012 Update the harness design's prerequisite status lines (on its own branch, owner's instruction) and the kernel design section 14 after each landing

## Dependencies

- T005 and T004 are needed by harness M4; T003 by M3; T006 and T007 by M5. T009 gates M4 scoring on waste.
