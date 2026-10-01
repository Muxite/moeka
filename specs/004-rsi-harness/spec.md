# Feature Specification: RSI Harness (pointer)

**Feature Branch**: `004-rsi-harness`
**Created**: 2026-09-30
**Status**: Draft; summary only. Nothing in the harness is built.
**Input**: The harness is designed on branch `rsi-harness-spec`, which this spec summarises and does not
replace. Read it with `git show rsi-harness-spec:.agent/rsi-harness-design.md` (spec) and
`git show rsi-harness-spec:.agent/rsi-harness-impl-plan.md` (milestones M1-M7). That branch is not edited from
here. Where this summary and the design disagree, the design wins; report the difference in
`specs/CLARIFY-LOG.md`.

## Consumer Scenarios & Testing *(mandatory)*

The harness is a consumer of the kernel (a separate repo pinning moeka as a submodule). Its own stories are
summarised here only to state what it needs from the kernel.

### Consumer Story 1 - the harness drives one kernel per rollout (Priority: P1)

The harness builds a kernel per rollout from a candidate variant, a budget and fresh registries, runs a task
under a span, and reads results and traces.

**Why this priority**: it is the harness's only use of the kernel and the base of every score.

**Independent Test**: two rollouts in one process with different variants share nothing; the result carries a
stop reason, usage, trace id and fingerprint.

**Acceptance Scenarios**:

1. **Given** a candidate variant tree, **When** a rollout runs, **Then** the rollout's events carry its rollout
   tags and the run result names the limit that ended it.
2. **Given** an offline agent, **When** it runs, **Then** web, MCP and network-declaring tools are absent.

---

### Consumer Story 2 - scoring uses trustworthy, replayable observation (Priority: P1)

Cost, waste, denial and provenance objectives are computed from a stream the mutator cannot forge, and a run
can be replayed from its recorded responses.

**Why this priority**: the acceptance rule (kernel design section 8) is only as honest as its inputs.

**Independent Test**: depends on `001-consumer-usage-surface` (FR-005, FR-010, FR-011).

**Acceptance Scenarios**:

1. **Given** a rollout that rewrites the usage SQLite file, **When** the harness scores cost, **Then** the
   score comes from the protected stream.

---

### Consumer Story 3 - the mutator edits only its candidate (Priority: P1)

A candidate changes only its variant tree (skills, prompts, tool descriptions, sampling and run knobs); the
evaluator, gate, task suite, supervisor and the pinned core are outside its write scope; held-out tests never
reach it.

**Why this priority**: it is the structural defence against evaluator tampering.

**Independent Test**: an edit outside the variant tree fails the candidate; no held-out artifact appears in
the mutator's mounted context.

**Acceptance Scenarios**:

1. **Given** a candidate that edits the runner, **When** gated, **Then** it fails and the edit is logged.

---

### Consumer Story 4 - the owner stops and resumes anytime (Priority: P2)

The daemon stops within about two minutes of notice, loses little, and resumes from the last durable row.

**Why this priority**: the owner runs it on a shared GPU, on demand.

**Independent Test**: kill and resume a no-op task through the supervisor (milestone M2).

**Acceptance Scenarios**:

1. **Given** a SIGTERM, **When** in-flight rollouts finish or flush, **Then** state is committed and the GPU
   lock released.

### Edge Cases

- A sampling parameter dropped by the provider makes a rollout non-reproducible.
- Noise larger than the effect: the gate returns "unresolved" and samples more, within a cap.
- A candidate that raises a proxy without raising pass rate is flagged as suspected gaming.
- Held-out rate limit reached: the query is refused and counted.

## Requirements *(mandatory)*

### Functional Requirements

Kernel requirements the harness imposes (K1-K6 in the harness design; not to be confused with assumptions K1-K11
in the kernel design section 14):

- **FR-001**: the kernel MUST include skill bodies in the agent fingerprint (harness K1).
- **FR-002**: the kernel MUST allow tool parameter-description overrides in a variant, never structural
  changes (harness K2).
- **FR-003**: the `tool.call` event MUST carry an argument digest (harness K3).
- **FR-004**: `RunLimits.max_tool_errors` MUST be enforced, ending a run with `stop_reason="tool_error"` at the
  cap (harness K4; constitution V).
- **FR-005**: an agent spec MUST be able to fail a run when a requested sampling parameter (such as `seed`) is
  unsupported (harness K5).
- **FR-006**: skill reads through any file-read capability MUST be observable, not only `read_file` (harness
  K6, optional).

Harness requirements (summary; the design is authoritative):

- **FR-007**: the harness MUST drive `moeka` only through its public API, one kernel per rollout, with
  explicit environment, offline agent, explicit tool allow-list from the task, and rollout identity as span
  tags.
- **FR-008**: the harness MUST pin a kernel commit as a submodule and treat it as read-only; a pin bump is its
  own commit; the pin is `core-slim` `6f80c392` until consolidation (`003`) moves it to `main`.
- **FR-009**: the mutator MUST write only inside a candidate's variant tree; the evaluator, gate, task suite,
  verifiers, supervisor and pinned core MUST be outside its write scope.
- **FR-010**: verification MUST be deterministic state-diff checks plus held-out tests; no LLM judge in any
  gate; the verifier MUST run outside the rollout container. This is constitution VIII applied.
- **FR-011**: held-out tests MUST never be visible to the agent or mutator; only coarse verdicts cross the
  boundary, rate-limited and counted.
- **FR-012**: a candidate MUST be accepted only if it passes every hard constraint (I1-V never regress; floors
  and tier-4 files untouched; held-out stays hidden), is not Pareto-dominated by its parent, and is strictly
  better on at least one objective, with ties broken toward fewer tokens, then fewer tools.
- **FR-013**: objectives MUST include held-out quality, cost per solved task, provenance rate, clarification
  yield, denial rate, latency, and wasted tokens per `001`; ambient leakage MUST be a hard zero.
  [NEEDS CLARIFICATION: clarification yield has no signal today; either the kernel emits a clarification trace
  event (with a way to mark a question as needed) or the objective is dropped until the harness can measure it;
  until decided the objective is absent from any Pareto comparison (CLARIFY-LOG Q1)]
- **FR-014**: every step MUST write durable state first; the daemon MUST stop on notice within about two
  minutes and resume from the last row.
- **FR-015**: rollouts MUST run in containers whose only network peer is the model-serving sidecar; the
  container, not the kernel's shell floor, is the isolation boundary.

### Key Entities

- **Candidate / variant tree**: the mutable unit; maps to kernel `Variant` and `AgentSpec` knobs.
- **Rollout**: one task run under one kernel, identified by span tags.
- **Archive**: variant lineage, scores, verdicts, certificates.
- **Gate**: deterministic admission rules and cascade tiers.
- **Held-out set / practice pool / locked set**.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: kernel prerequisites FR-001 to FR-005 each have a failing-then-passing test in the kernel.
- **SC-002**: the paired-margin gate is calibrated by a measured noise figure (two-seed disagreement on the
  unchanged agent) before any candidate is admitted.
- **SC-003**: the task-admission audit holds: reference solutions pass; no-op, touch-everything and `rm -rf`
  agents score 0.
- **SC-004**: no held-out artifact appears in any mutator-visible path (checked by a redaction test).
- **SC-005**: a kill and resume loses no more than the in-flight rollouts.
- **SC-006**: the verifier false-pass rate on injected bad candidates is reported each epoch.

## Assumptions

All unproven unless a number backs them (design section 16 and kernel design section 14):

- The objectives together measure usefulness; the task suite can be built; mutations transfer from the
  practice pool to unseen tasks and real use; evaluation noise can be resolved with the available task count.
- A single RTX 5070 Ti and a local served model are the compute; vLLM on this card is unverified.
- Scope is tier-1 mutation (skills, prompts, tool descriptions, sampling and run knobs); core and tool code
  mutation is deferred.
