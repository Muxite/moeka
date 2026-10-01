# Conformance: emitting usage-record.v1 from a non-moeka consumer

Worked for awork-resume's `awr.llm` (read at `awork-resume` commit `c1a6dce`: `ledger.py`, `cache.py`,
`complete.py`, `ref.py`, `transport.py`). awork-resume stays independent of moeka; this page says how it
can emit the same documents today, and what adopting moeka later changes. Nothing here edits awork-resume.

## What conformance means

A producer conforms when every document it emits validates against `usage-record.v1.schema.json`
(and `budget-event.v1.schema.json` for budget decisions), states `schema_version`, and honours the
semantics in `ATTRIBUTION.md` (tokens_in includes cache tokens, null means unknown, a cache hit bills zero).
Test it with `jsonschema.Draft202012Validator`; `tests/schemas/test_schemas.py` shows the pattern.

## What awr.llm records today

- `Ledger` (`ledger.py`): `reserve(ref, prompt_chars, max_tokens)` before dispatch, then one of
  `settle(ref, RawReply, reserved)`, `charge(reserved)` (read timeout, worst-case estimate) or
  `release(reserved)` (never reached the provider). It keeps running totals only: `spent`, open
  reservations, and per stage `calls, prompt_tokens, completion_tokens, cost_usd, seconds`. No per-call
  record, no ids, no attempt, no latency per call.
- `RawReply` (`transport.py`): `text, prompt_tokens, completion_tokens, cost_usd` (0.0 unless the provider,
  OpenRouter, reported a cost).
- `LLMCache` (`cache.py`): table `llm_cache(key, value)`; key = sha256 of canonical JSON
  `{provider, model, system, prompt, schema, prompt_version, max_tokens, t: 0}`. A hit returns before the
  ledger is touched, so hits are not counted anywhere and the saved usage is not stored.
- `complete_json(ref, *, system, prompt, out, prompt_version, ctx, max_tokens)`: at most 2 rounds (one
  re-prompt on a parse failure or a timeout); `ref = ModelRef(provider, model, family)`.

## Field mapping: usage-record.v1 from awr.llm

| usage-record.v1 | awr.llm source | Status |
|---|---|---|
| `schema_version` | constant `"1.0"` | adapter |
| `record_id`, `call_id`, `attempt` | `call_id = uuid4().hex` per `complete_json` call; `attempt = round_i + 1`; cache hit `attempt = 0` | gap: not recorded today; adapter generates |
| `kind` | `model_call` per round that reached `transport.chat`; `cache_hit` when `ctx.cache.get` returns a value | adapter |
| `consumer` | constant `"awork-resume"` | adapter |
| `agent` | none (no agents); the actor, e.g. `"claim-loop"`, if wanted | optional; null |
| `session` | build or run id | gap: `LLMContext` has no session; add one field to `LLMContext` |
| `role` | maker/checker/judge of the caller | gap: not passed to `complete_json`; pass as a keyword, it is not in the cache key |
| `purpose` | `Ledger.stage(name)` current stage (`_current_stage` ContextVar) | direct: stage name becomes `purpose` |
| `trace_id`, `parent_call_id` | none | null (or the build id) |
| `request_key` | `make_key(...)` | direct; `key_scheme = awr.sha256.v1` |
| `prompt_version` | `complete_json(prompt_version=...)` | direct |
| `started_at_ms`, `latency_ms` | none per call | gap: time `transport.chat` in the adapter |
| `model`, `provider` | `ref.model`, `ref.provider` (`ollama`, `openrouter`) | direct |
| `alias`, `tier` | none; `tier = "local"` when provider is `ollama` | adapter |
| `tokens_in` | `RawReply.prompt_tokens` | direct (OpenRouter's prompt_tokens includes cached) |
| `tokens_out` | `RawReply.completion_tokens` | direct |
| `tokens_cache_read`, `tokens_cache_write`, `tokens_reasoning` | not carried by `RawReply` | null (unknown, not zero); fill if transport starts reading them |
| `usage_source` | `reported` when the reply has usage; `estimated` for `charge(reserved)`; `none` when released | adapter |
| `cost_usd` | `settle` return value; `charge` amount; `0.0` for ollama | direct |
| `cost_billed` | true for reported usage at a known price and for ollama; false for `charge` | adapter |
| `price_source` | `provider_reported` if `RawReply.cost_usd` truthy; `price_table` if from `PRICES_PER_MTOK`; `local_zero` for ollama; `none` never (an unpriced paid model raises `UnpricedModel` before dispatch) | adapter |
| `cache_hit`, `saved_tokens_in/out`, `saved_cost_usd` | hit in `complete_json` | gap: `llm_cache` stores no usage, so saved figures are null today; fix: add a nullable `usage_json` column to `llm_cache` (additive) written at `put` time |
| `finish_reason` | not carried | null |
| `outcome` | ok; `ProviderTimeout` -> `timeout`; `MalformedResponse` or a parse failure on the round -> `parse_failure`; other `LLMInfraError` -> `error` | adapter |
| `error_kind` | `type(exc).__name__` of the `LLMInfraError` | adapter |
| `waste_label` | round 2 of a re-prompt or timeout retry -> `retry`, `waste_set_by = "consumer"` | adapter |
| `tags` | `{}` | optional |
| `producer` | `{"name": "awr.llm"}` | constant |

A round that never reached the provider (4xx/5xx, released) is billed nothing. Emitting it is optional; if
emitted, use `outcome = "error"`, `usage_source = "none"`, `tokens_*` 0, `cost_usd = null`,
`price_source = "none"`, `cost_billed = false` (the schema requires that combination for `none`).

## Field mapping: budget-event.v1 from the awr Ledger

| budget-event.v1 | awr.llm source |
|---|---|
| `kind` | `admit` after `reserve` succeeds; `refuse` when it raises |
| `cap_usd` | `Ledger.cap_usd` (`None` = uncapped); `cap_tokens` null (no token cap) |
| `spent_usd` | `Ledger.spent` |
| `reserved_usd` | `sum(_open_reservations)` (`summary()["open_reservations_usd"]`) |
| `remaining_usd` | `max(0, cap - spent - reserved)` |
| `scope` | `{"kind": "kernel"}` (one ledger per process); per-stage caps do not exist |
| `worst_case_usd` | the `_estimate_cost` in `reserve` |
| `refusal.code` | `SpendCapReached` -> `cap_usd`; `UnpricedModel` -> `unpriced_model`; `PaidDisabled` -> `paid_disabled` |

Replay mode (`CacheMiss`) is not a budget event: it surfaces as `complete-json-call.v1` result
`cache_miss_in_replay`.

## complete-json-call.v1 from complete_json

`request.ref` = `ModelRef` (`provider, model, family`); `system`, `prompt`, `prompt_version` verbatim;
`output_schema = out.model_json_schema()`; `sampling = {temperature: 0, max_tokens}`; `max_rounds = 2`;
`cache_mode = ctx.cache.mode`; `request_key = make_key(...)`; `key_scheme = "awr.sha256.v1"`.
`result.parsed` = the validated model, `raw_ref = {store: "llm_cache", key}`, `usage_record_ids` = the records
of its rounds (one `cache_hit` record for a hit), `final_record_id` = the round that parsed.

## Adapter sketch (lives in awork-resume, not in moeka)

```python
# awr/llm/usage.py  (sketch; no change to Ledger or cache semantics)
import json, time, uuid
from jsonschema import Draft202012Validator

SCHEMA_VERSION = "1.0"

class UsageEmitter:
    """Wraps one complete_json call; appends usage-record.v1 lines to a JSONL file."""
    def __init__(self, path, *, consumer="awork-resume", validate=None):
        self._path, self._consumer, self._validate = path, consumer, validate  # validator optional

    def record(self, *, call_id, attempt, ref, purpose, role, session, request_key,
               prompt_version, reply=None, cost=None, price_source="none",
               outcome="ok", error_kind=None, started_at=None, cache_hit=False, saved=None):
        local = ref.provider == "ollama"
        doc = {
            "schema_version": SCHEMA_VERSION, "record_id": f"{call_id}:{attempt}",
            "kind": "cache_hit" if cache_hit else "model_call",
            "call_id": call_id, "attempt": attempt, "consumer": self._consumer,
            "agent": None, "session": session, "role": role, "purpose": purpose,
            "request_key": request_key, "prompt_version": prompt_version,
            "started_at_ms": int((started_at or time.time()) * 1000),
            "latency_ms": None if started_at is None else (time.time() - started_at) * 1000,
            "model": ref.model, "provider": ref.provider, "tier": "local" if local else None,
            "tokens_in": 0 if reply is None else reply.prompt_tokens,
            "tokens_out": 0 if reply is None else reply.completion_tokens,
            "tokens_cache_read": None, "tokens_cache_write": None, "tokens_reasoning": None,
            "usage_source": ("reported" if reply is not None
                             else "estimated" if cost is not None and not cache_hit else "none"),
            "cost_usd": 0.0 if (local or cache_hit) else cost,
            "cost_billed": bool(local or cache_hit
                                or (reply is not None and price_source != "none")),
            "price_source": "cache" if cache_hit else "local_zero" if local else price_source,
            "cache_hit": cache_hit,
            "saved_tokens_in": (saved or {}).get("in"), "saved_tokens_out": (saved or {}).get("out"),
            "saved_cost_usd": (saved or {}).get("usd"),
            "outcome": outcome, "error_kind": error_kind,
            "waste_label": "retry" if attempt > 1 else None,
            "waste_set_by": "consumer" if attempt > 1 else None,
            "producer": {"name": "awr.llm", "version": None},
        }
        if self._validate:
            self._validate.validate(doc)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, sort_keys=True) + "\n")
        return doc["record_id"]
```

Call sites: in `complete_json`, after `ledger.settle(...)` call `record(... reply=reply, cost=actual,
price_source="provider_reported" if reply.cost_usd else "price_table")`; after `ledger.charge(...)`
record with `reply=None`, `outcome="timeout"`, `cost=amount` (the sketch then emits `usage_source`
`estimated` and `cost_billed` false); on a cache hit record `cache_hit=True` before returning. Three extra keyword arguments reach
`complete_json` (`session`, `role`, `purpose` or the stage) and none is part of `make_key`.

## Adopting moeka later: what changes and what does not

- Data model: nothing. The ledger rows above are `usage-record.v1`; with moeka the same documents come from
  `kernel.usage.records()` and `kernel.usage.total(consumer="awork-resume")` replaces `Ledger.summary()`
  (`spent_usd` = `cost_usd`; `stages` = `totals(["purpose"])`; open reservations = `budget()["reserved_usd"]`;
  `SpendCapReached` = `BudgetExceeded` with `reason_code == "cap_usd"`).
- Call shape: `complete_json(ref, system=, prompt=, out=, prompt_version=, max_tokens=)` becomes
  `kernel.llm.complete_json(prompt, system=system, model_cls=out, retries=1, opts=GenerateOptions(model=alias,
  sampling=Sampling(temperature=0, max_tokens=n), prompt_version=version, attribution=Attribution(role=..., purpose=...)))`.
  `prompt_version` is part of moeka's `request_key` when set, and rides on the usage record. `ModelRef.family`
  has no moeka equivalent: keep it in awork-resume's role config; moeka sees only the alias.
- Differences to plan for, none of them a schema change: (1) the cache key scheme differs
  (`moeka.request_key.v1`), so existing `awr.sha256.v1` replay fixtures do not hit under moeka; keep
  `key_scheme` beside each recording (add a nullable column, default `awr.sha256.v1`) and re-record or run both
  schemes side by side; (2) moeka's re-prompt count is `retries` and a native `response_format` round may be
  added, so `max_rounds` is not always 2; (3) moeka counts every attempt including failed ones in `requests`,
  awr's `calls` counts settled calls; (4) awr's per-stage `seconds` is wall time, moeka's `latency_ms` sums
  call latencies; (5) paid-disabled and replay-miss are awork-resume policy and stay in its wrapper.
- Bumping to moeka's latest main: pin `schema_version` major 1 in the consumer's tests (validate against the
  copy of `schemas/` at the pinned commit). An additive minor never breaks that; a major bump is announced by
  `kernel.usage.schema_versions()`.

## Gaps for awork-resume (summary)

1. No per-call id, attempt or latency: the adapter must generate them (small, local).
2. `session` and `role` are not on `LLMContext` or `complete_json`: add two optional fields.
3. Cache hits are unaccounted and their saved tokens are not stored: add nullable `usage_json` to `llm_cache`.
4. `RawReply` carries no cache-read/write, reasoning tokens or finish reason: reported as null, never zero.
5. The cache key differs from moeka's: no data loss, but fixtures replay only under the same `key_scheme`.
