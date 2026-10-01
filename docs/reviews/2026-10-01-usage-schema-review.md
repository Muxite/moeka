# Usage surface and shared schemas: adversarial review

**Date**: 2026-10-01. **Subject**: `feat/usage-surface` (moeka, on `core-slim`) and `feat/usage-record-adapter`
(awork-resume), with the vendored schema copy. **Branches**: `review/usage` (moeka),
`review/usage-adapter` (awork-resume). **Method**: write a test that states a property a consumer relies
on, run it against the subject branch, record the failure, fix, rerun. The tests are
`tests/kernel/test_usage_review.py`, `tests/schemas/test_schemas.py` (moeka) and
`tests/llm/test_usage_review.py` (awork-resume). On the unmodified subject branch 26 of the 27 first-round moeka tests failed. Three of those were
errors in my tests (a cap too large to bind, a timing race before the call reached the provider, an off-by-one
in the retention count), corrected before the fixes; the rest failed for the reason stated below. 9 of 11
awork-resume tests failed against the original adapter (some only because an API they exercise, such as
`dedupe`, did not exist); the two that passed are controls (see "What held").

Nothing was pushed or merged, the live checkout and its service were not touched, no paid model was called.

## Verdict

The surface is sound in shape and the happy path reconciles: stream totals equal stored totals, the
budget never overshoots under concurrency, attribution survives the agent loop. It was **not** trustworthy
for the two things the owner wants it for. The waste metric pointed at the wrong call, and "every token is
attributable" was untrue for three billed or computed paths. Several store behaviours (duplicate
delivery, locked writes, retention) could make a total silently wrong. All but the items under
"Recommended, not done" are fixed on the review branches.

## Findings, ranked

Each: what breaks, evidence, fix.

### 1. The `retry` waste label was on the wrong attempt (high; fixed, usage-record 1.1)

`waste_label = "retry"` was set on attempt n > 1, the attempt that **produced the answer**; the failed
attempt before it stayed "useful". `wasted_tokens` therefore summed the good call and missed the bad one.
The spec test `test_retry_attempt_is_labelled_and_summed` asserted exactly this, so the suite agreed with the
bug. awork-resume's adapter had the same bug (its test asserted `"retry"` on round 2).
Evidence: `test_r1_retry_waste_is_the_superseded_attempt` (`assert 'retry' is None`); awork
`test_waste_is_the_superseded_attempt_in_every_retry_path`.
Fix: the label belongs to the superseded attempt. moeka emits a `waste-label` (`call.waste`) event for
attempt n-1 when attempt n appears (the earlier record is already written, records are append-only);
awork-resume knows before it writes round 1 that round 2 follows, so it labels the record directly.
The v3 to v4 store migration moves existing labels. Migration notes: `schemas/ATTRIBUTION.md`.

### 2. A late waste label relabelled every attempt of the call (high; fixed)

`label_waste(call_id, label, attempt=3)` ignored `attempt`: the totals view took the newest `call.waste`
row for the `call_id` and applied it to all attempts. Evidence: `test_r1_late_label_targets_its_own_attempt`
(attempts 1 and 2 flipped from `retry` to `discarded_draft`). Fix: labels match `(call_id, attempt)`, an
attempt's own label beats a call-wide (`attempt` null) one. `call.waste` had no schema at all; it now has
`waste-label.v1`.

### 3. "Every token attributable" (U9) is false for three paths (high; documented and pinned, not fixed)

Everything through an `LLMProvider` is metered because the observer lives in the provider base class.
`providers/image_generation.py` (billed), `providers/transcription.py` (billed) and `core/vec_store.py`
(local embedder) call models with no ledger, no attribution and no budget gate. Evidence:
`test_r10_unmetered_model_paths_are_inventoried` pins the three files, so a fourth fails the build.
Not fixed because metering them is a design call (below). Documented under "Not covered" in
`ATTRIBUTION.md`, design 3b and the SDK doc. K11 ("good enough to justify paid models") stays UNPROVEN while
paid image generation and transcription are invisible.

### 4. Calls outside the LLM layer fell to `unattributed` (high; fixed)

Dream, memory and router calls reach the provider with no bound attribution, so they were recorded as
`unattributed` even though the kernel has a `consumer`. Evidence:
`test_r10_provider_call_outside_the_llm_layer_gets_the_kernel_consumer`
(`'unattributed' == 'test-app'`) and the plain-thread variant. Fix: `CoreEnvironment.consumer`
(additive, defaults None) reaches `LedgerObserver(default_consumer=...)`. `kernel.usage.loss()` now reports
`unattributed_requests`. Still true and documented: a plain thread loses the ambient `agent`/`session`
(context variables); function tools already copy context.

### 5. The store was not idempotent (high; fixed)

Evidence: `test_r2_duplicate_delivery_is_idempotent` (`2 == 1`), same for cache hits.
A replayed trace or a write retried after an unacknowledged commit double counted. Fix: unique
`(call_id, attempt)` on `llm_calls`, unique `dedupe_key` on events, `INSERT OR IGNORE`, duplicates counted.
Merge key across producers is `(producer.name, record_id)` (two producers can both write `c-1:1`).
awork-resume had a second variant: its `calls` ignored the producer, so a moeka `c-1` and an awr `c-1`
merged (found by the mixed-producer fixture); fixed.
Also: the direct-call id was 48 random bits (`direct-<12 hex>`), a collision at roughly 10^7 calls became
a silently dropped record once dedupe existed; now a full UUID. awork's call id is 96 bits (was 48).

### 6. Locked-database writes were lost silently (high; fixed)

SQLite `busy_timeout` is 250 ms. A writer in another process holding the lock made the ledger write fail;
the ledger logged and swallowed it; `loss()` reported 0. Evidence: `test_r5_locked_store_is_counted_and_recovered`
(`0 >= 1`). Fix: failed writes are counted, kept in a bounded pending buffer (1000) and retried with the next
write; overflow is counted (`write_dropped`). A crash mid-write is atomic (SQLite); with `synchronous=NORMAL`
a power loss can drop the last commits, which is the existing trade.
Side finding (not fixed): the write runs synchronously on the event loop inside the provider-call path, so a
contended database stalls model calls up to 250 ms each.

### 7. Retention was invisible and unbounded where it mattered (medium; fixed)

`llm_calls` was pruned to 100k rows without telling anyone (totals over old windows silently shrank), while
`llm_usage_events` was never pruned (unbounded). Evidence: `test_r6_pruning_is_reported_and_events_are_pruned_too`.
Fix: events are pruned with the same bounds; pruned counts persist in a meta table and show in
`loss()["pruned_rows"]` and `store.stats()["oldest_started_at_ms"]`.

### 8. Null versus zero: the stream said 0, the store said null (medium; fixed)

`tokens_cache_read` was `or 0` on the event but raw in the store (U8 violation), and "unknown" shipped as
"none". Evidence: `test_r3_*` (`assert (0 is None)`). Fix: null on the event when the provider did not report.

### 9. Totals did not reconcile with provider-reported usage when reasoning is billed outside output (medium; fixed)

`LLMUsage.total_tokens` can exceed input plus output (hidden reasoning; the base class says so). The
ledger priced and recorded only input and output, so tokens and cost under-counted. Evidence:
`test_r4_hidden_output_tokens_are_billed_and_visible` (`40 == 90`). Fix: the excess is normalised into
`tokens_out` and exposed as `tokens_reasoning` (a subset of `tokens_out`); cost uses it. Cache tokens:
Anthropic reports `input_tokens` excluding cache, but moeka's provider already adds them in; the schema now
states the normalisation rule for every producer. Unproven: any provider other than the fakes and
the `LLMUsage` rule; no live API was called.

### 10. A timed-out or cancelled call refunded its whole reservation (medium; fixed, owner decision)

Evidence: `test_r9_timed_out_call_still_counts_against_the_cap`, `test_r9_cancelled_call_still_counts_against_the_cap`
(`spent_usd == 0.0`). The provider may have billed it and reported nothing, so a loop of cancelled calls
could exceed a cap. awork-resume already charges the worst case on a timeout (M7). Fix: the reservation stays
as spend for outcomes `timeout` and `cancelled`. Consequence: `budget().spent_usd` can exceed
`sum(cost_usd)`; the gap is `unpriced_requests`. Owner may prefer a smaller charge (input estimate only).

### 11. Estimated and billed cost were summed together (medium; fixed)

`cost_usd` summed an estimate (awork's timeout worst case, `tokens_out = max_tokens`) with real bills.
Fix: totals expose `billed_cost_usd`, `estimated_cost_usd` and `estimated_tokens`; both producers' totals
agree on one definition set (`ATTRIBUTION.md` "Totals"). The estimate is still in `cost_usd` (compatible).

### 12. Schema evolution was announced, not enforced (medium; fixed)

The snapshot test caught a removed field name only. A new required field, a narrowed type or enum, a
tightened bound, a closed `additionalProperties` or a changed conditional all passed. Evidence:
`test_breaking_change_detector_detects` mutates the schema seven ways. Fix: frozen copies of the released
1.0 schemas in `tests/schemas/frozen/` and a checker that fails on any of those. Additive growth passes.
Other gaps closed: each document versions on its own (`kernel.usage.documents()`; the kernel used one
constant for usage records and budget events); an old reader meeting a newer minor with a new enum value
failed strict validation (the doc promised readers may ignore it), so there is a reader profile
(`validate.problems(doc, name, strict=False)`) and a stated rule; `key_scheme`, previously stuffed in
awork's `tags`, is a field, and the `complete-json-call` `key_scheme` enum became a pattern so a third
producer need not edit the schema.

### 13. Forgeability (medium; partly fixed)

An exec-capable agent could rewrite `llm_usage.sqlite3`: the file was not in the file floor (only
`auth/`, `plugin-data/`, `sessions/`, plus facts and artifacts in `state_dir`) and no shell pattern named it.
Fixes: database and sidecars in `ProtectedFloor`; the exec floor refuses command lines naming the file (a
hint, trivially bypassed, and it also blocks `sqlite3` reads of it); `kernel.usage.checkpoint()` /
`verify()` give tamper evidence: a digest the harness keeps outside the agent's reach proves rows were not
edited, deleted or reordered (honest appends pass). Evidence: `test_r11_*`. **Not fixed**: prevention. An
agent that can exec can still edit the file; only a writer in another process or uid prevents it, and SC-007
stays unproven.

### 14. Tags: unbounded, leaky, and not in the store (medium; fixed)

`tags` took any object of any size on the stream (5 698 serialised bytes in the test, nested values and a
5 000-character "prompt"), and the store had no tags column, so stream and store differed. Fix: bounds
(16 keys, 64-character keys, scalar values, strings up to 120 characters, 2 KiB), over-limit values are
dropped not clipped (a clipped prompt is still a leaked prompt), the marker `moeka.tags_truncated` says so,
attribution strings clip at 200, and tags round-trip through the store. The bounds are a producer rule, not
a schema constraint, so a reader never rejects a record for them (a tightening would be a breaking change).
A 8 000-character event payload used to be cut mid-JSON, corrupting `request_key` on read; now slimmed.
PII: content-free is enforced only by construction; nothing can detect a prompt pasted into `purpose`.

### 15. awork-resume: a torn JSONL line failed `awr usage` (low; fixed)

A crash mid-append left an unparseable last line and `load_jsonl` raised. Now skipped. Multi-process appends
were tested and do not interleave (a control that held).

### 16. Late and out-of-order data (low; documented)

`started_at_ms` is the producer's clock. Hosts skew, deliveries arrive late, and a window can still grow
after it closes. Aggregate by window; tail by ingestion order. There is no ingestion sequence on the
documents (recommended, below).

## What held (controls)

- 40 concurrent calls against a small cap: spend never exceeded the cap, reservations drained to zero, and
  stored cost equalled budget spend (`test_concurrent_calls_never_overshoot_the_cap`).
- awork-resume: 40 threads sharing one `Ledger` produce unique, consistent records; 4 processes appending
  to one log never interleave lines; record costs equal `Ledger.spent` across settled, charged and
  billed-malformed attempts.
- A 30-line third-party emitter written from the docs alone validated first time
  (`test_a_third_party_emitter_written_from_the_docs_validates`). It would not have caught the cross-field
  rules, which is why `validate.py` exists now (record_id shape, cache subsets, reasoning subset, "estimated
  is not billed").
- `reduce_records` (the reference reducer over documents) agrees with the SQL totals on a mixed workload with
  retries, hits, estimates and filters, so the two implementations of the definitions cannot drift unseen.
- Migration is additive and rolls back: a v3 database opens, old rows read with `unattributed` and empty
  tags, and the previous writer's INSERT still works on the migrated file.

## Interop: can awork-resume's and moeka's records be merged in one analysis?

Yes, now, with one caveat. Before: `retry` meant different things in effect, `calls` meant settled calls in
one and distinct `call_id` in the other, `call_id` collided across producers, `key_scheme` lived in a tag and
`request_key` could not be compared. `schemas/examples/mixed-producers.jsonl` (both producers, a duplicate
delivery, a `record_id` collision) now reduces to identical numbers in both repos' reducers
(`test_mixed_producer_fixture_reduces_to_moeka_numbers`).

`key_scheme` alone was **not** enough: it said how a key was derived but the record did not carry it, and the
schema listed two values. Two records join as "the same request" only under the same scheme; the field is now
on the record and open-ended. Remaining semantic gaps (documented, not schema-fixable): awork's latency is
per attempt, awork reserves per round while moeka admits per logical call (`attempt` on the budget event
says which), awork cannot report cache-read/write tokens (null, summed as 0).

## Over-engineering check

Cost more than they return; recommended cuts or deferrals:

- **`complete-json-call.v1`**: nobody emits it, it is the one document allowed to carry content, and it
  contradicts the content-free principle. Freeze it as informative or delete it; do not version it further.
- **Four of five waste categories.** Only `retry` and `discarded_draft` are real. `refused_after_admission`
  is zero tokens by construction (a refused call sent nothing); `failover` and `avoidable_cache_miss` are not
  labelled by anything and the second is gameable (it needs a "prior identical call" oracle). Cut the enum to
  what is implemented, or implement `failover` (cheap: the fallback wrapper knows) and cut the other two.
- **T019 cross-process stream** (a transport): the SQLite store is already readable from another process
  under WAL. A reader polling `records(since=...)` or tailing the JSONL gives U6's cross-process half at a
  fraction of the cost. Cut.
- **OpenTelemetry export (T025)**: no consumer; the field names already map. Defer until a viewer needs it.
- **T023 replay recorder**: awork-resume replays through its own cache and the kernel through
  `request_key`; a third recorder adds a format to maintain. Defer until the RSI harness asks.
- **A per-tag budget scope in `budget-event`**: built, but no consumer uses it and awork has none. Keep the
  code, stop promising it in the schema until used.
- **Hash-chain or signed rows**: not built; `checkpoint`/`verify` (about 40 lines) gives most of the value.

Worth their cost: typed attribution, the cache-hit-as-record rule, `cost_billed`/`usage_source`, the
`(producer, record_id)` key, the compat checker and `validate.py`.

## Fixes made (commits on `review/usage`, `review/usage-adapter`)

moeka: waste on the superseded attempt and late labels per attempt; `waste-label.v1`; usage-record and
budget-event 1.1 (per-document versions, `key_scheme`, `attempt` on budget events, documented limits and
semantics); store idempotency, pending buffer, loss and retention counters, durable pruned count; v3 to v4
migration; null cache tokens; hidden-output normalisation; tag bounds and storage; slim event payloads;
billed/estimated/`calls`/`retries`/`latency_ms`/`estimated_tokens` totals; `reduce_records`;
timeout/cancel keep their reservation; default consumer for out-of-layer calls; `unattributed` and
unmetered-path surfacing; file floor, exec-floor pattern, `checkpoint`/`verify`; the schema compat checker,
frozen 1.0 copies and `schemas/validate.py` (strict and reader profiles); docs (`ATTRIBUTION.md`,
`CONFORMANCE.md`, `docs/python-sdk.md`, design 3b, spec 001 task status).
awork-resume: waste label, `key_scheme` field, per-round budget events, billed/estimated totals, per-producer
`calls`, dedupe, torn-line tolerance, 96-bit ids, vendored copy refreshed with `validate.py`, the
`waste-label` schema and the mixed fixture, conformance helper now runs the cross-field rules.

## Recommended, not done

1. Meter or exclude image generation and transcription (U9). Smallest honest step: an `unmetered.call` usage
   record (tokens null, cost null, attribution set) from those two modules, so counts and attribution exist
   even without prices.
2. Move the store write off the event loop (single writer thread with the pending buffer behind it).
3. A writer the agent cannot reach (separate uid or process owning the file) if SC-007 matters.
4. `price_ref` on records (which price table priced it) so price drift is visible; today a repricing is
   possible from tokens and model but not detectable.
5. An ingestion sequence number on stored records and a `records(after_seq=...)` cursor, for tailing.
6. Label `failover` (the fallback wrapper knows) and surface a final failed round as "unproductive" tokens.
7. Propagate ambient attribution into plain threads, or fail loudly in `strict` mode.
8. Real provider checks of the token rules (cache, reasoning) against live responses; only the `LLMUsage`
   normalisation and fakes were exercised.
9. awork-resume: emit `waste-label.v1` for rejected drafts in the claim loop (task T012); pass `role=` at call
   sites (T010).

## Owner decisions

1. **1.1 or 2.0 for the `retry` re-meaning.** Shipped as 1.1 because 1.0 never left the review branches. If
   any consumer already stored 1.0 records, make it `2.0` and a new `$id`; the migration code stays.
2. **What a timeout or cancel costs the budget**: the whole reservation (shipped, matches awork's M7), or an
   input-token estimate only.
3. **Exec floor blocks any command naming the usage database**, including read-only `sqlite3`. Keep, or narrow
   to mutating verbs.
4. **`CoreEnvironment.consumer`**: a new defaulted field on a frozen public dataclass. Accept, or pass the
   consumer another way.
5. **The cut list above**, especially `complete-json-call.v1` and three waste labels.
6. **Meter the unmetered paths, or declare U9 to cover LLM calls only.**
7. **Whether tamper evidence (not prevention) is enough** for the RSI harness trust model.

## Verification

moeka (`review/usage`): see the run log in the final report for the exact counts; relevant suites run
locally with `uv run pytest` and the full suite through `scripts/test-docker.sh`. awork-resume: `uv run pytest -q`
(two e2e files need `pdflatex`; they are skipped where it is absent, so the build-level conformance test was
not exercised here).
