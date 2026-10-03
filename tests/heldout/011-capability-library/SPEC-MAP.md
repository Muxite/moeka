# SPEC-MAP: 011-capability-library held-out tests

Written from `specs/011-capability-library/spec.md` (branch `feat/011-capability-library`, spec
commit `f0533069`) only, through the spec's Test Harness Contract surfaces plus the public kernel
API (`moeka.Environment.from_config`, `Kernel`, `AgentSpec`, `FakeProvider`, `MemoryTraceSink`).
`nanobot.capabilities.*` is imported lazily inside each test (`_h011.cap`), so a tree without the
feature collects cleanly and fails per requirement id (`ModuleNotFoundError` inside the test).

Files: `test_a_vocabulary.py` (FR-001), `test_a_manifest.py` (FR-002, FR-003),
`test_b_index.py` (FR-004, SC-003 incremental sync), `test_c_task_types.py` (FR-005, one table per
rule), `test_d_select.py` (FR-006..FR-009, SC-003 soundness, SC-002 at index level),
`test_d_prompt.py` (FR-010, FR-011, FR-012, FR-015, SC-001, SC-002 through the prompt),
`test_e_library.py` (FR-013 CLI, FR-014), `test_x_nonfunctional.py` (NFR-001..004, SC-004).
Helpers: `_h011.py` (lazy imports, the FR-001 reference vocabulary, a Python reference of FR-007
rules 1-4, the FR-008 lexical scorer, FR-004 reference sync counts, fixture skill libraries, kernel /
ContextBuilder construction), `conftest.py` (isolation, loguru capture, per-id summary),
`golden_sc001_prompt.txt` (SC-001 golden, see below).

Property tests use a seeded `random.Random` loop of 500 examples each (hypothesis is not in the
project's dev dependencies): `b:test_incremental_sync_equals_fresh_sync_property` (seeds
11000..11499) and `d1:test_soundness_sql_filter_equals_python_reference` (seeds 7000..7499).

| file | tests collected | baseline (spec commit f0533069) |
|---|---|---|
| test_a_vocabulary.py | 90 | 90 fail |
| test_a_manifest.py | 45 | 43 fail, 2 pass |
| test_b_index.py | 11 | 4 fail, 7 error (fixture import) |
| test_c_task_types.py | 121 | 121 fail |
| test_d_select.py | 69 | 69 fail |
| test_d_prompt.py | 49 | 48 fail, 1 pass |
| test_e_library.py | 38 | 37 fail, 1 pass |
| test_x_nonfunctional.py | 9 (1 `bench`, deselected by default) | 5 fail, 3 pass |
| **total** | **432 (431 default)** | **417 fail + 7 error, 7 pass** |

Tests that pass on the baseline pin behaviour the spec keeps: built-in skills are not validated at
runtime and a valid workspace skill still shadows a built-in (FR-003), the pre-feature golden prompt
with default config (SC-001), the bundled skills exist (FR-014 precondition), no new runtime
dependency (NFR-003), no new allow-list entries and the repo guards pass (NFR-004).

Tester self-check: every default-run test (431/431) and both property tests also passed against a
private throwaway reference implementation of the spec written by the tester in a scratch copy of
the spec commit (deleted; never shared), so failures against an implementation are not harness bugs
by construction of that check. Mutation check: removing "sudo implies exec" from that reference
failed FR-007 (2), FR-008 (1) and SC-003 (1). The `bench` test (NFR-001) was ~6 ms p99 on the naive
reference with one index added (42 ms without), so the 5 ms target is meaningful and reachable.

## Requirement -> tests

Parametrised tests count once per parameter in the run summary. Prefixes: a1 vocabulary,
a2 manifest, b index, c task types, d1 select, d2 prompt, e library, x non-functional.

| id | tests |
|---|---|
| FR-001 | a1:test_types_yaml_is_shipped_and_loads<br>a1:test_every_listed_type_is_a_type (param)<br>a1:test_unknown_strings_are_not_types (param)<br>a1:test_ancestors_chain_matches_listed_parents (param)<br>a1:test_ancestors_examples_self_first_any_last<br>a1:test_satisfies_full_truth_table_over_listed_types<br>a1:test_satisfies_examples (param)<br>a1:test_unknown_type_raises_unknown_type_error (param)<br>a1:test_none_has_no_parent_and_nothing_below_it |
| FR-002 | a2:test_no_block_is_none<br>a2:test_shorthand_inputs_and_defaults<br>a2:test_dict_form_inputs<br>a2:test_dict_form_with_only_any<br>a2:test_dict_form_with_only_all<br>a2:test_empty_block_takes_all_defaults<br>a2:test_missing_inputs_is_empty<br>a2:test_all_fields_are_sorted_tuples<br>a2:test_iospec_is_frozen_and_equal_by_value<br>a2:test_every_effect_value_is_accepted (param)<br>a2:test_none_is_a_valid_output<br>a2:test_manifest_errors_name_the_field (param)<br>e:test_tool_base_io_defaults_to_none |
| FR-003 | a2:test_valid_skill_without_io<br>a2:test_valid_skill_with_io<br>a2:test_each_invalid_skill_reports_problems<br>a2:test_description_at_limit_is_valid<br>a2:test_loader_skips_invalid_workspace_skills_and_warns<br>a2:test_invalid_workspace_skill_is_not_explicitly_invocable<br>a2:test_builtin_skills_are_not_validated_at_runtime<br>a2:test_valid_workspace_skill_with_io_still_shadows_builtin<br>e:test_validate_all_valid_exits_zero<br>e:test_validate_invalid_exits_one_and_lists_each |
| FR-004 | b:test_entry_and_result_types_exist<br>b:test_first_sync_adds_everything<br>b:test_resync_same_list_is_all_unchanged<br>b:test_sha_change_is_an_update<br>b:test_availability_change_is_an_update<br>b:test_removed_and_added<br>b:test_kind_is_part_of_identity<br>b:test_index_persists_across_instances<br>b:test_database_uses_wal<br>b:test_index_reflects_sync_in_selection<br>b:test_incremental_sync_equals_fresh_sync_property<br>d2:test_kernel_index_lives_in_state_data_dir |
| FR-005 | c:test_result_shape<br>c:test_no_signal_no_types_and_no_ambient (param)<br>c:test_attachment_by_name (param)<br>c:test_attachment_by_mime (param)<br>c:test_attachment_mime_and_name_agree<br>c:test_attachment_with_none_mime_uses_name<br>c:test_unknown_attachment_adds_nothing (param)<br>c:test_several_attachments_union<br>c:test_urls (param)<br>c:test_url_with_file_extension_adds_extension_type (param)<br>c:test_two_urls_union<br>c:test_paths (param)<br>c:test_non_absolute_text_is_not_a_path (param)<br>c:test_nonexistent_path_is_still_typed<br>c:test_fences (param)<br>c:test_two_fences_union<br>c:test_type_hints (param)<br>c:test_requested_output (param)<br>c:test_requested_output_last_match_wins (param)<br>c:test_out_hint_overrides_when_valid (param)<br>c:test_no_requested_output<br>c:test_combined_rules<br>c:test_pure_no_filesystem_no_network<br>c:test_deterministic_in_process<br>c:test_does_not_mutate_inputs<br>c:test_deterministic_across_processes<br>d1:test_lexical_ranker_deterministic_across_processes<br>d1:test_sc002_pdf_attachment_markdown |
| FR-006 | d1:test_selection_fields<br>d1:test_selected_is_first_k_survivors<br>d1:test_task_types_include_extracted_and_ambient<br>d1:test_text_query_is_always_ambient |
| FR-007 | d2:test_filtered_unavailable_skill_keeps_marker<br>d2:test_filtered_allowed_effects_from_config<br>d2:test_filtered_ambient_types_from_config<br>d1:test_text_query_is_always_ambient<br>d1:test_rule1_unavailable_tool_dropped_unavailable_skill_kept<br>d1:test_rule2_effects_policy (param)<br>d1:test_rule2_missing_effects_means_exec<br>d1:test_rule3_inputs_all (param)<br>d1:test_rule3_inputs_any (param)<br>d1:test_rule3_ambient_types_count<br>d1:test_rule4_outputs (param)<br>d1:test_untyped_always_passes_type_rules<br>d1:test_soundness_sql_filter_equals_python_reference<br>d1:test_sc002_pdf_attachment_markdown |
| FR-008 | d1:test_soundness_sql_filter_equals_python_reference<br>d1:test_custom_ranker_is_honoured_and_called_with_survivors<br>d1:test_ties_break_by_kind_then_name<br>d1:test_negative_and_fractional_scores_order<br>d1:test_default_lexical_ranker_matches_reference (param)<br>d1:test_lexical_df_is_over_survivors_only<br>d1:test_lexical_ranker_deterministic_across_processes |
| FR-009 | d2:test_filtered_empty_fallback_is_todays_listing<br>d2:test_filtered_small_fallback_is_todays_listing<br>d2:test_capability_selected_reports_empty_fallback<br>d1:test_empty_fallback_selects_everything<br>d1:test_small_fallback_no_filtering<br>d1:test_small_fallback_does_not_reorder<br>d1:test_small_boundary (param)<br>d1:test_no_fallback_when_some_survive<br>d1:test_sc002_pdf_attachment_markdown<br>e:test_explain_k_and_small_fallback |
| FR-010 | d2:test_config_defaults<br>d2:test_config_accepts_camel_and_snake<br>d2:test_config_rejects_invalid (param)<br>d2:test_config_top_k_bounds_inclusive (param)<br>d2:test_sc001_golden_default_config<br>d2:test_sc001_golden_explicit_all (param)<br>d2:test_sc001_all_is_byte_identical_to_default (param)<br>d2:test_sc001_kernel_prompt_identical<br>d2:test_filtered_lists_at_most_k_and_respects_types<br>d2:test_filtered_keeps_active_skills_section<br>d2:test_filtered_line_format_unchanged<br>d2:test_filtered_unavailable_skill_keeps_marker<br>d2:test_explicit_skill_bypasses_filter_and_is_not_counted<br>d2:test_filtered_query_changes_listing<br>d2:test_filtered_allowed_effects_from_config<br>d2:test_filtered_ambient_types_from_config<br>d2:test_filtered_empty_fallback_is_todays_listing<br>d2:test_filtered_small_fallback_is_todays_listing<br>d2:test_kernel_filtered_run<br>d2:test_kernel_explicit_skill_still_loaded |
| FR-011 | d2:test_all_mode_emits_no_capability_selected<br>d2:test_capability_selected_event<br>d2:test_capability_selected_reports_empty_fallback<br>d2:test_no_event_when_trace_is_off<br>d2:test_one_event_per_build<br>d2:test_kernel_filtered_run<br>d2:test_fingerprint_emits_no_capability_selected |
| FR-012 | d2:test_fingerprint_stable_for_same_config<br>d2:test_fingerprint_differs_when_capabilities_config_differs (param)<br>d2:test_fingerprint_emits_no_capability_selected |
| FR-013 | e:test_list_plain<br>e:test_list_json<br>e:test_explain_text_message<br>e:test_explain_with_attachment<br>e:test_explain_k_and_small_fallback<br>e:test_explain_uses_the_given_workspace<br>e:test_validate_all_valid_exits_zero<br>e:test_validate_invalid_exits_one_and_lists_each<br>e:test_cli_makes_no_model_call_without_credentials |
| FR-014 | e:test_bundled_skills_exist<br>e:test_every_bundled_skill_has_valid_io (param)<br>e:test_tool_base_io_defaults_to_none<br>e:test_builtin_tool_effects_are_truthful (param)<br>e:test_read_only_tools_do_not_claim_none (param) |
| FR-015 | d2:test_kernel_index_lives_in_state_data_dir<br>d2:test_kernel_unusable_index_degrades_to_all<br>d2:test_context_unusable_index_degrades_to_all<br>d2:test_index_follows_skill_changes |
| NFR-001 | x:test_filter_p99_under_5ms_on_1000_capabilities |
| NFR-002 | x:test_labelled_benchmark_cases_exist<br>x:test_repo_benchmark_test_passes |
| NFR-003 | x:test_no_new_runtime_dependency<br>x:test_capabilities_import_without_vec_extra |
| NFR-004 | x:test_capabilities_import_without_vec_extra<br>x:test_capabilities_package_imports_no_host_package<br>x:test_capabilities_import_leaks_no_host_module<br>x:test_no_new_allowlist_entries<br>x:test_repo_guards_pass |
| SC-001 | d2:test_sc001_golden_default_config<br>d2:test_sc001_golden_explicit_all (param)<br>d2:test_sc001_all_is_byte_identical_to_default (param)<br>d2:test_sc001_kernel_prompt_identical<br>d2:test_all_mode_emits_no_capability_selected |
| SC-002 | d2:test_filtered_lists_at_most_k_and_respects_types<br>d2:test_kernel_filtered_run<br>d1:test_sc002_pdf_attachment_markdown |
| SC-003 | b:test_incremental_sync_equals_fresh_sync_property<br>d1:test_soundness_sql_filter_equals_python_reference |
| SC-004 | x:test_filter_p99_under_5ms_on_1000_capabilities<br>x:test_labelled_benchmark_cases_exist<br>x:test_repo_benchmark_test_passes |

## Not covered by a hidden test (and why)

- **NFR-002 recall / token targets**: the labelled benchmark's fixture library (>=100 capabilities)
  is a repo deliverable with no specified format, so the hidden suite checks the shape of
  `tests/capabilities/bench/cases.jsonl` (>=150 cases with message / attachments / expected) and runs
  the repo's own test(s) under `tests/capabilities` that read it (`x:test_repo_benchmark_test_passes`).
  Reviewers should confirm that test really asserts recall@8 >= 0.95 and >= 50% token reduction.
- **FR-008 vec ranking**: the contract forbids the vec extra; only the lexical scorer and custom
  rankers are tested (and that the capabilities modules import without `sqlite_vec`).
- **FR-015 "whenever the skills list changes" cheapness** ("sync touches only changed rows") is
  covered at index level by the FR-004 counts, not through the loop.
- **FR-016** (referenced by FR-003, not defined) is read as FR-014's built-in validation.
- **NFR-001** is the `bench` test only (deselected by default; run with `-m bench`).

## Surface assumptions (double-check against the implementation)

1. `CapabilityEntry(kind=, name=, description=, io=, content_sha256=, available=)` keyword
   construction; `io` is the `IOSpec` returned by `parse_io` (or `None` for untyped).
2. `TaskTyping(types=frozenset, requested_output=)` keyword construction; `select(...)` receives
   `ambient` and `allowed_effects` as tuples.
3. A custom `ranker(query, survivors)` may receive names, objects with `.name`, `(kind, name, ...)`
   tuples or dicts; the test rankers accept all of these and return `{name: score}`.
4. `text/query` is added by the selector even when `ambient` omits it ("text/query always" + "the
   selector adds them"); `Selection.task_types` = ambient types | extracted types.
5. `Selection.survivors == ()` on the empty fallback; on the small fallback only `selected` is
   checked (set equality, and order independent of query / ranker = "no reordering").
6. Index content equality (SC-003) is checked through behaviour, not a schema: identical
   `SyncResult` counts vs a reference, equal per-table row counts between the incremental and the
   fresh database, equal selections (custom and default ranker) and an all-unchanged resync.
7. Untyped capabilities: the property test generates them only when `exec` is allowed and always
   allows `none`, so both readings of "untyped has no effects" vs "missing effects = exec", and of
   whether an `effects: [none]` capability needs `none` allowed, agree.
8. FR-005 strict readings: a URL contributes no `path`; a URL on a git / video host is not also
   `url/web`; a generic URL ending in an extension adds that type (with or without `url/web`, both
   accepted); relative paths (`docs/x.pdf`), `read/write`, `50/50` are not paths; the closing fence
   of a block is not a second (language-less) fence; `$type: x` (space) is not a hint; `requested
   output` text adds no task type; extensions are matched lowercase only.
9. FR-008 lexical: token sets (no multiplicity); a capability's tokens are its name split on `-`/`_`
   plus its description; `df` counts survivors only (`d1:test_lexical_df_is_over_survivors_only`
   would order differently with df over the whole index).
10. FR-010 config reaches a bare `ContextBuilder` through `env.config.section("capabilities")` of a
    host env built by `Environment.from_config(Config(capabilities=...))`, and also through a
    constructor keyword when one of `capabilities`, `capabilities_config`, `capability_config`,
    `capabilities_cfg` exists (passed `Config().capabilities`). The turn text is
    `build_system_prompt(query=...)`. Kernel tests need no such guess (the `Config` is the env's).
11. FR-010 `$skill` bypass: the invoked skill is not counted toward `k`; its body still reaches the
    user turn (`build_current_message`, unchanged); whether it also appears in the summary is not
    asserted.
12. FR-011: the stripped event has exactly the six spec keys; no `capability.selected` under
    `selection="all"` (SC-001 "changes nothing") nor from `agent.fingerprint()` (trace off).
13. FR-012 is observed through `Kernel(...).agent(...).fingerprint().digest`, including pairs whose
    rendered prompts are identical (`top_k` 63 vs 64, `"all"` with different filter settings).
14. FR-015: the database path is made unusable by a directory at `<data_dir>/capabilities.db`; "logs
    once" = exactly one loguru record at WARNING or above mentioning "capabilit" over two turns.
15. FR-004 default path: `kernel.env.paths.data_dir / "capabilities.db"` exists after a filtered
    run; nothing named `capabilities.db*` under the work dir.
16. FR-013 CLI: `python -m nanobot capabilities ...` with a temp `HOME`, no config file and no keys;
    `--json` output is a list of records (or a dict holding one) with `kind`, `name`, `io`
    (`null` / `"untyped"` when untyped) and `available`; the 12-char sha prefix appears in each
    record; tools (at least `read_file`) are listed; `explain` prints extracted types, the requested
    output, survivor names and the word "fallback", and `--k 64` on the small fixture prints "small".
17. FR-014 tool effects are read from the class attribute `io` of `ExecTool`, `ExecSessionTool`,
    `BackgroundShellTool` (exec), `WebSearchTool`, `WebFetchTool` (network), `ReadFileTool`,
    `ListDirTool`, `GrepTool`, `FindFilesTool` (read-workspace), `WriteFileTool`, `EditFileTool`,
    `ApplyPatchTool` (write-workspace).
18. FR-003 warning: at least one WARNING names each skipped skill and contains
    `validate_skill(path)[0]`; no warning names a valid skill.

## SC-001 golden

`golden_sc001_prompt.txt` is the system prompt the spec commit `f0533069` (pre-feature) rendered
for `_h011.sc001_fixture` with `PDF_QUERY`, normalised by `_h011.normalise_prompt` (temp root ->
`<ROOT>`, Python version -> `<V>`). The comparison sorts each run of skills-summary lines because
directory iteration order is filesystem-dependent; exact order is checked by the in-process
comparisons (`d2:test_sc001_all_is_byte_identical_to_default`, kernel variant).

## Baseline (2026-10-03, spec commit f0533069, git archive copy)

417 failed, 7 errors, 7 passed of 431 (default run). Failing reasons are all missing features
(`ModuleNotFoundError: nanobot.capabilities`, `Config` has no `capabilities` block, no
`capabilities` CLI command, `Tool.io` absent, no `io` blocks in bundled skills, invalid workspace
skills still listed). 22 requirement ids have at least one failing test.
