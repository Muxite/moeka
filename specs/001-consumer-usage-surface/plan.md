# Implementation Plan: Consumer Usage Surface

**Branch**: `001-consumer-usage-surface` | **Date**: 2026-09-30 | **Spec**: [spec.md](spec.md)

## Summary

Close gaps G1-G12 so a consumer can show usage and spend from `moeka` alone. Approach: extend the existing
ledger event (`nanobot/kernel/ledger.py`) rather than add a second pipeline; make the trace stream the source
of truth; add typed attribution, a budget snapshot, waste labels and a schema version; expose a public
`moeka` usage module; add a protected durable sink, a cross-process stream, replay recording and the tests of
U12.

## Technical Context (as of 2026-09-30; verified against `core-slim` at `2438fffe`, tests read not re-run)

**Language/Version**: Python 3.11+, asyncio.
**Primary Dependencies**: pydantic, loguru, stdlib sqlite3.
**Storage**: `<data_dir>/llm_usage.sqlite3` (`nanobot/llm_usage/store.py`, table `llm_calls`).
**Testing**: pytest (`asyncio_mode=auto`); kernel tests under `tests/kernel/`; Docker runner `scripts/test-docker.sh`.
**Project Type**: library (`moeka` re-exports `nanobot/kernel/`).
**Constraints**: fail-open observation; content-free events; no ambient reads (I1 AST guard).

Verified current state per gap (closes in the tasks):

| Gap | Fact as of 2026-09-30 |
|---|---|
| G1 | `moeka/` has no usage module; its submodules are `agents, budget, epistemics, errors, llm, memory, sessions, testing, tools, trace, variants`. |
| G2 | `LedgerEvent` fields: `trace_id, slot, tier, model, provider, tokens_in, tokens_out, tokens_cache_read, latency_ms, cost_usd, source, usage_source, finish_reason, call_id, alias, attempt, cached, tags`. No consumer, agent or session field. `source` is the `LLMUsageSource` enum. |
| G2 | `llm_calls` columns: `started_at_ms, duration_ms, provider, model, source, stream, finish_reason, input/output/total/cache_read/cache_write tokens, reported/estimated tokens, generation_ms, measured_output_tokens, ttft_ms, timed_requests, error_status_code, error_kind, tier, cost_usd`. No tags, call id, attempt, cached or slot column. |
| G3 | The event has `tokens_cache_read` only; the table has `cache_write_tokens`. A cache hit emits `cache.hit` (`call_id, key, alias, model, tags`), not `model.call`; it carries no saved-token figure. `CallAttribution.cached` exists but a hit never reaches the observer. |
| G4 | `CapBudget` emits `budget.admit`/`budget.refuse`; remaining is on no event. |
| G5 | `Tracer.subscribe` is in-process and synchronous; coroutine subscribers are rejected. |
| G6 | No `schema_version` on events; `schema_version()` exists only on the fact and artifact stores. `EVENTS` in `trace.py` documents names and keys. |
| G7 | Trace sink and `LLMUsageStore` are separate writers; no totals-agreement test. |
| G8 | Retries visible as `attempt > 1`; no waste label. |
| G9 | `ResponseCache` answers repeats; nothing records responses for replay. |
| G10 | The ledger SQLite is writable by an exec-capable agent; in the flat legacy layout the file floor does not name `llm_usage.sqlite3`. |
| G11 | The turn loop, sub-agents and memory call providers directly; calls outside the LLM layer have no `call_id` or `alias`. |
| G12 | `BudgetExceeded` is in `moeka.errors`; `BudgetHalt` exists only in awork (`awork/llm.py`). |

## Constitution Check

- VII Everything Is Data: this feature implements it. PASS.
- IX Consumers Derive Usage From moeka: implements it. PASS.
- I Zero Ambient Reads: any new sink or stream location MUST be passed by the host; no env reads. GATE.
- II Path separation: the protected usage source MUST live under `state_dir` or a host-chosen location the
  file floor protects in both layouts (closes the flat-layout hole). GATE.
- Content-free events versus replay recordings (FR-003 vs FR-011): recordings are a separate opt-in host-owned
  artifact. PASS with note (see Inconsistencies in `CLARIFY-LOG.md`).

## Design Decisions (pending owner answers)

- Attribution (FR-002): add typed fields to `CallAttribution` and `LedgerEvent`; bind them from the agent and
  sub-agent paths through the same context var (`call_attribution`). Pending CLARIFY-LOG Q2.
- Waste (FR-010): kernel-known waste (retry, failover, post-admission refusal) set at emission; caller-known
  waste (discarded draft) through a linked follow-up event keyed by `call_id`. Pending CLARIFY-LOG Q3.
- Source of truth (FR-008): events are authoritative; the SQLite store becomes a projection written from the
  same event, never a second independent writer.
- Protected source (FR-005): host-owned append-only sink the agent cannot reach, with the SQLite store read
  only as a convenience.
- Stream (FR-006): host-chosen transport behind a small interface; in-process subscribers stay.
- Schema (FR-007): constant `SCHEMA_VERSION` stamped in `to_trace`; snapshot test; an OpenTelemetry GenAI
  mapping function.

## Project Structure

```text
specs/001-consumer-usage-surface/
├── spec.md
├── plan.md
└── tasks.md
nanobot/kernel/ledger.py        # event, attribution, waste, schema version
nanobot/kernel/budget.py        # budget snapshot on events and query
nanobot/kernel/llm.py           # cache.hit -> usage event with saved tokens
nanobot/llm_usage/              # projection store, query helpers
moeka/usage.py                  # new public module (re-exports only)
tests/kernel/                   # attribution, totals agreement, schema snapshot, waste, replay, fail-open
```

**Structure Decision**: single library; one new re-export module in `moeka/`, logic stays in `nanobot/kernel/`
per the existing "moeka only re-exports" rule.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Cross-process stream | gateway and dashboards are separate processes | in-process only cannot serve the gateway (G5) |
| Replay recording store | U11 offline replay | `ResponseCache` holds only cacheable successes, not every response |
