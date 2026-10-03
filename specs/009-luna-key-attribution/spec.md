# Feature Specification: gpt-6-luna and API-key attribution

**Feature Branch**: `feat/009-luna-key-attribution` (base: `consolidate/new-main`)

**Created**: 2026-10-03

**Status**: Draft (implementation-ready; held-out pattern)

**Input**: Owner plan `plan-further-work-on-melodic-sunbeam.md` Phase 1. Moeka moves to `openai/gpt-6-luna` on
OpenRouter. Moeka development uses the **moeka key** (`~/projects/moeka/keys.env`); awork's use of moeka uses the
**awork key** (`~/projects/awork/keys.env`). Both files name the variable `OPENROUTER_API_KEY`, so today nothing
recorded can show which key paid for a call. This spec makes that provable without ever storing a key.

**Held-out pattern** (`004` FR-016): a tester agent writes hidden tests from this spec only; a different implementer
builds from the same spec and never sees those tests. Hidden tests use only the surfaces in the
[Test Harness Contract](#test-harness-contract).

## Terms

- **Key fingerprint (`key_fp`)**: the first 12 lowercase hex characters of `sha256(key.encode("utf-8"))`, where
  `key` is the API key string the provider sends, with surrounding whitespace removed. A provider with no key (OAuth
  providers, local providers, an empty or whitespace-only key) has `key_fp = None`.
- **Call row**: one row of `llm_calls` written by `LLMUsageStore.record`.

## Requirements

### Functional

- **FR-001 Provider fingerprint.** `LLMProvider` (`nanobot/providers/base.py`) exposes a read-only property
  `key_fp: str | None`, computed from `self.api_key` as defined in Terms. Every provider subclass inherits it.
  Changing `api_key` after construction changes `key_fp`, so it must not be computed only once.
- **FR-002 Record carries it.** `LLMCallRecord` (`nanobot/llm_usage/models.py`) gains an optional field
  `key_fp: str | None = None`. `LLMProvider._observe_llm_call` sets it from the provider's `key_fp`.
  `__post_init__` rejects a `key_fp` that is not `None` and not exactly 12 lowercase hex characters (`ValueError`).
- **FR-003 Store persists it.** `llm_calls` gains a nullable `key_fp TEXT` column through the store's existing
  additive-column migration (`nanobot/llm_usage/store.py` column list). Opening a database created before this
  change adds the column, and existing rows read back with `key_fp = None`. `LLMUsageStore.record` writes it.
- **FR-004 Query by key.** `key_fp` is accepted in `GROUP_BY_CALLS` (`nanobot/llm_usage/query.py`), so totals can
  group by it. `UsageFilter` gains `key_fp: str | None = None`, which filters call rows to that fingerprint.
  Event rows (`llm_usage_events`) carry no key; grouping events by `key_fp` is not required (they sit under a
  `None` group or are excluded, consistent with how other call-only columns such as `tier` behave today).
- **FR-005 Export.** Usage rows produced by the read side (`usage-record.v1`) include `key_fp` (string or null).
  `SCHEMA_VERSION` becomes `"1.2"`. `schemas/usage-record.v1.schema.json` documents `key_fp` as an optional
  `["string","null"]` property matching `^[0-9a-f]{12}$`. A 1.1 document stays valid.
- **FR-006 Never the key.** No stored row, trace event, export row, log line or exception message produced by this
  feature contains the API key or any substring of it 8 characters or longer, other than by the fingerprint's
  ordinary chance. `repr()` / `str()` of `LLMCallRecord` show `key_fp`, never the key.
- **FR-007 Luna preset.** `docs/configuration.md` gains a "Model presets: gpt-6-luna" section with a ready preset:
  `{"model": "openai/gpt-6-luna", "provider": "openrouter", "tier": "fast", "price_in_per_mtok": 0.10,
  "price_out_per_mtok": 0.50, "context_window_tokens": 1000000}`, and a key matrix table (consumer → key file →
  variable): moeka gateway and moeka development → `~/projects/moeka/keys.env` `OPENROUTER_API_KEY`; awork's
  moeka → `~/projects/awork/keys.env` `OPENROUTER_API_KEY`. That preset, loaded through `Config`, prices a 1M-input
  + 1M-output call at exactly 0.60 USD through the existing ledger (`_ledger_pricing` / `LedgerObserver`).
- **FR-008 Usage CLI.** `nanobot usage keys` prints one line per distinct `key_fp` in the usage store of the active
  instance (`--workspace` honoured as elsewhere): fingerprint (or `none`), call count, total cost (or `unknown`),
  first and last call time (UTC ISO-8601). `nanobot usage keys --fingerprint-env VAR` additionally prints the
  fingerprint of the environment variable `VAR`'s current value (never the value), so the owner can match a key
  file to rows. Exit 0 with an empty store prints a header only.

### Non-functional

- **NFR-001** No new dependency. `hashlib` only.
- **NFR-002** Existing usage, ledger and schema tests still pass; the store migration is idempotent (opening twice
  adds nothing the second time).

## Test Harness Contract

Hidden tests may use only:
- `nanobot.providers.base.LLMProvider` subclasses constructible in tests (e.g. the OpenAI-compatible provider with a
  fake `api_key`, no network), `.api_key`, `.key_fp`.
- `nanobot.llm_usage.models.LLMCallRecord(...)`, `nanobot.llm_usage.store.LLMUsageStore` / `get_llm_usage_store`
  with a temp `data_dir`, `.record(...)`.
- `nanobot.llm_usage.query`: `UsageFilter`, `GROUP_BY_CALLS`, `SCHEMA_VERSION`, the existing totals and row-export
  functions (by their current names).
- `nanobot.kernel.ledger.LedgerObserver`, `nanobot.providers.factory._ledger_pricing`, `nanobot.config.schema.Config`.
- `schemas/usage-record.v1.schema.json` with `schemas/validate.py`.
- `docs/configuration.md` text.
- The CLI via `python -m nanobot usage keys ...` (or the `nanobot` entry point) with `--workspace <tmp>` and a
  pre-populated store; no gateway, no network.

## Success Criteria

- **SC-001** Two providers built with two different keys produce call rows whose `key_fp` values differ and each
  equal the Terms definition; grouping totals by `key_fp` splits cost and counts exactly.
- **SC-002** A grep of the temp store file, trace output and captured logs for the key finds nothing (FR-006).
- **SC-003** An old-format store opens, migrates, and reads `key_fp = None` for pre-existing rows.
