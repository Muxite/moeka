# SPEC-MAP: 009-luna-key-attribution held-out tests

Written from `specs/009-luna-key-attribution/spec.md` only, through the spec's Test Harness
Contract surfaces (existing API names read from the pre-implementation tree `9920dafb`:
`query.totals`, `query.records`, `query.reduce_records`, `LLMUsageStore(path)`,
`get_llm_usage_store(data_dir=...)`, `OpenAICompatProvider(api_key=..., provider_name=...)`).

Files (one per FR): `test_f1_provider_fp.py` (FR-001), `test_f2_record.py` (FR-002),
`test_f3_store_migration.py` (FR-003, SC-003, NFR-002 idempotency), `test_f4_query.py` (FR-004,
SC-001), `test_f5_export_schema.py` (FR-005), `test_f6_never_key.py` (FR-006, SC-002),
`test_f7_luna_preset.py` (FR-007), `test_f8_cli.py` (FR-008), `test_x_cross_cutting.py` (NFR-001,
NFR-002 repo guard). Helpers: `_h009.py` (Terms reference `ref_fp`, fake keys, offline provider,
record builder, faithful pre-009 v1/v4 `llm_calls` DDL for migration tests, leak probe = every
8-char window of the key), `conftest.py` (isolation, per-id summary, `store` and `log_lines`
fixtures).

| file | tests | baseline (9920dafb, pre-implementation) |
|---|---|---|
| f1 test_f1_provider_fp.py | 21 | 21 fail |
| f2 test_f2_record.py | 30 | 30 fail |
| f3 test_f3_store_migration.py | 11 | 11 fail |
| f4 test_f4_query.py | 12 | 12 fail |
| f5 test_f5_export_schema.py | 19 | 15 fail, 4 pass |
| f6 test_f6_never_key.py | 10 | 6 fail, 4 pass |
| f7 test_f7_luna_preset.py | 9 | 7 fail, 2 pass |
| f8 test_f8_cli.py | 13 | 13 fail |
| x test_x_cross_cutting.py | 3 | 3 pass |
| **total** | **128** | **115 fail, 13 pass, 0 errors** |

## Interpretation choices (spec ambiguities)

- **FR-004 events.** Cache-hit events never land in a `key_fp` group (tests assert keyed groups
  have `cache_hits == 0`); whether they sit under the `None` group or are dropped is not tested.
  With `UsageFilter(key_fp=...)` only call rows are asserted (event handling unconstrained, but it
  must not raise).
- **FR-005 cache-hit rows.** `key_fp` may be absent or `null` on `cache_hit` documents; model-call
  documents must carry the key.
- **FR-006 exception text.** A key passed by mistake as `key_fp` must not be echoed in the
  `ValueError` message (an exception "produced by this feature").
- **FR-008 instance.** The test instance has `config.json` in `inst/`, `MOEKA_WORKSPACE=inst`
  and `--workspace inst`; the store is written with identical rows to both `inst/` (config
  directory = `runtime_data_dir`) and `inst/data/` (a config-less `state_dir/data`), so either
  reading of "the active instance" sees the same rows. Output parsing is format-tolerant: rows
  are found by the 12-hex fingerprint (or the word `none`), counts as bare integers, costs as
  decimals (an optional `$` is ignored), times as `YYYY-MM-DDTHH:MM:SS` prefixes (any fraction /
  `Z` / `+00:00` suffix allowed), first time before last time on the line. The header is "at
  least one non-empty line before the first row". A cost of unpriced-only calls must read
  `unknown`, never 0. Mixed priced/unpriced groups are not tested.
- **FR-008 `--fingerprint-env`.** The fingerprint follows Terms (whitespace stripped); the value
  and every 8-char window of it must be absent from stdout+stderr; listing rows still happens.
  An unset variable is not tested.
- **FR-007.** The section is found by a heading matching `Model presets: gpt-6-luna`
  (case-insensitive) and runs to the next heading of the same or higher level. The preset is the
  first JSON object in a fenced block (or inline) whose `model` is `openai/gpt-6-luna`, at any
  nesting depth; camelCase or snake_case keys both accepted. The key matrix: table rows (lines
  starting `|`) mapping moeka gateway + development to `~/projects/moeka/keys.env`
  `OPENROUTER_API_KEY` and awork to `~/projects/awork/keys.env` `OPENROUTER_API_KEY`.
- **NFR-002.** Idempotency is checked on the file (`PRAGMA table_info` identical, `user_version`
  stable, row count unchanged after 3 opens), plus the repo's own usage/ledger/schema tests
  passing in a subprocess.

## Baseline

Pre-implementation tree (`9920dafb`): 115 of 128 fail, 0 errors, 0 collection errors. Every
failure is the missing feature (`LLMCallRecord` rejects `key_fp=`, providers have no `key_fp`,
no `key_fp` column / group / filter, `SCHEMA_VERSION == "1.1"`, schema has no `key_fp` property,
docs section absent, `No such command 'usage'`). The 13 baseline passes pin behaviour the spec
keeps: 1.1 and 1.2 documents validate (FR-005 x4), no key in trace / logs / failure logs today
(FR-006 x4), preset pricing to 0.60 USD through Config/_ledger_pricing/LedgerObserver already
works (FR-007 x2), no new dependency (NFR-001 x2), repo guard tests pass (NFR-002 x1).

Tester self-check: every test also passed (128/128) against a private throwaway reference
implementation of FR-001..FR-008 written by the tester in a scratch copy (deleted; never
shared), so failures against an implementation are not harness bugs by construction of that
check.

## Requirement -> tests

Parametrised tests count once per parameter in the run summary.

| id | tests |
|---|---|
| FR-001 | f1:test_terms_definition_exact<br>f1:test_lower_hex_length_12<br>f1:test_surrounding_whitespace_is_stripped (param)<br>f1:test_inner_whitespace_is_not_stripped<br>f1:test_no_key_is_none (param)<br>f1:test_non_ascii_key_hashes_utf8<br>f1:test_different_keys_differ<br>f1:test_changing_api_key_after_construction_changes_fp<br>f1:test_key_set_after_keyless_construction<br>f1:test_is_a_read_only_property_on_the_base_class<br>f1:test_direct_subclass_inherits<br>f1:test_concrete_provider_classes_do_not_shadow_it<br>f1:test_anthropic_provider_key_fp<br>f2:test_observe_follows_key_change_between_calls<br>f4:test_sc001_two_providers_two_keys_end_to_end<br>f8:test_fingerprint_env_strips_whitespace |
| FR-002 | f2:test_field_exists_optional_default_none<br>f2:test_valid_fingerprint_accepted (param)<br>f2:test_invalid_fingerprint_rejected (param)<br>f2:test_non_string_fingerprint_rejected (param)<br>f2:test_replace_revalidates<br>f2:test_existing_validation_still_applies<br>f2:test_observe_sets_fp_from_provider<br>f2:test_observe_keyless_provider_is_none<br>f2:test_observe_whitespace_key<br>f2:test_observe_follows_key_change_between_calls<br>f2:test_observe_streaming_path_sets_fp<br>f3:test_record_event_path_writes_fp<br>f4:test_sc001_two_providers_two_keys_end_to_end<br>f6:test_record_repr_and_str_show_fp_not_key<br>f6:test_observed_record_repr_has_no_key<br>f6:test_validation_error_does_not_echo_a_key_passed_as_fp |
| FR-003 | f3:test_fresh_store_has_nullable_text_column<br>f3:test_record_writes_fp<br>f3:test_record_round_trips_through_read_side<br>f3:test_record_event_path_writes_fp<br>f3:test_old_store_gains_column_and_old_rows_read_none (param)<br>f3:test_old_store_accepts_new_keyed_rows_after_migration<br>f3:test_migration_is_idempotent<br>f3:test_fresh_store_reopen_adds_nothing<br>f3:test_get_llm_usage_store_with_data_dir<br>f3:test_concurrent_open_of_old_store_converges<br>f4:test_sc001_two_providers_two_keys_end_to_end |
| FR-004 | f4:test_key_fp_is_groupable<br>f4:test_group_by_key_fp_splits_exactly<br>f4:test_group_by_key_fp_and_model<br>f4:test_filter_field_default_none<br>f4:test_filter_restricts_totals<br>f4:test_filter_unknown_fp_matches_nothing<br>f4:test_filter_restricts_records<br>f4:test_filter_combines_with_other_fields<br>f4:test_events_do_not_break_group_by_key_fp<br>f4:test_events_do_not_break_key_fp_filter<br>f4:test_reference_reducer_agrees_with_store_totals<br>f4:test_sc001_two_providers_two_keys_end_to_end |
| FR-005 | f3:test_record_round_trips_through_read_side<br>f4:test_filter_restricts_records<br>f4:test_reference_reducer_agrees_with_store_totals<br>f5:test_schema_version_is_1_2<br>f5:test_row_has_key_fp_and_version<br>f5:test_key_fp_is_string_or_null_in_json<br>f5:test_totals_carry_new_version<br>f5:test_cache_hit_rows_have_no_key<br>f5:test_schema_documents_key_fp<br>f5:test_exported_rows_validate_strict<br>f5:test_1_1_document_stays_valid<br>f5:test_1_2_document_with_key_fp_valid (param)<br>f5:test_bad_key_fp_rejected_by_schema (param)<br>f5:test_bad_key_fp_rejected_by_reader_profile_too<br>f6:test_export_rows_never_hold_the_key |
| FR-006 | f6:test_store_files_never_hold_the_key<br>f6:test_trace_events_never_hold_the_key<br>f6:test_captured_logs_never_hold_the_key<br>f6:test_observer_failure_log_never_holds_the_key<br>f6:test_store_failure_log_never_holds_the_key<br>f6:test_export_rows_never_hold_the_key<br>f6:test_record_repr_and_str_show_fp_not_key<br>f6:test_observed_record_repr_has_no_key<br>f6:test_validation_error_does_not_echo_a_key_passed_as_fp<br>f6:test_fp_is_not_a_key_substring<br>f7:test_docs_contain_no_key_material<br>f8:test_fingerprint_env_prints_fp_never_value<br>f8:test_cli_never_prints_any_env_key |
| FR-007 | f7:test_section_exists<br>f7:test_section_preset_values_match_spec<br>f7:test_section_preset_loads_through_config_and_prices_060<br>f7:test_spec_preset_prices_060_through_ledger<br>f7:test_spec_preset_camelcase_form_loads<br>f7:test_key_matrix_moeka_rows<br>f7:test_key_matrix_awork_row<br>f7:test_key_matrix_has_header_columns<br>f7:test_docs_contain_no_key_material |
| FR-008 | f8:test_exit_zero_and_one_line_per_fp<br>f8:test_counts_and_costs<br>f8:test_none_row_counts_keyless_calls<br>f8:test_unpriced_cost_is_unknown<br>f8:test_first_and_last_call_times_utc_iso<br>f8:test_has_a_header<br>f8:test_empty_store_prints_header_only<br>f8:test_missing_store_file_is_empty_not_an_error<br>f8:test_fingerprint_env_prints_fp_never_value<br>f8:test_fingerprint_env_strips_whitespace<br>f8:test_fingerprint_env_unmatched_key_still_listed_rows<br>f8:test_cli_never_prints_any_env_key<br>f8:test_workspace_selects_the_instance |
| NFR-001 | x:test_no_new_runtime_dependency<br>x:test_no_third_party_hash_library_imported |
| NFR-002 | f3:test_migration_is_idempotent<br>f3:test_fresh_store_reopen_adds_nothing<br>f3:test_concurrent_open_of_old_store_converges<br>x:test_existing_usage_ledger_schema_tests_pass |
| SC-001 | f4:test_group_by_key_fp_splits_exactly<br>f4:test_sc001_two_providers_two_keys_end_to_end |
| SC-002 | f6:test_store_files_never_hold_the_key<br>f6:test_trace_events_never_hold_the_key<br>f6:test_captured_logs_never_hold_the_key |
| SC-003 | f3:test_old_store_gains_column_and_old_rows_read_none (param)<br>f3:test_old_store_accepts_new_keyed_rows_after_migration |
