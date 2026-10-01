# Feature Specification: Kernel API for Consumers

**Feature Branch**: `002-kernel-api-for-consumers`
**Created**: 2026-09-30
**Status**: Draft
**Input**: Replace the legacy `nanobot.api` entry points and the `MoekaCore` facade with the public `moeka`
package as the only surface consumers use; carry awork's needs as listed in `docs/migration-moeka-api.md`.
Source: `.agent/moeka-kernel-design.md` sections 3a, 10 (awork compatibility), 15; system contract section 3a.

## Consumer Scenarios & Testing *(mandatory)*

### Consumer Story 1 - awork migrates off the legacy entry points (Priority: P1)

awork replaces every call to `nanobot.api.complete*`, `MoekaCore`, and `open_vec_store` with `moeka.Kernel`,
keeping its behaviour (spend caps, caching, scoped research agent, corpus search) and its test suite green.

**Why this priority**: awork is the only consumer still on the legacy surface, and legacy removal is blocked on
it.

**Independent Test**: awork's backend test suite, run against the migrated commit, passes with the legacy
modules made unimportable; a grep of awork finds no `nanobot.` import outside `moeka` and its own shim.

**Acceptance Scenarios**:

1. **Given** awork's `moeka_config(secrets)`, **When** replaced by `Environment.for_host`, **Then** the same
   provider and model run with prices so cost and caps work, and no environment variable is read.
2. **Given** a spend cap reached mid-batch, **When** the refusal surfaces, **Then** awork's best-effort handlers
   cannot swallow it (stop contract, FR-008).
3. **Given** the research agent, **When** built from its profile, **Then** limits, memory and tool allow-list
   behave as the migration page states, with no surprising `ValueError`.

---

### Consumer Story 2 - a third-party embedder uses one documented API (Priority: P2)

An embedder installs moeka, builds a kernel from explicit config, runs completions, structured output and an
agent, and never touches internals.

**Why this priority**: it validates that the surface stands alone, not only for awork's shape.

**Independent Test**: a fresh virtualenv install, a script using only `moeka` imports and the fake provider;
import-boundary test shows no gateway, channel or CLI import.

**Acceptance Scenarios**:

1. **Given** a clean environment with no HOME config, **When** the script runs, **Then** it succeeds and
   creates nothing under HOME.
2. **Given** a typo in a credential ref, **When** the first call runs, **Then** a typed `AuthError` names the
   ref.

---

### Consumer Story 3 - the live gateway runs on the same kernel (Priority: P3)

The gateway reaches the agent loop, providers and usage through the kernel rather than a parallel legacy path
(sequenced by `003-main-consolidation`).

**Why this priority**: it depends on consolidation and is not needed for awork.

**Independent Test**: the gateway's end-to-end test in a worktree passes with the kernel path.

**Acceptance Scenarios**:

1. **Given** a chat turn, **When** processed, **Then** its usage appears in the usage surface of `001`.

---

### Consumer Story 4 - the RSI harness keeps a stable driving API (Priority: P2)

The harness builds one kernel per rollout with a `Variant`, budget and registries and is unaffected by legacy
removal.

**Why this priority**: the harness pins the same commit as awork.

**Independent Test**: the harness's rollout runner contract test against the kernel API.

**Acceptance Scenarios**:

1. **Given** two kernels in one process with different variants, **When** both run, **Then** nothing leaks
   between them.

### Edge Cases

- A legacy call after removal: fails at call time with an import error naming the replacement.
- Mixed async and sync use inside a running event loop: the `*_sync` twins work there.
- A `memory=True` agent built from a profile that did not mean to enable memory.
- A corpus file that cannot be opened: raises on first use, not empty results.
- Sharing one kernel across threads.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: the `moeka` package MUST provide a replacement for every legacy completion entry point
  (`acomplete`, `complete`, `acomplete_json`, `complete_json`, `acomplete_stream`, `complete_stream`) with the
  same capabilities (system prompt, images, model or preset, sampling, response format, JSON schema or model
  class, retries, task type and payload, streaming).
- **FR-002**: provider and parse failures MUST be typed exceptions, never error text returned as content.
  Parse failure and truncation MUST be distinguishable.
- **FR-003**: an agent run MUST return a result with content, stop reason, tools used, usage and cost for the
  whole run; a provider failure MUST be a typed error on the result.
- **FR-004**: a host MUST build the environment from explicit values only (state and work dirs, credentials,
  providers, models, tools, child-process environment); it MUST read no environment variable or home file. A
  missing credential MUST raise `AuthError` naming the ref on first use.
- **FR-005**: scoped agents MUST be creatable from a profile and a name. A profile that sets a setting with no
  spec equivalent MUST raise a documented error. Memory MUST be off unless the caller enables it, or the
  profile mapping MUST be documented where it differs. Memory MUST be per agent and never shared by default.
- **FR-006**: document memory MUST be openable at a host-chosen path, and a consumer MUST be able to move a
  collection between stores without reading the store's tables directly. (Derived from the migration page's
  note that `DocStore` cannot list every chunk; the owner may drop it.)
- **FR-007**: a consumer MUST be able to supply a response cache and a budget by protocol, with request keys
  derived by the kernel; a cache hit MUST skip the budget and cost nothing.
- **FR-008**: there MUST be one documented stop contract for budget exhaustion across surfaces: an LLM call, a
  batch, and an agent run each report it in a stated, typed way (exception, systemic error, stop reason), and
  the page MUST say how a consumer whose own stop signal must bypass best-effort handlers (awork's
  `BudgetHalt`, a `BaseException`) maps onto it. [NEEDS CLARIFICATION: whether the kernel only documents the
  mapping and leaves `BudgetHalt` in the consumer, or offers a kernel-provided stop type or hook; recommended:
  document only (CLARIFY-LOG Q4)]
- **FR-009**: a batch MUST return outcomes in input order with per-item typed errors, keep completed items on a
  systemic stop, pause on rate limits and treat a long `retry_after` as systemic.
- **FR-010**: sync twins MUST be safe from any thread and inside a running event loop; one deadline MUST cover
  a whole logical call including retries.
- **FR-011**: observation MUST be through trace subscriptions and stream events; the legacy hook class MUST NOT
  be part of the public API.
- **FR-012**: every deprecated entry point MUST have a documented replacement and a `DeprecationWarning` naming
  it until removal; the migration page MUST list every deprecated entry point (drift test).
- **FR-013**: legacy entry points MUST be removed only after every named consumer pins a migrated commit; after
  removal, importing them MUST fail with a message naming the replacement.
- **FR-014**: moeka's declared dependencies MUST cover everything the public API imports, so a consumer
  installing from its own lock gets a working kernel.
- **FR-015**: consumer-visible usage and spend MUST come from the surface of `001-consumer-usage-surface`; the
  legacy per-turn usage callback is removed with the other legacy entry points and its agreement with the new
  surface is tested until then.

### Key Entities

- **Kernel**: one per process, tenant or rollout; owns the loop thread, budget, cache, registries.
- **Environment**: host-built paths, credentials, providers, models, trace sink, tools, child env.
- **Agent spec / agent**: declarative agent on a kernel; run, stream, add action.
- **Completion / RunResult / BatchResult**: results with usage, cost, stop reason, typed errors.
- **Document store**: scoped or path-opened memory.
- **Legacy entry point**: a deprecated function or class with a named replacement.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: awork's backend suite passes on the migrated commit with `nanobot.api` and `nanobot.core`
  legacy names unimportable.
- **SC-002**: 100% of the deprecated entry points have a row in the migration page and a replacement test.
- **SC-003**: a fresh-install embedder script runs and leaves zero files under HOME.
- **SC-004**: a seeded spend cap stops awork's pipeline with no retry of work after the refusal (0 calls after
  the stop).
- **SC-005**: removing the legacy modules changes no passing test outside the legacy tests themselves.
- **SC-006**: a doc-versus-code test finds 0 names in the migration page that do not exist in the API.

## Assumptions

- awork stays the strict variant and the first migrating consumer; awork-resume is out of scope.
- Both consumers pin one `core-slim` commit until consolidation (owner, 2026-09-30); after it the pin moves to
  `main`.
- Deprecation shims are not removed before awork's migrated commit exists.
