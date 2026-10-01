# Usage schemas and attribution

Canonical, versioned JSON Schemas (draft 2020-12) for what a model call costs and who caused it.
Any program may emit them; moeka emits them natively and exposes them through `kernel.usage`.

| Schema | One document is | Content |
|---|---|---|
| `usage-record.v1` | one physical call attempt, or one response-cache hit | content-free |
| `budget-event.v1` | one admit or refuse decision with the budget in force | content-free |
| `waste-label.v1` | a late "these tokens were wasted" label for one earlier attempt | content-free |
| `complete-json-call.v1` | one structured completion: request contract and result | may carry content |

Examples: `examples/`. Tests: `tests/schemas/test_schemas.py`. Validator (schema plus cross-field
rules, producer and reader profiles): `validate.py`. Other consumers: `CONFORMANCE.md`.

## Versioning

- Each document versions on its own: `schema_version` is `"<major>.<minor>"`, the major is fixed by the
  schema file (`*.v1.*` accepts only `1.x`), and the `$id` carries it. Current: `usage-record` 1.1,
  `budget-event` 1.1, `waste-label` 1.0, `complete-json-call` 1.0 (`kernel.usage.documents()`;
  `kernel.usage.schema_versions()` lists the usage-record versions the kernel emits). The kernel emits
  only its newest version and does not translate down.
- Additive change (a new optional field, a new enum value): minor bump, same file name. Removing a
  field, re-meaning one, tightening a bound, making an optional field required, or closing
  `additionalProperties`: new major, new file (`v2`).
- Enforced, not promised: `tests/schemas/frozen/` holds the first released copy of each schema and
  `test_current_schema_is_a_compatible_evolution_of_1_0` fails on any of the breaking changes above
  (a removed property, a grown `required`, a narrowed type or enum, a tightened bound, a changed
  conditional rule). Add a frozen copy when a minor ships; never edit one.
- Reader rule (a consumer that lags a version): readers MUST ignore unknown fields and MUST treat an
  unknown value of an enum-valued field (`outcome`, `waste_label`, `price_source`, ...) as opaque, not
  as an error. A reader validates with the reader profile (`validate.problems(doc, name, strict=False)`:
  enums relaxed, major still enforced); a producer validates strict, which catches typos. A document
  whose major is higher than the reader's is refused, never half-read.
- Producer limits that a reader never enforces (a record is not rejected for them): see `tags`.

### Migration notes

- usage-record 1.0 to 1.1 (1.0 existed on review branches only): the meaning of `waste_label =
  "retry"` was corrected. 1.0 put it on the attempt that was a retry (attempt 2 of a call), which
  counted the attempt that produced the answer as waste and the one that failed as useful. 1.1 puts it
  on the attempt that was superseded. A consumer that read 1.0 must not carry the old meaning over: a
  record stamped `1.0` with `waste_label = "retry"` is a retry attempt, not wasted tokens. moeka's
  usage database moves the old labels itself on first open (store version 3 to 4: the label is removed
  from attempt n and added as a `waste-label` event on attempt n-1). A strict reading would make this
  a v2; it ships as 1.1 because 1.0 never left the review branches, and that call is the owner's
  (docs/reviews/2026-10-01-usage-schema-review.md).
- Rolling back to a 1.0 build: the database additions are nullable columns and indexes; the old
  writer keeps working and its rows read back (with `tags` empty).

## Attribution fields (who and why)

Set by the caller through typed fields, never parsed out of `tags`.

| Field | Meaning | moeka source | Example |
|---|---|---|---|
| `consumer` | the product on whose behalf the call ran; required | `Kernel(consumer=...)`, overridable per call | `awork`, `awork-resume`, `gateway` |
| `agent` | agent or pipeline actor | agent spec name (bound for the whole run) | `research` |
| `session` | run, build or chat id | session key (agents), `GenerateOptions.attribution` | `build-2026-10-01-a` |
| `role` | functional role of the call | `GenerateOptions.attribution` | `extractor`, `checker` |
| `purpose` | task type or stage | `GenerateOptions.attribution` | `extract_claims` |
| `slot` | schema slot filled (kernel router) | existing `llm_usage_slot` | `skills[0].bullets` |
| `tags` | host extras only | `GenerateOptions.tags` | `{"stage": "ingest"}` |

`consumer = "unattributed"` means nobody set it; the attribution-completeness test treats it as a defect.

## Identity, idempotency and ordering

- `call_id`: one logical call; all attempts of it (retries, re-prompt rounds, failover) share it. Use at
  least 96 random bits (moeka: a full UUID; awork-resume: 48 bits, fine per run, thin across years).
- `attempt`: 1-based; `0` for a cache hit. `record_id = "<call_id>:<attempt>"`.
- Idempotency key: `(producer.name, record_id)`. Two producers can both write `c-1:1`; one producer
  delivering the same record twice (a replayed trace, a retried write) is one record. moeka's store
  ignores the duplicate and counts it (`kernel.usage.loss()["duplicates"]`); `reduce_records` and
  awork-resume's totals do the same. A `waste-label` is idempotent on `(call_id, attempt, waste_label)`.
- `parent_call_id`: the logical call that caused this one (a verifier's call points at the producer's).
- `trace_id`: the trace the call ran under. `request_key` + `prompt_version`: the replay/cache identity.
- Time: `started_at_ms` is the producer's clock at the start of the attempt. Producers on different
  hosts disagree by seconds, a late delivery arrives after later records, and an old record can be
  written after a newer one. Aggregate by `started_at_ms` windows; to follow a stream (tail) use the
  producer's own ingestion order, never the timestamp. Totals over a window that closed recently
  can still grow.

## Tokens and cost

Normative: these hold for every record, and `validate.py` checks the ones a schema cannot.

- `tokens_in` INCLUDES cache reads and cache writes (the OpenAI convention). A provider that reports
  them separately (Anthropic: `input_tokens` excludes both) is normalised by adding them in. The
  subsets `tokens_cache_read` and `tokens_cache_write` never exceed `tokens_in` together.
  `null` = the provider did not report it, `0` = explicitly none. A sum over records treats null as 0;
  a document never does.
- `tokens_out` INCLUDES reasoning tokens, always. A provider that bills reasoning outside its output
  count (a reported total above input plus output, as some Gemini and OpenAI-compatible endpoints do)
  is normalised by adding the excess to `tokens_out`, and `tokens_reasoning` carries it.
  `tokens_reasoning` is a subset of `tokens_out`, never an addition to it; null = not reported. Cost
  uses the normalised figures, so `tokens_in + tokens_out` equals the provider's reported total.
- `usage_source`: `reported` (provider billed these counts), `estimated` (producer counted locally),
  `mixed`, `none`. An estimated count is never presented as billed: `cost_billed` is false for it.
- `cost_usd` is US dollars, per record, at the price in force when the call ran. A producer billed in
  another currency converts at emit time (the rate is not recorded). `null` = unknown, never free.
  `cost_billed` is true only for provider-reported usage at a known price, or the local-zero
  convention. `price_source`: `provider_reported` (the provider returned the cost, e.g. OpenRouter),
  `price_table` (tokens times the producer's table), `local_zero` (local tier), `cache` (a cache hit,
  zero by construction), `none`.
- Price drift: a price table that changes later does not rewrite old records, and a record does not
  say which table priced it. Totals across a price change mix both. Re-pricing from tokens is possible
  (tokens and model are on the record); a `price_ref` field to say which table is a recommended
  addition, not yet made.
- A timed-out or cancelled attempt usually has no usage (`usage_source none`, `cost_usd` null) although
  the provider may have billed it. The budget does not forgive it: moeka keeps the reservation as
  spend, so `budget().spent_usd` can exceed the sum of `cost_usd`; the gap is the unknowable calls
  (count them with `unpriced_requests`).
- Totals separate what was billed from what was guessed: `cost_usd = billed_cost_usd +
  estimated_cost_usd`; an estimate is a hint, not a bill.

## Totals (reference definitions)

One definition set for every consumer (`nanobot.llm_usage.query.reduce_records` implements it over
documents; the store's SQL agrees, tested): `requests` = physical attempts (`model_call` records);
`calls` = distinct `(producer, call_id)`; `retries` = attempts with `attempt > 1`; `latency_ms` = the
sum of attempt latencies (concurrent attempts overlap: not wall time); `cache_hits` and `saved_*` from
`cache_hit` records only, never added to `requests`; `unpriced_requests` = null cost; `wasted_*` =
records carrying a waste label. awork-resume's old `calls` (settled calls) and per-stage `seconds`
(wall time) are different quantities: use `calls`/`requests` and `latency_ms` when merging.

## Cache hits

A hit is its own record (`kind: cache_hit`, `attempt: 0`): billed tokens and cost are zero,
`saved_tokens_in/out` and `saved_cost_usd` say what it avoided. Totals across a window therefore split
into billed (model_call records) and saved (cache_hit records) and never double count.

## Outcome and waste

- `outcome`: `ok`, `truncated`, `parse_failure` (replied, invalid for the schema; still billed),
  `error`, `timeout`, `cancelled`.
- `waste_label` says why THESE tokens did not contribute to the final result: `retry` (this attempt
  was superseded by a later attempt of the same call; the attempt that produced the answer is not
  waste), `failover` (superseded because the model was swapped; not yet labelled by the kernel),
  `avoidable_cache_miss` (not yet labelled), `discarded_draft` (a verifier rejected the output),
  `refused_after_admission` (not yet labelled). `waste_set_by` says who: `kernel`, `caller`,
  `consumer`.
- Records are append-only, so a label learned after the record was written is its own document,
  `waste-label.v1` (the `call.waste` trace event), keyed by `(call_id, attempt)`; `attempt` null labels
  every attempt of the call that has none of its own, and an attempt's own label wins. Readers apply
  it. A producer that knows at emission time (awork-resume decides before it writes a round that there
  will be another) writes the label on the record.
- What a label is not: a final attempt that failed (an error with no retry left) is unlabelled even
  though its tokens produced nothing; count it through `outcome` and `failed_requests`. Labels are set
  by callers and can be wrong or withheld: a consumer that wants a number it can hold a producer to
  should compare `wasted_tokens` with the outcome-based counts, not trust the label alone.

## Budget events

A decision per logical call (moeka) or per attempt (awork-resume reserves each round: it sets `attempt`).
`budget-event.v1.kind` is `admit` or `refuse`. Both carry the same attribution as the call, the scope
(`kernel`, `consumer`, or a `tag`) and its figures: `cap_*`, `spent_*`, `reserved_*`, `remaining_*`
(`null` = uncapped). `remaining = max(0, cap - spent - reserved)`. A refusal has `refusal.code`:
`cap_usd`, `cap_tokens`, `cap_tag`, `unpriced_model`, `paid_disabled`, `policy`, `other`.

## complete-json-call

`request` mirrors awork-resume's `complete_json(ref, system, prompt, out, prompt_version)`: `ref`
(`provider`, `model`, `family`), `system`, `prompt`, `output_schema` (the `out` model's JSON Schema),
`prompt_version`, `sampling` (temperature 0 and `max_tokens` are part of the key). `request_key` is opaque;
`key_scheme` says how it was derived, so a recording replays only under the same scheme. `result` carries
`parsed`, a `raw_ref` to the raw reply (not inlined), `cache_hit`, `attempts`, and the `usage_record_ids`
of every attempt joined to `usage-record.v1.record_id`.

## Privacy and size

Records are content-free by construction (no prompt, reply or reasoning text). The free-text fields can
still leak if a caller puts content in them: `purpose`, `role`, `session`, `agent` are clipped at 200
characters by moeka; `tags` are bounded (16 keys, 64-character keys, scalar values, strings up to 120
characters, 2 KiB; an oversized value is dropped, not clipped, and `moeka.tags_truncated` is set);
`error_kind` is a class name; a budget refusal `message` is the exception text. High-cardinality tags (a
user id, a request id) are legal and make `group_by` useless; put identity in `session`. A stored event
payload is capped at 8000 characters and is slimmed, never cut mid-JSON.

## Not covered (U9 does not hold here)

Everything that reaches a model through an `LLMProvider` is metered, attributed and budget-gated (the
observer lives in the provider base class). Three paths do not, and a test pins the list so a new one
fails the build: `providers/image_generation.py` (billed image generation),
`providers/transcription.py` (billed speech to text), `core/vec_store.py` (the local embedder: tokens
computed, no money). Their spend is in no record and no budget. A provider called from a plain thread
loses the ambient `bind_attribution` (context variables do not cross threads: use
`contextvars.copy_context().run`), but keeps the kernel's `consumer`, so it is never `unattributed`;
`kernel.usage.loss()["unattributed_requests"]` counts the rest.

## Forging

The usage database is an SQLite file; an exec-capable agent can rewrite it. Defences, weakest to
strongest: the exec floor refuses command lines that name `llm_usage.sqlite3` (a hint, trivially
bypassed); the file floor (`ProtectedFloor`) covers the database and its sidecars for the file tools;
`kernel.usage.checkpoint()` returns a digest the harness keeps in its own state, and
`kernel.usage.verify(cp)` later proves the rows were not edited, removed or reordered (honest appends
are fine; retention pruning is not, so take one per run). Prevention needs a writer the agent cannot
reach (a separate process or uid owning the file): not built.

## OpenTelemetry mapping (when exporting)

`tokens_in` -> `gen_ai.usage.input_tokens`, `tokens_out` -> `gen_ai.usage.output_tokens`,
`model` -> `gen_ai.request.model`, `provider` -> `gen_ai.provider.name`,
`finish_reason` -> `gen_ai.response.finish_reasons`; other fields as `moeka.*` attributes.
Not implemented in code yet (spec 001 T025).
