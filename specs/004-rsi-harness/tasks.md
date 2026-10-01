---
description: "Kernel-side task list for the RSI harness (pointer)"
---

# Tasks: RSI Harness (kernel prerequisites only)

**Input**: `specs/004-rsi-harness/spec.md`, `plan.md`. The harness milestones M1-M7 are tracked in
`rsi-harness-impl-plan.md` on branch `rsi-harness-spec` and are not copied here. Paths as of 2026-09-30.

## Phase 1: Setup

- [x] T001 Decide CLARIFY-LOG Q1 (clarification-yield event or drop); record in `spec.md` (FR-013). Owner, 2026-10-01: B
- [~] T002 [P] Pin. Superseded by the consolidation: K1-K6 and the held-out runner exist only on `consolidate/new-main`
  (merges `d1766eb2`, `b67c795f`), not on `core-slim` or the cut `main` (`d3524a79`). The local-only harness pins a
  `consolidate/new-main` commit with a green `scripts/test-docker.sh` run; after the G7 push it re-pins to `main`

## Phase 2: Kernel prerequisites (each its own commit and test, tests first)

- [x] T003 [P] [US1] K5: add `on_unsupported` to `AgentSpec`; run ends `stop_reason="error"` with `UnsupportedRequestError`; `nanobot/kernel/agent.py`, `tests/kernel/test_agent.py` (FR-005)
- [x] T004 [P] [US2] K3: add `args_digest` (sha256 of canonical arguments JSON) to `tool.call` in `nanobot/kernel/trace_hook.py`; update `EVENTS` in `nanobot/kernel/trace.py`; test in `tests/kernel/test_trace_spans.py` (FR-003)
- [x] T005 [US1] K1: add a `skills` component to the fingerprint in `nanobot/kernel/variants.py`; test in `tests/kernel/test_variants.py` (FR-001)
- [x] T006 [P] [US3] K2: add `Variant.tool_param_descriptions`, rejecting structural keys, in `nanobot/kernel/variants.py` (FR-002)
- [x] T007 [P] [US1] K4: implement `RunLimits.max_tool_errors` at `nanobot/kernel/agent.py:693` and the runner ceiling in `nanobot/agent/runner.py`; stop reason `tool_error`; update the I5 text in the design (FR-004)
- [x] T008 [US2] K6 (optional): emit `skill.read` with `via=<tool>` from any `fs.read` check on a `SKILL.md` in `nanobot/kernel/gate.py` (FR-006)

## Phase 3: Observation the harness depends on

- [~] T009 [US2] `001` T020-T023 (waste labels, protected sink, replay) no longer block harness M4. The plan's M4
  needs only K1 and K3; the harness covers the rest outside the kernel until `001` lands:
  - Protected cost stream: a metering proxy in front of the model server, in the supervisor's trust zone (not
    the runner container), logs per-request usage keyed by rollout tags. Cost is scored from the proxy log;
    kernel usage rows are cross-checked against it and any difference flags the rollout. The runner's file tools
    cannot reach the proxy log (it is not mounted).
  - Replay: not needed for M4. Resume uses M3's result cache (task, seed, fingerprint, variant tree hash, core
    commit); the proxy can record responses by request digest when replay is wanted.
  - Wasted tokens: derived in `supervisor/metrics.py` from the proxy log (repeated request digests = retries)
    and the trace; any part that cannot be derived is marked unavailable, which M4's exit criterion allows.
  - Switch to the kernel sink and replayer once `001` T022 and T023 land; keep the proxy as the cross-check

## Phase 4: Clarification yield (after T001)

- [ ] T010 [US2] Emit `clarify.question` and `clarify.commit_ready` (paths and reasons, never values) from `nanobot/kernel/clarify.py` or the facade; no "needed" mark in the kernel (Q1 = B, the harness judges it); test in `tests/kernel/test_clarify.py`
- [-] T011 [US2] Not applicable (Q1 = B, objective kept). Was: if dropped: remove clarification yield from kernel design section 8 and `rsi-harness-design.md` on its branch via the owner's separate instruction

## Phase 5: Held-out spec testing (FR-016, SC-007)

Done by spec `007-heldout-runner` (`scripts/heldout_run.py`, `scripts/run-heldout.sh`; accepted suite
`tests/heldout/007-heldout-runner/`). T015 needed no kernel API: the harness vendors or calls the runner.


- [x] T013 Runner `heldout-run <repo> <feature> <worktree>`: copy the held-out suite into a throwaway copy of the tree, run under `timeout`, report per-requirement pass/fail counts only (FR-016)
- [x] T014 Redaction test on the runner's feedback: no test names, assertion text, inputs or held-out paths; held-out dir not mounted into the implementer or mutator container (SC-007)
- [x] T015 Candidate kernel helper (owner decision, see kernel design section 8): `heldout` evaluation that runs hidden checks in an isolated copy and returns redacted per-requirement results

## Phase N: Polish

- [ ] T012 Update the harness design's prerequisite status lines (on its own branch, owner's instruction) and the kernel design section 14 after each landing

## Dependencies

- T005 and T004 are needed by harness M4; T003 by M3; T006 and T007 by M5. T009 no longer gates M4 (proxy metering). T010 gates the
clarification-yield objective only.
