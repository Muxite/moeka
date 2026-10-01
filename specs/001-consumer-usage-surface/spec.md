# Feature Specification: Consumer Usage Surface

**Feature Branch**: `001-consumer-usage-surface`
**Created**: 2026-09-30
**Status**: Draft
**Input**: Owner rulings 2026-09-30: "awork should derive its usage figures from moeka"; "everything is data";
better or paid models are acceptable when observation is so well tested that no token goes to waste.
Source requirements U1-U12 and gaps G1-G12 are in `.agent/moeka-kernel-design.md` section 3b; this spec
restates them as testable requirements and does not replace that section.

## Consumer Scenarios & Testing *(mandatory)*

Consumers: awork (private dogfood), the live gateway, the RSI harness, a third-party embedder.

### Consumer Story 1 - awork shows usage and spend from moeka alone (Priority: P1)

awork renders per-stage, per-agent usage, spend and remaining budget in its own interface using only moeka's
public API, with no ledger of its own.

**Why this priority**: it is the owner's stated direction and the reason the surface exists; it resolves awork
PRD D7 in moeka's favour.

**Independent Test**: run a scripted multi-call session (completions, a retried call, a batch, an agent run)
against a fake provider; a consumer script using only `moeka` imports reproduces the per-consumer, per-agent
and per-session totals and the remaining budget.

**Acceptance Scenarios**:

1. **Given** a kernel with a capped budget and two agents under one consumer, **When** each runs calls,
   **Then** a consumer query by agent returns tokens and USD per agent and a separate query returns the
   remaining budget.
2. **Given** a refused call, **When** the consumer queries, **Then** the refusal is a recorded event with the
   same attribution as an admitted call.

---

### Consumer Story 2 - every token is attributable (Priority: P1)

Any consumer can account for 100% of tokens the kernel caused to be billed or computed: each is tied to a call,
a purpose and a consumer.

**Why this priority**: it is the precondition for allowing better or paid models (owner ruling) and the base of
every other story.

**Independent Test**: an attribution-completeness test drives every provider entry point (LLM layer, agent
turn loop, sub-agents, memory and dream calls, router) and fails if any call lacks the required attribution.

**Acceptance Scenarios**:

1. **Given** an agent run with a sub-agent and a memory consolidation call, **When** it ends, **Then** every
   physical call event carries consumer, agent, session, role or slot, call id and attempt.
2. **Given** a call made outside the LLM layer, **When** observed, **Then** it is either attributed or the test
   suite fails.

---

### Consumer Story 3 - live observation without risk to the call (Priority: P2)

The live gateway or a dashboard subscribes to usage events as they happen, in-process or across processes, and
a slow or broken subscriber never delays or fails a model call.

**Why this priority**: the gateway is a long-running process with a separate UI consumer.

**Independent Test**: a subscriber that raises, blocks or lags while calls proceed; call latency and outcomes
are unchanged and the dropped-event count is reported.

**Acceptance Scenarios**:

1. **Given** a stalled subscriber, **When** 1000 calls run, **Then** no call fails or waits on it.
2. **Given** a second process subscribed, **When** calls occur, **Then** it receives the same events.

---

### Consumer Story 4 - RSI harness scores waste and replays runs (Priority: P2)

The harness reads wasted-token metrics per scope as objectives and gaming detectors, trusts a source the
mutator cannot forge, and replays a recorded run offline to reproduce verdicts, cache behaviour and cost.

**Why this priority**: kernel design section 8 objectives depend on it; K11 is unproven without it.

**Independent Test**: fixtures that produce each waste category (retry, failover, avoidable cache miss,
discarded draft, post-admission refusal) are labelled and summed; a recorded run replays with no network and
equal figures.

**Acceptance Scenarios**:

1. **Given** a verifier rejecting a draft that a second call supersedes, **When** totals are queried, **Then**
   the first call's tokens and USD appear under "discarded draft".
2. **Given** an exec-capable agent that rewrites the usage SQLite file, **When** the harness reads totals,
   **Then** the figures from the protected source are unchanged.

---

### Consumer Story 5 - third-party embedder reads a stable schema (Priority: P3)

An embedder writes a usage dashboard once; additive kernel changes do not break it and breaking changes are
announced by a version.

**Why this priority**: it hardens the surface after the first consumers work.

**Independent Test**: a schema-snapshot test fails on a removed or re-meant field without a major bump; an
export in OpenTelemetry GenAI naming loads in a standard viewer.

**Acceptance Scenarios**:

1. **Given** an event, **When** read, **Then** it carries `schema_version`, and the kernel reports the versions
   it speaks.
2. **Given** a stream total and a stored total for the same filter, **When** compared, **Then** they are equal.

### Edge Cases

- A call that fails before any token is billed: still an event with zero tokens and its error kind.
- A provider that reports no usage: the event flags tokens as estimated, never as billed.
- A response-cache hit: an event with zero billed tokens and the tokens it saved.
- A call during a store outage: the call succeeds; the loss is counted and surfaced.
- Concurrent kernels sharing one usage store: closing one never closes the other's store.
- A consumer reads totals while writes are in flight: reads are consistent per query.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** (U1; closes G1): moeka MUST expose one public, documented usage API in the `moeka` package;
  a consumer MUST NOT need `nanobot.*` imports or direct SQLite reads to read usage for its own calls.
- **FR-002** (U2; closes G2): every physical call event MUST carry consumer, agent, session, role or slot,
  call id and attempt number, set by the caller through typed fields and never inferred from free-form tags.
  Decided (Q2, option A): typed `consumer`, `agent`, `session`, `role` and `purpose` on the event, the store
  and every query; `tags` stays beside them for host-defined extras and never carries these five.
- **FR-003** (U3; closes G3): each call event MUST state tokens in, tokens out, cache-read tokens, cache-write
  tokens, reasoning tokens where the provider reports them, model, provider, tier, latency, cost in USD with a
  billed-or-estimated flag, finish reason, and whether a response-cache hit answered it. A hit MUST be an
  event with zero billed tokens and the tokens it saved. Events MUST NOT contain prompt or response text.
- **FR-004** (U4; closes G4): moeka MUST report, per scope, the budget in force (limit, spent, reserved,
  remaining, in USD and tokens) on each event and through a separate query. A refusal MUST be a recorded
  event with the same attribution as an admitted call, not only an exception.
- **FR-005** (U5; closes G1, G10): usage MUST be queryable by consumer, agent, session, role, model, time range
  and trace id, with totals and group-by. The store MUST be durable across restarts, readable by the consumer,
  and not rewritable by the agent under observation.
- **FR-006** (U6; closes G5): a consumer MUST be able to subscribe to the same events live, in-process and
  across processes, with backpressure that never blocks or fails a model call and a visible dropped-event
  count.
- **FR-007** (U7; closes G6): every event and query result MUST carry a `schema_version`; additive changes keep
  the major number, removals or meaning changes bump it, and a consumer MUST be able to ask which versions the
  kernel speaks. The schema MUST be exportable in OpenTelemetry GenAI naming.
- **FR-008** (U8; closes G7): totals computed from the event stream MUST equal stored totals for the same
  filter, enforced by a test.
- **FR-009** (U9; closes G11): every token MUST be attributable to a call, a purpose (slot or role plus task
  type) and a consumer; an unattributed token MUST fail a test. This includes calls from the agent turn loop,
  sub-agents, memory and dream, and the router.
- **FR-010** (U10; closes G8): moeka MUST report, per scope, tokens and USD spent on retries of a failed or
  malformed call, failover attempts, avoidable cache misses, discarded drafts, and calls refused after
  admission. Waste MUST be a label set by the caller or kernel at the time known, not a later estimate.
  Decided (Q3, option C): kernel-known cases (retry, failover, post-admission refusal) are a `waste_label`
  field on the event at emission; caller-known cases (discarded draft) are a separate append-only `call.waste`
  event keyed by `call_id`. The query layer joins them; past events are never mutated.
- **FR-011** (U11; closes G9): a trace stream plus recorded model responses MUST be enough to replay a run and
  reproduce verdicts, cache behaviour and cost figures without network access. Recordings are a separate,
  host-owned, opt-in artifact; usage events stay content-free (FR-003).
- **FR-012** (U12): attribution completeness, schema stability, totals agreement (FR-008) and waste labelling
  MUST each have tests that fail on drift.
- **FR-013**: observation MUST be fail-open: a broken sink, store or subscriber never fails, delays or alters a
  model call; the failure is counted and logged.
- **FR-014**: the legacy per-turn `usage_sink` payload MUST remain available until `002-kernel-api-for-consumers`
  removes it, and its figures MUST agree with the new surface for the same calls.
- **FR-015**: the stop contract for budget refusals (G12) is specified in `002-kernel-api-for-consumers`
  (FR-008 there) and MUST be consistent with FR-004 here.

### Key Entities

- **Usage event**: one physical call attempt; attribution, token and cost figures, flags, waste label,
  budget snapshot, schema version. Content-free.
- **Attribution**: consumer, agent, session, role or slot, call id, attempt, trace id.
- **Budget scope**: limit, spent, reserved, remaining for a scope (kernel, consumer, tag).
- **Waste label**: category, the call it applies to, who set it.
- **Replay recording**: host-owned record of responses keyed by request key.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: a consumer script importing only `moeka` reproduces per-agent and per-session tokens and USD for
  a scripted session with zero difference from the event stream.
- **SC-002**: an attribution-completeness test over every provider entry point finds 0 events with a missing
  required attribution field.
- **SC-003**: for at least 100 randomised filters, stream total equals stored total exactly.
- **SC-004**: each of the five waste categories has a fixture whose labelled tokens and USD equal the known
  injected waste exactly.
- **SC-005**: a recorded run replays offline and reproduces stop reason, cache hit count and total cost
  equal to the original run.
- **SC-006**: removing or re-meaning any field in the schema snapshot without a major bump fails the build.
- **SC-007**: with the usage SQLite file rewritten by an exec-capable agent, the consumer-visible totals from
  the protected source are unchanged.
- **SC-008**: with a failing sink, store and subscriber, 1000 calls complete with the same results and a
  non-zero reported loss count.
- **SC-009**: awork's usage view has no ledger code of its own after migration (checked by awork's own
  review, tracked in `002`).

## Decisions (2026-10-01)

Resolved with the CLARIFY-LOG recommended defaults, after checking the code (nothing in it argued otherwise).
Provisional until the owner answers; the owner may veto.

- **Q2 typed attribution**: option A. Canonical fields are in `schemas/usage-record.v1.schema.json`
  (`consumer`, `agent`, `session`, `role`, `purpose`; `consumer` required). The SQLite store has columns for
  them because it has no tags column.
- **Q3 waste label**: option C (see FR-010).
- **Q4 stop contract**: option A, in `002` FR-008: the kernel documents one stop-contract table and ships no
  `BaseException` type. The refusal itself is a recorded `budget-event.v1` with a typed `refusal.code`.
- **Schemas**: `schemas/` holds the canonical `usage-record.v1`, `budget-event.v1` and
  `complete-json-call.v1`; the kernel's `model.call`, `cache.hit` and `budget.*` events are those documents
  (plus the `event` key and the legacy `cached` alias).

## Assumptions

- The owner rulings of 2026-09-30 stand; awork is the first consumer and awork-resume is out of scope (it keeps
  `awr.llm.ledger`).
- Local-tier calls cost `0.0` by convention; token caps are then the enforced limit.
- The host owns durable locations and chooses the protected source for FR-005.
- Hidden reasoning tokens and cache-write premiums require provider-reported fields; where a provider does not
  report them the event says so rather than guessing.
- Better or paid models are not enabled by this spec; it only meets the precondition.
