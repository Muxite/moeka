# Usage schemas and attribution

Canonical, versioned JSON Schemas (draft 2020-12) for what a model call costs and who caused it.
Any program may emit them; moeka emits them natively and exposes them through `kernel.usage`.

| Schema | One document is | Content |
|---|---|---|
| `usage-record.v1` | one physical call attempt, or one response-cache hit | content-free |
| `budget-event.v1` | one admit or refuse decision with the budget in force | content-free |
| `complete-json-call.v1` | one structured completion: request contract and result | may carry content |

Examples: `examples/`. Test: `tests/schemas/test_schemas.py`. Other consumers: `CONFORMANCE.md`.

## Versioning

- `schema_version` is `"<major>.<minor>"` on every document. The major is fixed by the schema file
  (`*.v1.*` accepts only `1.x`); the `$id` carries it.
- Additive change (a new optional field, a new enum value readers may ignore): minor bump, same file name.
  Readers MUST ignore unknown fields (`additionalProperties` is open).
- Removing a field, re-meaning one, or making an optional field required: new major, new file (`v2`), and
  producers state the versions they speak (`kernel.usage.schema_versions()`).
- The test keeps a snapshot of declared field names; removing one fails the build.

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

## Identity and linkage

- `call_id`: one logical call; all attempts of it (retries, re-prompt rounds, failover) share it.
- `attempt`: 1-based; `0` for a cache hit. `record_id = "<call_id>:<attempt>"`.
- `parent_call_id`: the logical call that caused this one (a verifier's call points at the producer's).
- `trace_id`: the trace the call ran under. `request_key` + `prompt_version`: the replay/cache identity.

## Tokens and cost

- `tokens_in` INCLUDES cache reads and cache writes (provider convention in both moeka and OpenRouter);
  `tokens_cache_read` and `tokens_cache_write` are subsets. `null` = the provider did not report it, `0` =
  explicitly none.
- `tokens_reasoning`: hidden reasoning tokens when reported separately. moeka's providers do not report it
  yet, so it is `null`.
- `usage_source`: `reported` (provider billed these counts), `estimated` (producer counted locally),
  `mixed`, `none`. An estimated count is never presented as billed.
- `cost_usd`: `null` = unknown, never free. `cost_billed` is true only for provider-reported usage at a known
  price, or the local-zero convention. `price_source`: `provider_reported` (the provider returned the cost,
  e.g. OpenRouter), `price_table` (tokens times the producer's table), `local_zero` (local tier), `cache` (a cache hit, zero by construction), `none`.

## Cache hits

A hit is its own record (`kind: cache_hit`, `attempt: 0`): billed tokens and cost are zero,
`saved_tokens_in/out` and `saved_cost_usd` say what it avoided. Totals across a window therefore split
into billed (model_call records) and saved (cache_hit records) and never double count.

## Outcome and waste

- `outcome`: `ok`, `truncated`, `parse_failure` (replied, invalid for the schema; still billed),
  `error`, `timeout`, `cancelled`.
- `waste_label` is a label set when the fact is known, never a later estimate:
  `retry` (a retried or re-prompted attempt, set by the producer on attempt > 1), `failover` (an attempt on a
  fallback model after another failed), `avoidable_cache_miss`, `discarded_draft` (known only after a
  verifier rejects it: a linked `call.waste` event keyed by `call_id`, applied by readers), and
  `refused_after_admission`. `waste_set_by` says who: `kernel`, `caller`, `consumer`.
  Records are append-only: a late label is a separate `call.waste` event `{call_id, attempt?, label}`.

## Budget events

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

## OpenTelemetry mapping (when exporting)

`tokens_in` -> `gen_ai.usage.input_tokens`, `tokens_out` -> `gen_ai.usage.output_tokens`,
`model` -> `gen_ai.request.model`, `provider` -> `gen_ai.provider.name`,
`finish_reason` -> `gen_ai.response.finish_reasons`; other fields as `moeka.*` attributes.
Not implemented in code yet (spec 001 T025).
