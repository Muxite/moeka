# SPEC-MAP: 006-rsi-kernel-prerequisites held-out tests

Written from `specs/006-rsi-kernel-prerequisites/spec.md` (branch `spec/wave2`) only, through the
spec's Test Harness Contract surfaces. Open questions use the spec defaults: 006-Q1 (FR-033
`ModelSpec.unsupported_sampling`) = **yes, built**; 006-Q2 (infer `skill.read` from shell) = **no**.

Files (one per K item): `test_k1_fingerprint_skills.py` (K1), `test_k2_param_descriptions.py` (K2),
`test_k3_args_digest.py` (K3), `test_k4_max_tool_errors.py` (K4), `test_k5_strict_sampling.py` (K5),
`test_k6_skill_read.py` (K6), `test_x_cross_cutting.py` (FR-041, FR-042). Helpers: `_h006.py`
(environment, scripted tool calls, reference formulas for FR-003/FR-004 and FR-015), `conftest.py`
(isolation, per-id summary).

| file | tests | baseline (consolidate/new-main c5340d24) |
|---|---|---|
| K1 test_k1_fingerprint_skills.py | 31 | 30 fail, 1 pass |
| K2 test_k2_param_descriptions.py | 55 | 55 fail |
| K3 test_k3_args_digest.py | 39 | 39 fail |
| K4 test_k4_max_tool_errors.py | 30 | 21 fail, 9 pass |
| K5 test_k5_strict_sampling.py | 28 | 27 fail, 1 pass |
| K6 test_k6_skill_read.py | 26 | 12 fail, 14 pass |
| cross-cutting test_x_cross_cutting.py | 3 | 1 fail, 2 pass |
| **total** | **212** | **185 fail, 27 pass** |

Tests that pass on the baseline do so legitimately: they pin behaviour the spec says must stay
unchanged (RunLimits validation FR-020, `max_tool_errors=None` FR-026/SC-004 second half, drop default
FR-029, no `skill.read` for failures/non-skill files/other grep modes/gate denials/summary FR-038,
no exec inference FR-039, fingerprint emits no `skill.read` FR-006, repo guards FR-042).

Tester self-check: every test also passed (212/212) against a private throwaway reference
implementation of K1-K6 written by the tester in a scratch copy (deleted; never shared), so
failures against an implementation are not harness bugs by construction of that check.

## Requirement -> tests

Parametrised tests count once per parameter in the run summary.

| id | tests |
|---|---|
| FR-001 | k1:test_components_have_exactly_five_keys_and_digest_formula |
| FR-002 | k1:test_exact_recomputation_builtin_workspace_inline<br>k1:test_always_on_and_unavailable_skills_are_members<br>k1:test_one_byte_in_skill_body_changes_only_skills_and_digest<br>k1:test_skills_exclude_removes_member<br>k1:test_skills_include_restricts_members<br>k1:test_shadowed_builtin_is_not_a_member<br>k1:test_inline_skill_shadows_file_skill<br>k1:test_bundled_skills_count_without_variant_dir |
| FR-003 | k1:test_exact_recomputation_builtin_workspace_inline<br>k1:test_dangling_symlink_is_not_a_counted_file<br>k1:test_always_on_and_unavailable_skills_are_members<br>k1:test_one_byte_in_skill_body_changes_only_skills_and_digest<br>k1:test_skipped_paths_do_not_change_component (param)<br>k1:test_inline_skill_shadows_file_skill<br>k1:test_inline_skill_values_enter_the_record<br>k1:test_unreadable_skipped_file_is_not_read |
| FR-004 | k1:test_exact_recomputation_builtin_workspace_inline<br>k1:test_empty_effective_skill_set_is_sha_of_empty_bytes |
| FR-005 | k1:test_one_byte_in_skill_body_changes_only_skills_and_digest<br>k1:test_counted_file_changes_change_component (param)<br>k1:test_skipped_paths_do_not_change_component (param)<br>k1:test_tree_location_does_not_matter<br>k1:test_mtime_only_change_is_ignored<br>k1:test_skills_exclude_removes_member<br>k1:test_shadowed_builtin_is_not_a_member |
| FR-006 | k1:test_one_byte_in_skill_body_changes_only_skills_and_digest<br>k1:test_unreadable_counted_file_raises_oserror<br>k1:test_unreadable_skipped_file_is_not_read<br>k1:test_fingerprint_emits_no_skill_read |
| FR-007 | k2:test_default_empty_equal_and_hashable<br>k2:test_stored_frozen |
| FR-008 | k2:test_type_errors (param)<br>k2:test_invalid_path_grammar_is_value_error (param)<br>k2:test_valid_path_grammar (param) |
| FR-009 | k2:test_us4_read_file_path_description<br>k2:test_nested_array_item_paths<br>k2:test_description_added_when_absent |
| FR-010 | k2:test_us4_read_file_path_description<br>k2:test_override_reaches_provider_request_and_fingerprint<br>k2:test_per_kernel_isolation_and_base_class_untouched<br>k2:test_two_agents_same_kernel_share_variant |
| FR-011 | k2:test_unknown_tool_entry_ignored<br>k2:test_tool_not_loaded_by_scope_is_ignored<br>k2:test_unresolvable_path_raises_variant_error_on_tools (param)<br>k2:test_variant_error_on_first_fingerprint<br>k2:test_variant_error_on_first_run<br>k2:test_variant_error_is_public_value_error |
| FR-012 | k2:test_actions_are_not_overridden<br>k2:test_builtins_beyond_filesystem_are_covered |
| FR-013 | k2:test_validation_is_unchanged<br>k2:test_validation_unchanged_without_trace_dependency |
| FR-014 | k3:test_matches_reference_and_is_lower_hex (param)<br>k3:test_every_tool_call_kind_carries_the_digest<br>k3:test_digest_failure_does_not_change_outcome |
| FR-015 | k3:test_key_order_does_not_matter<br>k3:test_list_order_matters<br>k3:test_unicode_is_not_ascii_escaped<br>k3:test_none_is_empty_object<br>k3:test_json_string_is_parsed<br>k3:test_unparsable_string_is_a_json_string_value (param)<br>k3:test_unserialisable_gives_none (param)<br>k3:test_circular_gives_none_and_does_not_raise<br>k3:test_no_numeric_normalisation<br>k3:test_matches_reference_and_is_lower_hex (param)<br>k3:test_edge_case_arguments_in_a_run |
| FR-016 | k3:test_args_digest_is_exported<br>k3:test_key_order_does_not_matter<br>k3:test_list_order_matters<br>k3:test_unicode_is_not_ascii_escaped<br>k3:test_none_is_empty_object<br>k3:test_json_string_is_parsed<br>k3:test_unparsable_string_is_a_json_string_value (param)<br>k3:test_unserialisable_gives_none (param)<br>k3:test_circular_gives_none_and_does_not_raise<br>k3:test_no_numeric_normalisation<br>k3:test_matches_reference_and_is_lower_hex (param)<br>k3:test_every_tool_call_kind_carries_the_digest<br>k3:test_edge_case_arguments_in_a_run<br>k3:test_sc003_two_distinct_digests |
| FR-017 | k3:test_every_tool_call_kind_carries_the_digest |
| FR-018 | k3:test_no_raw_arguments_and_only_one_new_key |
| FR-019 | k3:test_circular_gives_none_and_does_not_raise<br>k3:test_edge_case_arguments_in_a_run<br>k3:test_digest_failure_does_not_change_outcome |
| FR-020 | k4:test_positive_limits_build (param)<br>k4:test_invalid_limits_raise_value_error (param) |
| FR-021 | k4:test_sc004_three_errors_three_calls<br>k4:test_errors_spread_over_iterations<br>k4:test_every_failure_kind_counts<br>k4:test_successes_do_not_count<br>k4:test_ask_user_alone_is_not_a_tool_error<br>k4:test_gate_denials_are_not_tool_errors |
| FR-022 | k4:test_count_is_per_run_same_session<br>k4:test_count_resets_after_a_tool_error_stop |
| FR-023 | k4:test_sc004_three_errors_three_calls<br>k4:test_all_calls_of_the_response_execute<br>k4:test_errors_spread_over_iterations<br>k4:test_every_failure_kind_counts<br>k4:test_async_run |
| FR-024 | k4:test_ask_user_wins_over_tool_error<br>k4:test_ask_user_alone_is_not_a_tool_error<br>k4:test_policy_denials_win_over_tool_error<br>k4:test_tool_error_before_iteration_budget<br>k4:test_tool_error_on_single_allowed_iteration |
| FR-025 | k4:test_sc004_three_errors_three_calls<br>k4:test_all_calls_of_the_response_execute<br>k4:test_stream_ends_with_run_failed<br>k4:test_async_run |
| FR-026 | k4:test_sc004_none_never_tool_error<br>k4:test_successes_do_not_count<br>k4:test_n_minus_one_errors_then_complete<br>k4:test_none_tolerates_many_errors |
| FR-028 | k5:test_field_default_and_values<br>k5:test_invalid_values_raise (param)<br>k5:test_equality_and_hash |
| FR-029 | k5:test_us3_drop_sends_without_seed<br>k5:test_default_is_drop_one_event_per_call<br>k5:test_passthrough_provider_sends_under_drop<br>k5:test_host_declared_unsupported_drop_agent |
| FR-030 | k5:test_us3_raise_makes_zero_provider_calls<br>k5:test_fields_in_sampling_field_order<br>k5:test_run_sampling_argument_is_explicit<br>k5:test_supported_explicit_sampling_runs_under_raise<br>k5:test_raise_stream_and_async<br>k5:test_defaults_quiet_explicit_strict |
| FR-031 | k5:test_model_defaults_dropped_quietly_under_raise<br>k5:test_defaults_quiet_explicit_strict<br>k5:test_host_declared_unsupported_default_is_quiet |
| FR-032 | k5:test_passthrough_provider_fails_closed_under_raise<br>k5:test_passthrough_provider_sends_under_drop<br>k5:test_passthrough_fails_closed_for_llm_complete_raise<br>k5:test_declared_support_is_respected<br>k5:test_host_declared_unsupported_raise_agent |
| FR-033 | k5:test_modelspec_field_and_validation<br>k5:test_host_declared_unsupported_raise_agent<br>k5:test_host_declared_unsupported_drop_agent<br>k5:test_host_declared_unsupported_llm_complete<br>k5:test_host_declared_unsupported_default_is_quiet |
| FR-034 | k5:test_no_honoured_event |
| FR-035 | k6:test_read_file_emits_with_via_and_call_id<br>k6:test_read_file_range_emits<br>k6:test_two_reads_in_one_response<br>k6:test_read_file_builtin_root_skill<br>k6:test_nested_skills_dir_counts<br>k6:test_us5_grep_content_one_event_per_skill_file<br>k6:test_grep_default_mode_is_content<br>k6:test_grep_k_skill_files<br>k6:test_grep_head_limit_only_returned_files |
| FR-036 | k6:test_read_file_emits_with_via_and_call_id<br>k6:test_two_reads_in_one_response<br>k6:test_read_file_builtin_root_skill<br>k6:test_us5_grep_content_one_event_per_skill_file |
| FR-037 | k6:test_read_file_emits_with_via_and_call_id<br>k6:test_read_file_range_emits<br>k6:test_two_reads_in_one_response<br>k6:test_us5_grep_content_one_event_per_skill_file<br>k6:test_grep_k_skill_files |
| FR-038 | k1:test_fingerprint_emits_no_skill_read<br>k6:test_non_skill_files_do_not_emit (param)<br>k6:test_lowercase_skill_md_is_not_a_skill_file<br>k6:test_missing_skill_file_does_not_emit<br>k6:test_directory_named_skill_md_does_not_emit<br>k6:test_too_large_skill_file_does_not_emit<br>k6:test_unreadable_skill_file_does_not_emit<br>k6:test_gate_denied_read_does_not_emit<br>k6:test_summary_and_always_on_do_not_emit<br>k6:test_us5_grep_other_modes_do_not_emit (param)<br>k6:test_grep_no_match_and_non_skill_matches |
| FR-039 | k6:test_exec_reads_are_not_inferred<br>k6:test_events_catalogue_text |
| FR-040 | k3:test_events_catalogue_mentions_args_digest<br>k6:test_events_catalogue_text |
| FR-041 | k2:test_default_empty_equal_and_hashable<br>k2:test_variant_error_is_public_value_error<br>k3:test_args_digest_is_exported<br>k5:test_field_default_and_values<br>k5:test_modelspec_field_and_validation<br>x:test_public_exports |
| FR-042 | x:test_no_new_allowlist_entries<br>x:test_repo_guards_pass |
| SC-002 | k1:test_one_byte_in_skill_body_changes_only_skills_and_digest<br>k1:test_tree_location_does_not_matter |
| SC-003 | k3:test_sc003_two_distinct_digests |
| SC-004 | k4:test_sc004_three_errors_three_calls<br>k4:test_sc004_none_never_tool_error |
| SC-005 | k5:test_us3_raise_makes_zero_provider_calls<br>k5:test_us3_drop_sends_without_seed |
| SC-006 | k6:test_us5_grep_content_one_event_per_skill_file<br>k6:test_grep_default_mode_is_content<br>k6:test_grep_k_skill_files |

## Not covered by a hidden test (and why)

- **FR-027** (kernel design I5 text lists the ceiling): documentation, checked in review.
- **FR-034** docs half (AgentSpec docstring, `docs/python-sdk.md` state the undetectable case):
  review only. The testable half (no `*honour*`/`*honor*` event in `EVENTS`, no event text
  claiming a seed was honoured) is `k5:test_no_honoured_event`.
- **FR-043, SC-001, SC-007** (repo suite and held-out 005 stay green; failing-then-passing tests in the
  kernel suite): process requirements; run `scripts/test-docker.sh` and
  `scripts/run-heldout.sh 005-multi-instance` separately. FR-042 is covered by running the two
  guard test files from the tree under test plus a no-new-allow-list-key check.
- **Partially untestable through the contract**:
  - FR-021 exec-guard refusals counted toward both ceilings: needs `tools.exec.denyPatterns`
    config, which is not a contract surface (gate denials not counting is tested).
  - FR-032 fallback-provider primary rule: `FallbackProvider` is not a contract surface.
  - FR-012 MCP tools not overridden: no offline MCP server in the contract (actions are tested).
  - FR-036 `call_id=None` outside a runner: needs a tool executed directly, not a contract surface.
  - FR-024 `ask_user` exclusion from the count is not separately observable (an `ask_user` call
    always ends the run first); precedence is tested.

## Surface assumptions (double-check against the implementation)

1. `agent.tools()`: the contract writes a method; the baseline has a **property**. Tests call
   `_h006.tools_of(agent)`, which accepts either form.
2. `FakeProvider.supported_sampling_fields = None` on an instance models a pass-through provider
   (no support information), for FR-032(b). Baseline `_sampling_support()` returns it as is.
3. "Request sent without seed" is read from `FakeProvider.calls[i].provider_context.request.sampling`.
4. `VariantError.variant` may be the `Variant` or its name; `.tool` and `.path` must be exact.
5. Policy denials for the FR-024/FR-021/FR-038 gate tests use `moeka.tools.DefaultPolicy(deny_rules=...)`
   via `AgentSpec(policy=...)` (public moeka API, not listed in the contract's import line).
6. Tool-exception / `result_invalid` kinds come from `AgentSpec.actions`: a plain function and a
   `moeka.tools.FunctionTool(..., output_model=...)`.
7. FR-003: a dangling symlink inside a skill directory is not a record and not an error (own test,
   `k1:test_dangling_symlink_is_not_a_counted_file`); a file symlink to an outside regular file is a
   record keyed by its in-tree path with the target's bytes.
8. FR-008: an empty tool name raises `TypeError` (the spec puts "non-empty str" in the type rule);
   `None`/`1`/`True` for `on_unsupported` must raise `ValueError` (FR-028 "any other value").
9. FR-032(b) fail-closed is also expected on `kernel.llm.complete(..., GenerateOptions(on_unsupported="raise"))`
   (`k5:test_passthrough_fails_closed_for_llm_complete_raise`); the spec does not restrict (b) to
   agent runs.
10. FR-011 on first run: `run_sync` raises `VariantError` (not a RunResult with an error) and makes
    no provider call.
11. K6 `skill.read.path` is compared with `str(Path(...).resolve())`; grep is driven over
    `<work_dir>/skills` (relative path `skills`), read_file maps `skills/<name>/SKILL.md` onto
    `Variant.builtin_skills_dir` when the workspace has no such file (baseline behaviour).
12. K4 error string is matched exactly: `max_tool_errors: {count} tool errors (limit {N})`, where
    count is the total after the whole iteration (may exceed N when one response has several errors).

## Baseline (2026-10-01, consolidate/new-main c5340d24, git archive copy)

185 failed, 27 passed of 212. Failing reasons are all missing features (`KeyError: 'skills'`,
unknown `Variant.tool_param_descriptions` / `AgentSpec.on_unsupported` /
`ModelSpec.unsupported_sampling` kwargs, missing `moeka.trace.args_digest` / `moeka.errors.VariantError`,
`NotImplementedError` for `max_tool_errors`, `skill.read` without `via`/`call_id`, `skill.read`
emitted for too-large/unreadable files, no grep coverage). 45 requirement ids have at least one
failing test; FR-042 is the only id fully green on the baseline.
