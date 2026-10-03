# Tasks: gpt-6-luna and API-key attribution

- [x] T1 (FR-001) `key_fingerprint()` and the read-only `LLMProvider.key_fp` property, computed on each
  read from `api_key` (`nanobot/providers/base.py`).
- [x] T2 (FR-002) `LLMCallRecord.key_fp` with `__post_init__` validation (12 lowercase hex chars; the error
  never echoes the value); `_observe_llm_call` sets it.
- [x] T3 (FR-003) `key_fp TEXT` in the store's additive column list; `record()` writes it. Store
  `user_version` stays 4: the migration is keyed on the columns present.
- [x] T4 (FR-004) `key_fp` in `GROUP_BY_CALLS`; `UsageFilter.key_fp` (events carry no key, so a `key_fp`
  filter matches no event row and grouping puts events under `None`).
- [x] T5 (FR-005) `key_fp` on every exported row (`null` on cache hits); `SCHEMA_VERSION = "1.2"` in
  `nanobot/llm_usage/query.py` and `nanobot/kernel/ledger.py` (the `model.call` trace event carries
  `key_fp` too); schema property with pattern `^[0-9a-f]{12}$`; frozen 1.1 copy plus a compatibility
  test; example, `ATTRIBUTION.md` and `CONFORMANCE.md` updated.
- [x] T6 (FR-006) no-leak test: store file, WAL, trace events, export rows and logs hold no 8-char
  substring of the key.
- [x] T7 (FR-007) `docs/configuration.md` "Model presets: gpt-6-luna" section with the preset and key
  matrix; test that the preset prices 1M in + 1M out at 0.60 USD via `_ledger_pricing` /
  `LedgerObserver`. The test image now ships `docs/configuration.md`.
- [x] T8 (FR-008) `nanobot usage keys [--workspace W] [--config F] [--fingerprint-env VAR]`
  (`nanobot/cli/usage.py`), read-only, never migrates or creates a store.
- [x] T9 Tests: `tests/providers/test_key_fingerprint.py`, `tests/llm_usage/test_key_fp.py`,
  `tests/cli/test_usage_keys.py`, `tests/schemas/test_schemas.py`.
