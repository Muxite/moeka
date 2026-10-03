# SPEC-MAP: 012-capability-provenance held-out tests

Written from `specs/012-capability-provenance/spec.md` (branch `feat/012-capability-provenance`, spec
commit `758fc05b`) only, through the spec's Test Harness Contract surfaces plus the public kernel
API (`moeka.Environment.from_config`, `Kernel`, `AgentSpec`, `FakeProvider`, `MemoryTraceSink`)
and the existing tool/skill surfaces (`SkillsLoader`, `ToolLoader`/`ToolRegistry`/`ToolContext`).
`nanobot.capabilities.provenance` / `script_tool` are imported lazily inside each test
(`_h012.cap`), so a tree without the feature collects cleanly and fails per requirement id.

Keys are FAKE: `_h012.Signer(label)` derives a deterministic Ed25519 key from
`sha256("h012-fake-" + label)` with `cryptography` (tests only). No network, no real keys.

Files: `test_a_cert.py` (FR-001 schema, bundle hash / BundleError), `test_a_verify.py` (FR-002, SC-001),
`test_a_trust.py` (FR-003, FR-004/SC-004), `test_b_config.py` (FR-005, certified_boost),
`test_b_gate.py` (FR-006 matrix, script tool registration), `test_b_index.py` (FR-007),
`test_b_protected.py` (FR-008), `test_c_manifest.py` (FR-009), `test_c_exec.py` (FR-010, FR-011,
SC-003), `test_d_cli.py` (FR-012, list status column), `test_e_sc002.py` (SC-002),
`test_x_nonfunctional.py` (NFR-001..003). Helpers: `_h012.py` (independent reference of canonical
JSON, bundle hash, cert id, key id and io normalisation; fake signer; bundle / trust-dir / kernel /
loader / registry builders; bwrap probe; CLI runner), `conftest.py` (isolation, loguru / warning
capture, per-id summary).

| file | tests | baseline (spec commit 758fc05b) |
|---|---|---|
| test_a_cert.py | 48 | 45 fail, 3 pass |
| test_a_verify.py | 125 | 125 fail |
| test_a_trust.py | 34 | 30 fail, 4 pass |
| test_b_config.py | 17 | 16 fail, 1 pass |
| test_b_gate.py | 33 | 14 fail, 19 pass |
| test_b_index.py | 9 | 9 fail |
| test_b_protected.py | 21 | 16 fail, 5 pass |
| test_c_manifest.py | 46 | 46 fail |
| test_c_exec.py | 37 | 36 fail, 1 skip (SC-003, bwrap unusable on jifan) |
| test_d_cli.py | 17 | 16 fail, 1 pass |
| test_e_sc002.py | 13 | 13 fail |
| test_x_nonfunctional.py | 7 | 4 fail, 3 pass |
| **total** | **407** | **370 fail, 36 pass, 1 skip, 0 error** |

Tests that pass on the baseline pin behaviour the spec keeps: today's loading with enforcement off
/ by default (nothing refused, builtin / inline / plugin skills and builtin tools listed), script
tools absent when `tools.script.enabled` is false, ordinary file writes still allowed (the FR-008
controls), no private-key handling and no `sign` command today (FR-004), `cryptography` in
`uv.lock`, the import-boundary guards, and the reference self-checks of `_h012`.

Tester self-check: every test (406 pass + the SC-003 skip on this host) also passed against a
private throwaway reference implementation of the spec written by the tester in a scratch copy of
the spec commit (deleted; never shared). Mutation checks on that reference, each caught by at least
one test: revoked checked before io (FR-002 order), per-directory instead of string-sorted relpaths
(hash), leaking the parent environment, killing only the child instead of the process group, full
stderr instead of the last 2000 chars, stdin not canonical, `MOEKA_WORKSPACE` always set (FR-010),
logging every refusal instead of once per (name, reason) (FR-006), no certified boost (FR-007),
treating "bwrap on PATH" as usable (FR-010/FR-011 on a host where bwrap cannot create namespaces),
dropping the `CERT.json` / `revoked.jsonl` name rule (FR-008). Run times: ~35 s baseline, ~46 s
against the reference (one kernel per kernel-level test, ~0.9 s each).

## Requirement -> tests

Parametrised tests count once per parameter in the run summary. Prefixes: a1 cert/hash, a2 verify,
a3 trust, b1 config, b2 gate, b3 index, b4 protected, c1 manifest, c2 exec, d cli, e SC-002,
x non-functional.

| id | tests |
|---|---|
| FR-001 | a1:test_schema_file_is_json_schema<br>a1:test_valid_skill_cert_has_no_problems<br>a1:test_valid_script_tool_cert_has_no_problems<br>a1:test_valid_variants_have_no_problems (param)<br>a1:test_every_field_is_required (param)<br>a1:test_invalid_variants_have_problems (param) |
| FR-002 | a1:test_reference_canonical_form_is_the_spec_one<br>a1:test_bundle_sha256_matches_reference<br>a1:test_reference_sort_is_plain_string_order<br>a1:test_bundle_sha256_is_lowercase_hex64<br>a1:test_bundle_sha256_single_file_by_hand<br>a1:test_cert_json_is_not_part_of_the_bundle<br>a1:test_any_content_change_changes_hash<br>a1:test_file_rename_inside_bundle_changes_hash<br>a1:test_added_file_changes_hash<br>a1:test_hash_is_relative_to_bundle_root<br>a1:test_empty_directory_contributes_nothing<br>a1:test_script_tool_bundle_hash_matches_reference<br>a1:test_special_entries_raise_bundle_error (param)<br>a1:test_bundle_error_is_an_exception_class<br>a1:test_reference_ids_shape<br>a2:test_valid_cert_verifies_ok_with_cert_id (param)<br>a2:test_verdict_type_and_fields<br>a2:test_verify_accepts_str_path<br>a2:test_io_is_compared_after_normalisation (param)<br>a2:test_script_tool_io_comes_from_tool_yaml<br>a2:test_parent_lineage_and_scores_do_not_matter<br>a2:test_cert_json_rewrite_with_same_content_still_ok<br>a2:test_tampered_scores_break_signature<br>a2:test_garbage_signature_is_bad_signature<br>a2:test_second_trusted_key_verifies<br>a2:test_each_code_from_its_own_fixture (param)<br>a2:test_bad_bundle_variants (param)<br>a2:test_bad_schema_variants (param)<br>a2:test_bad_json_variants (param)<br>a2:test_hash_mismatch_variants (param)<br>a2:test_renamed_dir_is_name_mismatch<br>a2:test_first_failure_in_listed_order_wins (param)<br>a2:test_everything_wrong_but_a_cert_is_bad_schema_or_earlier<br>a2:test_everything_after_signature_reports_name_mismatch |
| FR-003 | a3:test_load_reads_pub_files<br>a3:test_load_returns_a_trust_store<br>a3:test_empty_dir_trusts_nothing<br>a3:test_load_of_no_dirs_trusts_nothing<br>a3:test_key_in_any_listed_dir_is_trusted<br>a3:test_only_pub_suffix_is_read<br>a3:test_pub_without_trailing_newline<br>a3:test_duplicate_keys_are_fine<br>a3:test_malformed_pub_is_skipped_with_warning (param)<br>a3:test_malformed_only_dir_trusts_nothing<br>a3:test_revoked_jsonl_revokes<br>a3:test_other_revocations_do_not_affect<br>a3:test_revocation_in_another_dir_applies<br>a3:test_bad_revoked_line_is_skipped_with_warning (param)<br>a3:test_bad_line_alone_revokes_nothing<br>a3:test_from_keys_trusts_given_keys<br>a3:test_from_keys_with_revoked<br>a3:test_from_keys_other_key_is_unknown<br>a3:test_from_keys_empty<br>a3:test_from_keys_duplicates_fine<br>a3:test_package_trust_dir_ships_empty |
| FR-004 | a3:test_capabilities_package_has_no_private_key_handling<br>a3:test_moeka_source_has_no_private_key_handling<br>a3:test_capabilities_package_never_imports_private_key_classes<br>a3:test_public_api_has_no_sign_function (param)<br>a3:test_trust_store_has_no_private_key_constructor<br>a3:test_cli_has_no_sign_command |
| FR-005 | b1:test_capabilities_defaults<br>b1:test_tools_script_default_disabled<br>b1:test_tools_script_enable<br>b1:test_snake_case_keys<br>b1:test_camel_case_keys<br>b1:test_existing_capabilities_settings_untouched<br>b1:test_trust_dir_tilde_is_expanded<br>b1:test_certified_boost_default<br>b1:test_certified_boost_in_range (param)<br>b1:test_certified_boost_out_of_range_rejected (param)<br>b1:test_certified_boost_camel_case<br>b2:test_script_tools_disabled_registers_none (param)<br>b2:test_script_tools_off_by_default |
| FR-006 | b1:test_trust_dir_tilde_is_expanded<br>b2:test_enforced_prompt_lists_only_certified_workspace_skills<br>b2:test_unenforced_prompt_lists_every_workspace_skill<br>b2:test_default_config_refuses_nothing<br>b2:test_builtin_skill_with_garbage_cert_unaffected<br>b2:test_dollar_invocation (param)<br>b2:test_enforced_registers_only_certified_script_tools<br>b2:test_unenforced_registers_every_valid_script_tool<br>b2:test_enforced_builtin_tools_match_unenforced<br>b2:test_inline_action_tool_unaffected<br>b2:test_loader_lists (param)<br>b2:test_loader_load_skill (param)<br>b2:test_loader_explicit_invocation (param)<br>b2:test_loader_summary_hides_refused<br>b2:test_inline_skill_unaffected<br>b2:test_plugin_skill_unaffected<br>b2:test_refused_skill_logged_once_per_name_and_reason<br>b2:test_refused_script_tool_logged_once<br>b2:test_unenforced_does_not_log_refusals_as_errors<br>d:test_list_status_column (param)<br>d:test_list_json_status (param)<br>e:test_baseline_both_present<br>e:test_skill_removed_after_mutation (param)<br>e:test_script_tool_removed_after_mutation (param) |
| FR-007 | b1:test_certified_boost_default<br>b1:test_certified_boost_in_range (param)<br>b1:test_certified_boost_out_of_range_rejected (param)<br>b1:test_certified_boost_camel_case<br>b3:test_entries_round_trip_cert_fields<br>b3:test_cert_fields_persist_across_instances<br>b3:test_certified_wins_a_tie_under_default_ranker<br>b3:test_without_certified_ties_break_by_name<br>b3:test_only_certified_is_boosted (param)<br>b3:test_boost_is_multiplicative_not_dominant<br>b3:test_kernel_index_records_cert_status |
| FR-008 | b4:test_write_file_denied (param)<br>b4:test_relative_path_into_trust_dir_denied<br>b4:test_overwrite_existing_denied (param)<br>b4:test_edit_existing_denied (param)<br>b4:test_symlink_into_trust_dir_denied<br>b4:test_ordinary_writes_still_allowed (param)<br>b4:test_reading_cert_json_is_allowed<br>b4:test_unconfigured_dir_is_not_a_trust_dir |
| FR-009 | c1:test_valid_manifest_fields<br>c1:test_output_schema_is_optional<br>c1:test_entry_in_subdir_and_dotdot_inside_bundle<br>c1:test_timeout_bounds_inclusive (param)<br>c1:test_description_at_limit<br>c1:test_invalid_field_is_named (param)<br>c1:test_name_must_follow_skill_naming_rule (param)<br>c1:test_entry_symlink_rejected<br>c1:test_entry_symlink_to_outside_rejected<br>c1:test_entry_not_executable_rejected (param)<br>c1:test_entry_directory_rejected<br>c1:test_unparseable_manifest_raises (param)<br>c1:test_missing_tool_yaml_raises<br>c1:test_invalid_manifest_is_not_registered |
| FR-010 | b2:test_enforced_registers_only_certified_script_tools<br>b2:test_unenforced_registers_every_valid_script_tool<br>b2:test_script_tools_disabled_registers_none (param)<br>b2:test_script_tools_off_by_default<br>c2:test_success_returns_stdout_json<br>c2:test_any_single_json_value_is_accepted (param)<br>c2:test_stdin_is_canonical_json_of_arguments<br>c2:test_cwd_is_the_bundle_dir<br>c2:test_environment_is_exactly_path_home_lang<br>c2:test_home_is_a_fresh_empty_dir_per_call<br>c2:test_moeka_workspace_only_with_workspace_effects (param)<br>c2:test_invalid_arguments_rejected_before_running (param)<br>c2:test_valid_arguments_run<br>c2:test_timeout_kills_the_whole_process_group<br>c2:test_nonzero_exit_reports_code_and_stderr_tail<br>c2:test_non_json_stdout_is_an_error_with_stderr (param)<br>c2:test_stdout_over_one_mib_is_an_error<br>c2:test_stdout_under_limit_is_fine<br>c2:test_output_schema_violation_is_an_error<br>c2:test_output_schema_match_passes<br>c2:test_networkless_tool_runs_whatever_the_sandbox<br>c2:test_trace_sandboxed_flag_follows_bwrap<br>c2:test_networkless_tool_cannot_connect_under_bwrap<br>c2:test_network_tool_can_connect<br>c1:test_invalid_manifest_is_not_registered |
| FR-011 | c2:test_trace_event_fields<br>c2:test_trace_event_has_no_arguments_or_output<br>c2:test_trace_event_on_failure_records_exit_code<br>c2:test_trace_sandboxed_flag_follows_bwrap<br>c2:test_one_event_per_call |
| FR-012 | d:test_verify_ok<br>d:test_verify_ok_script_tool<br>d:test_verify_repeated_trust_dir<br>d:test_verify_without_trust_dir_is_unknown_key<br>d:test_verify_refused (param)<br>d:test_hash_prints_bundle_hash<br>d:test_hash_script_tool<br>d:test_hash_of_bundle_with_symlink_fails<br>d:test_list_status_column (param)<br>d:test_list_json_status (param) |
| NFR-001 | x:test_cryptography_is_an_explicit_dependency<br>x:test_cryptography_is_locked |
| NFR-002 | x:test_verify_100_file_bundle_under_50ms |
| NFR-003 | x:test_new_modules_exist<br>x:test_capabilities_package_imports_no_host_package<br>x:test_importing_new_modules_leaks_no_host_module<br>x:test_repo_import_boundary_guard_passes |
| SC-001 | a2:test_each_code_from_its_own_fixture (param)<br>a2:test_fixture_to_code_is_a_bijection (param)<br>a2:test_bad_bundle_variants (param)<br>a2:test_bad_schema_variants (param)<br>a2:test_bad_json_variants (param)<br>a2:test_hash_mismatch_variants (param)<br>a2:test_renamed_dir_is_name_mismatch |
| SC-002 | e:test_baseline_both_present<br>e:test_skill_removed_after_mutation (param)<br>e:test_script_tool_removed_after_mutation (param) |
| SC-003 | c2:test_networkless_tool_cannot_connect_under_bwrap<br>c2:test_network_tool_can_connect |
| SC-004 | a3:test_capabilities_package_has_no_private_key_handling<br>a3:test_moeka_source_has_no_private_key_handling<br>a3:test_capabilities_package_never_imports_private_key_classes<br>a3:test_public_api_has_no_sign_function (param)<br>a3:test_trust_store_has_no_private_key_constructor<br>a3:test_cli_has_no_sign_command |

## Not covered by a hidden test (and why)

- **Device files in a bundle** (BundleError): cannot be created unprivileged; symlink (file, dir,
  dangling, nested), fifo and unix socket are covered.
- **Plugin tools / MCP tools under enforcement**: only plugin *skills* (FR-006) and inline action
  tools are checked; kernel plugin tools need a registry and an installed entry point.
- **SC-003 on jifan**: bwrap is installed but cannot create namespaces
  (`apparmor_restrict_unprivileged_userns=1`: "loopback: Failed RTM_NEWADDR"), so the network-denial
  test skips with that reason recorded. It runs where `bwrap --unshare-net` works (and skips with
  "bwrap is absent" where it is not installed). Its control (`test_network_tool_can_connect`) runs
  everywhere.
- **Script tool listing when `tools.script.enabled` is false**: the CLI status of script-tool
  bundles is only asserted with script tools enabled.
- **`timeout_s` missing**: whether it is required or defaults to 30 is unspecified; not asserted.
- **Nested `sub/CERT.json`**: only the top-level `CERT.json` is asserted to be excluded from the hash.

## Interpretations pinned (double-check against the implementation)

1. **Bundle hash**: recursive walk; empty directories contribute nothing; relpaths sorted with
   plain Python string order (`"a-b.txt" < "a/b.txt" < "a0.txt"`); unicode file names hashed via
   `ensure_ascii=False`; renaming the bundle dir does not change the hash.
2. **Verdict**: only `ok`, `reason` (on failure) and `cert_id` (on success, = first 16 hex of
   sha256 of the canonical cert) are asserted; `reason` on success is not.
3. **Check order**: all 63 pairs of codes are asserted (first in the listed order wins) except
   `bad-bundle` vs `hash-mismatch` / `io-mismatch` / `revoked`: an unhashable bundle fails at
   the hash step, so those three orders are ambiguous. A bundle with a symlink and an otherwise
   valid cert must give `bad-bundle` (not `hash-mismatch`).
4. **Fixtures per code**: `bad-schema` = a signed cert with a missing / extra / wrong-valued
   top-level field (also `io: "untyped"`); `bad-json` = unparseable CERT.json (incl. empty file);
   `unknown-key` = signed by an untrusted key; `bad-signature` = trusted `key_id` but signed by
   another key, a tampered `scores` field, or 64 zero bytes; `kind-mismatch` = `script-tool` cert
   in `skills/` (and vice versa); `io-mismatch` = cert io differing from the bundle's normalised io
   while the hash still matches. The io comparison is after spec-011 normalisation (defaults
   `outputs: [any]`, `effects: [exec]`, sorted lists, shorthand inputs).
5. **Trust store**: only `*.pub` is read; a missing trailing newline is fine; a 31/33-byte key,
   bad base64, an empty file or a PEM block is malformed. "With a warning" = at least one warning
   via loguru (WARNING+), the `warnings` module or stdlib logging. Behaviour is observed only
   through `verify_bundle`. `TrustStore.load([])` trusts nothing; the package's
   `nanobot/capabilities/trust/` must exist and hold no `*.pub`.
6. **FR-004 grep**: `Ed25519PrivateKey`, `from_private_bytes`, `load_pem_private_key` must not appear
   anywhere under `nanobot/` or `moeka/`; `generate(` must not appear under `nanobot/capabilities`;
   no public non-module name matching `^sign($|_|[A-Z])` in `provenance` / `script_tool`; no
   `sign` CLI command.
7. **Config reaches the loaders**: kernel tests build `Environment.from_config(Config(capabilities=...,
   tools={"script": {"enabled": ...}}))`. Loader-level tests pass that host env's core env to
   `SkillsLoader(env=...)` / `ToolContext(env=..., config=Config.tools)`, i.e. the implementation
   reads `env.config.section("capabilities")`; when `SkillsLoader.__init__` also takes one of
   `capabilities`, `capabilities_config`, `enforce_certs`, `trust_dirs`, `trust`, `trust_store`
   it is passed too. Script tool execution tests use the registry `ToolLoader().load` fills; when
   the loader does not register script tools itself they fall back to a kernel agent's own registry
   (`agent._loop.tools`).
8. **Enforcement off**: nothing is refused, including bundles with a bad / revoked cert (they load);
   statuses still show. **Status values**: no cert = `uncertified` when off and `refused:no-cert`
   when on; any other failure = `refused:<reason>` in both modes; built-in skills, package skills
   and built-in tools = `builtin`. The trust dirs used are the package dir plus
   `capabilities.trust_dirs` (`~` expanded against $HOME, checked behaviourally).
9. **Logged once**: exactly one loguru record at INFO or above mentions the refused name per
   (name, reason) across repeated listings, `load_skill` calls and a second loader / kernel in the
   process, and that record contains the reason code; a new reason for the same name logs once more.
   Tests use uuid-suffixed names so the process-wide memory does not leak between tests.
10. **FR-007**: `CapabilityEntry(..., cert_id=, cert_status=)` keyword construction; both round-trip
   through `CapabilityIndex.entries()` and persist. The boost applies under the default ranker
   (`select(..., ranker=None)`) with the default 0.10 and only for `certified`: a certified entry wins
   an exact tie against an earlier name, does not overtake a 3.5x higher score, and a certified 0
   stays 0. Kernel: `<data_dir>/capabilities.db` after a `selection: filtered` run records
   `certified` + cert id for a certified workspace skill, `uncertified` + null for a plain one,
   `builtin` + null for everything else.
11. **FR-008**: denial through the kernel agent's real `write_file` / `edit_file` (FakeProvider tool
   calls); asserted as "the file is not created / unchanged" (+ an error tool message). Covered: trust
   dir inside and outside the workspace, its subdirs, a relative path, a symlink into a trust dir,
   `CERT.json` / `revoked.jsonl` anywhere (also outside the workspace). Reading those files stays
   allowed; writing elsewhere (incl. outside the workspace) stays allowed.
12. **FR-009**: `ManifestError.field` equals the field (or starts with `field.`, e.g. `io.effects`);
   without `.field` the message must contain it. `entry` rejected when absolute, escaping via
   `..`, missing, a directory, a symlink (inside or outside), or mode 0644/0600/0444. Name follows
   the skill rule (hyphen-case, no `--`, <= 64, == dir). `parameters` must be a mapping (a list or
   a string is invalid). Missing / unparseable / non-mapping `tool.yaml` raises `ManifestError`
   (field not asserted). `ScriptManifest` fields: `name, description, parameters, output_schema,
   io` (IOSpec or dict), `entry` (path relative to or inside the bundle), `timeout_s`.
13. **FR-010**: results come from `ToolRegistry.execute`; error = `ToolResult.is_error` (or text
   starting "Error"); success payload = `ToolResult.structured` when present, else `json.loads` of
   the text. Env must be exactly `PATH=/usr/bin:/bin`, `HOME`, `LANG=C.UTF-8` (+
   `MOEKA_WORKSPACE` = the workspace, only with read/write-workspace), ignoring `PWD`/`SHLVL`/`_`
   that a shell wrapper may add; HOME is empty at the start of every call. Execution tests declare
   `network` so they never depend on the host's bwrap. Timeout: error text contains "timeout",
   returns within 20 s for `timeout_s: 1`, and the script's own process and its grandchild
   `sleep` are gone (or zombies) within 5 s. Non-zero exit: text contains the exit code and the
   last 2000 stderr characters but not the first ones; non-JSON stdout (text, two values, empty,
   trailing garbage) is an error that includes the stderr tail; > 1 MiB is an error, 512 KiB is not;
   any single JSON value (number, string, list, null, bool) is a valid result; `output_schema`
   failures carry the existing `result failed schema` marker.
14. **bwrap "available" = usable**: a bwrap binary that cannot create a network namespace (jifan
   today) counts as unavailable, so a network-less tool must still run (`sandboxed: false`);
   `test_networkless_tool_runs_whatever_the_sandbox` and `test_trace_sandboxed_flag_follows_bwrap`
   pin this. An implementation that only checks `which bwrap` fails those two on jifan.
15. **FR-011**: observed through a kernel run (FakeProvider calls `script_<name>`); exactly one
   `script_tool.call` event per call whose keys, minus the sink stamps (`event, trace_id, span,
   tags, ts`), are exactly the five spec keys; `tool` is `script_<name>` or `<name>`;
   `effects_declared` compared as a sorted list; arguments and output never appear in the event.
16. **FR-012**: the last non-empty stdout line is `ok <cert_id>` / `refused <reason>` / the hash;
   exit 0 / 1. Without `--trust-dir` (and no config) the fixture is `unknown-key` (the package
   trust dir ships empty). `hash` of a bundle with a symlink exits non-zero. `list` (text): the
   line holding the name (or `script_<name>`) as a whitespace token also holds the status token;
   `list --json`: the record's `cert_status` (or `status`) key. Config is passed with
   `--config` (camelCase keys) and `--workspace`.
17. **SC-002**: "next load" = a fresh kernel over the same workspace; mutations: one byte of
   `SKILL.md` body, of a nested file, of `tool.yaml`, of the entry script; an added file; the dir
   renamed; io edited; the cert id appended to the trust dir's `revoked.jsonl`. The untouched other
   capability must stay.

## Baseline (2026-10-03, spec commit 758fc05b, git archive copy)

370 failed, 36 passed, 1 skipped of 407. Failing reasons are all missing features
(`ModuleNotFoundError: nanobot.capabilities.provenance` / `script_tool`, missing
`schemas/capability-cert.v1.schema.json`, `Config` without `enforce_certs` / `certified_boost` /
`tools.script`, `CapabilityEntry` without `cert_id`, no `verify`/`hash` CLI commands, no status
column, script tools never registered, trust paths writable). 19 requirement ids have at least one
failing test (every id).
