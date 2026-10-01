# SPEC-MAP: held-out tests for spec 005-multi-instance

Source: `specs/005-multi-instance/spec.md` (branch `spec/005-multi-instance`), Test Harness
Contract section. 235 collected tests (parametrized) from 173 test functions, in 12 files, one
per FR group. Every test carries `@pytest.mark.fr(...)`; the conftest summary groups
results by id.

| Group | File | FRs | Collected tests |
|-------|------|-----|-----------------|
| A control script | `test_a_control_script.py` | FR-001..012, SC-002 | 57 |
| B new + ports | `test_b_new_ports.py` | FR-013..020, SC-007 | 38 |
| C template unit | `test_c_units.py` | FR-021..024 | 13 |
| D fail fast | `test_d_fail_fast.py` | FR-025, 026 | 11 |
| E config-owned paths | `test_e_config_paths.py` | FR-027..032 (+FR-050 docstring) | 11 |
| F instance lock / RO attach | `test_f_instance_lock.py` | FR-033..039, SC-003, SC-006 | 41 |
| G token lock / Conflict | `test_g_channel_lock.py` | FR-041..043 | 14 |
| H shared budget | `test_h_shared_budget.py` | FR-044..050, SC-004, SC-006 | 22 |
| I usage store | `test_i_usage_store.py` | FR-051, 052, SC-005 | 4 |
| J many kernels | `test_j_many_kernels.py` | FR-053..055 | 5 |
| K containers | `test_k_containers.py` | FR-056..059, SC-001 (partial) | 10 |
| L floor | `test_l_floor.py` | FR-040, FR-060..062, SC-008 | 9 |

Support files: `conftest.py` (temp HOME, fakes, guards, FR summary), `_mi.py` (env/estimate/
event builders on public APIs), `workers.py` (child processes for lock, budget, usage and
channel-manager tests), `pytest.ini` (markers `fr`, `docker`, `slow`; asyncio auto; 120 s
default timeout).

## Requirements judged untestable or only partly testable

- **FR-036 `lock_unsupported`**: no local filesystem without `flock(2)` is available to the
  harness (tmpfs, ext4, overlay all support it; no NFS/FUSE). Only `reason == "held"` is
  tested.
- **FR-023 "without arguments keeps its current behaviour"**: not run; today's no-arg
  installer would drive systemd and `sudo loginctl` (only stopped by the PATH guards), so
  invoking it is unsafe and proves nothing.
- **FR-030 "call sites that hold a Config pass `config.runtime_data_dir`"**: no public
  surface; only the helper signatures and defaults are tested.
- **FR-031 "`nanobot gateway` pins its config path at startup"**: no surface to observe the
  pin inside a separate gateway process; pin/set/reset semantics are tested in-process.
- **FR-043 "stops issuing getUpdates" / "one restart no earlier than conflict_retry_s" /
  "episode ends at the first successful round trip"**: needs a live or faked Bot API
  application object, which the contract does not name. State, logging-once, config range,
  webhook mode and manager status are tested.
- **FR-057 "before binding any port"**: only the exit code is observed.
- **FR-056/058/059/062, FR-032, FR-021 ([S]/[D])**: static and doc checks are keyword based.
- **SC-001**: two compose projects are tested; the "two host instances plus two containers
  for 60 s" run is a measurement [M], not a test.
- **SC-009**: live checkout `git status` and `moeka.service` bytes are an operator check
  outside the throwaway copy; not testable here.
- **Edge "two containers that do not share a run dir"** and **"network filesystem"**:
  documentation only.

## Assumptions the head agent should check against the contract

1. The fake gateway is a Python script given as `MOEKA_NANOBOT_BIN`; tests read `--config`
   and `--workspace` from its argv and the `HT_*` env vars it inherited (FR-003). `run` must
   `exec` it (same PID as `moeka.sh run`, PID file equals it).
2. `moeka.sh logs` with the unit active is expected to call `journalctl` from `PATH`
   (no contract hook exists; a PATH guard records it). FR-007 journal test depends on this.
3. `status --json` prints the JSON object alone on stdout; warnings go to stderr. `unit` is
   the full name (`moeka@a.service`, `moeka.service`), `name` is `default` for `~/.nanobot`.
4. `moeka.sh list` (plain): each discovered workspace path appears on exactly one line.
5. FR-010 is tested offline only: pairing fails at `getMe` (dead proxy); tests assert the
   repo `keys.env` is untouched and the printed target is `<root>/keys.env`.
6. FR-022 linger: the fake `loginctl show-user` answers `Linger=no`; the installer must print
   a command containing `enable-linger` and must not call `sudo` or `loginctl enable-linger`.
7. The CLI lock tests hold `<root>/.instance.lock` with a raw `fcntl.flock` from a child and
   write `<root>/.instance.json` themselves; implementations must use `flock` on that exact
   file (filelock's Unix lock is compatible). Expected stderr: resolved root and `pid <n>`.
8. FR-026 positive case pre-holds the lock on the override root and only asserts "exit code
   is not 2".
9. FR-042 manager tests monkeypatch `TelegramChannel.start/stop` and
   `WebSocketChannel.start/stop` with recorders (no network); the lock must therefore be
   taken by `ChannelManager` before `channel.start()`. The holder in the cross-instance test
   is a second `ChannelManager` in a child process; the sidecar `workspace` must be that
   config's workspace. `get_status()["websocket"]["state"] == "running"` relies on the
   patched start setting `_running`.
10. FR-043 log check uses a loguru sink: exactly one ERROR record containing both `Conflict`
    and `another process is polling this bot token`; in webhook mode exactly one record of
    any level containing `Conflict`. `polling_state` before/after for non-Conflict errors
    and webhook mode must be unchanged.
11. Budget tests drive `SharedCapBudget` directly with `moeka.budget.CallEstimate` and
    `moeka.budget.ModelCallEvent` (public), and compare with `CapBudget` step by step
    (refusal `reason_code`, `scope`, every snapshot figure within 1e-9).
12. **Spec inconsistency (US3-1)**: with `$0.01` against `$0.50`, CapBudget's own float
    arithmetic admits **49**, not 50 (49 x 0.01 = 0.49000000000000027, +0.01 > 0.50). Since
    FR-045 demands CapBudget's arithmetic, the "exactly 50" test uses `2**-7` USD amounts
    (exact in binary), and the literal scenario accepts 49 or 50. The spec text should be
    corrected (or the scenario amounts changed).
13. FR-048 and FR-051 tests hold the SQLite write lock on `<data_dir>/llm_usage.sqlite3`
    with `BEGIN IMMEDIATE` from the test (no table is read). Settle/release during the lock
    may raise or not; only the eventual figures are asserted.
14. FR-047 trace event: `budget.expire` is observed through the `trace=` sink of the Kernel
    the `SharedCapBudget` is passed to (the expiring admission happens in that kernel's
    `kernel.llm` call). Fields: `call_id`, plus some key containing `usd` equal to the
    charged USD and some key containing `token` equal to the charged tokens.
15. FR-051/SC-005 require `Environment.for_host(data_dir=...)` (FR-053) to share one data dir
    across processes; those tests also carry FR-053.
16. FR-038: `kernel.memory("<scope>")` for a store that does not exist raises
    `ReadOnlyKernelError`; `epistemics.record_fact(..., source="user", ref=...)` is the
    epistemics write probed.
17. FR-060 own-root `.env` is tested for the legacy flat layout (root = workspace); for
    kernel-native floors `.env` is covered as part of the passed other roots.
18. Container tests need network for `docker build`; they use tags `moeka-heldout-005:*`,
    containers/projects `mhd*`, and clean up with `rm -f -v` / `down -v --rmi local`.

## Baseline (consolidate/new-main @ 25e66fa7, `git archive` copy, 2026-10-01)

Full run, docker available, all markers: **207 failed, 5 errors, 23 passed** of 235
(11m52s). Every one of the 62 FRs and all mapped SCs (SC-001..SC-008) have at least one
failing test (70 of 70 ids). The 23 baseline passes are non-regression checks that today's
code already satisfies (e.g. FR-026 `--workspace` override proceeds, FR-035 close/aclose
release when there is no lock, FR-039 `nanobot status` works, FR-055 distinct memory dirs,
SC-008 control paths allowed, FR-041 "no lock for empty/unexpanded token").

## Tests per requirement id

### FR-001 (3 test functions)
- `test_a_control_script.py::test_workspace_flag_beats_env_and_tilde_expands`
- `test_a_control_script.py::test_default_instance_is_home_nanobot`
- `test_a_control_script.py::test_env_file_cannot_redirect_instance`

### FR-002 (2 test functions)
- `test_a_control_script.py::test_unexpanded_workspace_exits_2_and_creates_nothing`
- `test_a_control_script.py::test_unexpanded_workspace_flag_exits_2`

### FR-003 (4 test functions)
- `test_a_control_script.py::test_named_instance_skips_repo_env_files`
- `test_a_control_script.py::test_repo_env_opt_in_and_order`
- `test_a_control_script.py::test_default_instance_loads_repo_env_files`
- `test_a_control_script.py::test_registered_instance_skips_repo_env_files`

### FR-004 (9 test functions)
- `test_a_control_script.py::test_stop_one_instance_leaves_others_running`
- `test_a_control_script.py::test_sc002_ten_repetitions`
- `test_a_control_script.py::test_stop_sigkills_after_timeout`
- `test_a_control_script.py::test_stop_does_not_signal_reused_pid`
- `test_a_control_script.py::test_stop_does_not_signal_other_instances_gateway_via_pid_file`
- `test_a_control_script.py::test_stop_with_garbage_pid_file`
- `test_a_control_script.py::test_stop_nothing_running_exits_0`
- `test_a_control_script.py::test_stop_named_unit_and_stray_pid`
- `test_a_control_script.py::test_static_no_pattern_kill`

### FR-005 (6 test functions)
- `test_a_control_script.py::test_status_never_started`
- `test_a_control_script.py::test_status_running_via_pid`
- `test_a_control_script.py::test_status_running_via_gateway_lock`
- `test_a_control_script.py::test_status_running_via_unit`
- `test_a_control_script.py::test_status_without_procps`
- `test_a_control_script.py::test_start_when_gateway_lock_held_starts_nothing`

### FR-006 (6 test functions)
- `test_a_control_script.py::test_workspace_flag_beats_env_and_tilde_expands`
- `test_a_control_script.py::test_default_instance_is_home_nanobot`
- `test_a_control_script.py::test_status_never_started`
- `test_a_control_script.py::test_status_running_via_pid`
- `test_a_control_script.py::test_status_running_via_unit`
- `test_a_control_script.py::test_status_json_ports_socket_and_units`

### FR-007 (4 test functions)
- `test_a_control_script.py::test_doctor_reports_only_own_instance`
- `test_a_control_script.py::test_logs_reads_own_log_file`
- `test_a_control_script.py::test_logs_reads_own_unit_journal_when_active`
- `test_a_control_script.py::test_static_doctor_logs_no_pgrep`

### FR-008 (2 test functions)
- `test_a_control_script.py::test_run_pid_lock_and_argv`
- `test_a_control_script.py::test_run_honours_moeka_config`

### FR-009 (4 test functions)
- `test_a_control_script.py::test_start_twice_starts_one_process`
- `test_a_control_script.py::test_start_when_unit_active_starts_nothing`
- `test_a_control_script.py::test_start_other_instance_while_one_runs`
- `test_a_control_script.py::test_start_when_gateway_lock_held_starts_nothing`

### FR-010 (1 test functions)
- `test_a_control_script.py::test_telegram_pair_targets_instance_keys`

### FR-011 (2 test functions)
- `test_a_control_script.py::test_list_json_discovers_instances`
- `test_a_control_script.py::test_list_json_empty`

### FR-012 (8 test functions)
- `test_a_control_script.py::test_stop_named_unit_and_stray_pid`
- `test_a_control_script.py::test_status_running_via_unit`
- `test_a_control_script.py::test_status_json_ports_socket_and_units`
- `test_a_control_script.py::test_disable_named_addresses_only_its_unit`
- `test_a_control_script.py::test_restart_named_unit`
- `test_a_control_script.py::test_registered_instance_has_no_unit`
- `test_a_control_script.py::test_default_instance_unit_is_moeka_service`
- `test_a_control_script.py::test_no_unrouted_systemctl`

### FR-013 (5 test functions)
- `test_b_new_ports.py::test_bad_names_exit_2`
- `test_b_new_ports.py::test_good_names_create_default_target`
- `test_b_new_ports.py::test_non_empty_target_refused`
- `test_b_new_ports.py::test_empty_existing_target_allowed`
- `test_b_new_ports.py::test_custom_workspace_is_registered`

### FR-014 (3 test functions)
- `test_b_new_ports.py::test_good_names_create_default_target`
- `test_b_new_ports.py::test_custom_workspace_is_registered`
- `test_b_new_ports.py::test_workspace_is_absolute_not_placeholder`

### FR-015 (2 test functions)
- `test_b_new_ports.py::test_default_layout_unix_socket`
- `test_b_new_ports.py::test_ws_tcp_layout`

### FR-016 (7 test functions)
- `test_b_new_ports.py::test_auto_base_is_smallest_free`
- `test_b_new_ports.py::test_auto_skips_ports_of_discovered_instances`
- `test_b_new_ports.py::test_auto_skips_registered_instance_api_and_ws_ports`
- `test_b_new_ports.py::test_auto_skips_bound_port`
- `test_b_new_ports.py::test_no_free_port_base`
- `test_b_new_ports.py::test_schema_default_ports_count_as_used`
- `test_b_new_ports.py::test_sc007_five_consecutive_news`

### FR-017 (5 test functions)
- `test_b_new_ports.py::test_schema_default_ports_count_as_used`
- `test_b_new_ports.py::test_bad_port_base_exit_2`
- `test_b_new_ports.py::test_explicit_port_base_used`
- `test_b_new_ports.py::test_explicit_port_base_conflicts_with_instance`
- `test_b_new_ports.py::test_explicit_port_base_bound_by_unrelated_process`

### FR-018 (1 test functions)
- `test_b_new_ports.py::test_concurrent_new_never_shares_ports`

### FR-019 (1 test functions)
- `test_b_new_ports.py::test_socket_path_too_long`

### FR-020 (1 test functions)
- `test_b_new_ports.py::test_keys_env_mode_and_printed_ports`

### FR-021 (2 test functions)
- `test_c_units.py::test_template_unit_contents`
- `test_c_units.py::test_template_keeps_backoff_settings`

### FR-022 (5 test functions)
- `test_c_units.py::test_install_named_renders_and_enables`
- `test_c_units.py::test_install_bad_name`
- `test_c_units.py::test_install_missing_instance`
- `test_c_units.py::test_install_no_enable`
- `test_c_units.py::test_install_dry_run_changes_nothing`

### FR-023 (1 test functions)
- `test_c_units.py::test_install_named_never_touches_legacy_units`

### FR-024 (3 test functions)
- `test_a_control_script.py::test_disable_named_addresses_only_its_unit`
- `test_c_units.py::test_moeka_enable_named_runs_installer`
- `test_c_units.py::test_moeka_disable_named`

### FR-025 (3 test functions)
- `test_d_fail_fast.py::test_workspace_path_raises`
- `test_d_fail_fast.py::test_no_fallback_to_state_home`
- `test_d_fail_fast.py::test_other_missing_vars_still_warn_only`

### FR-026 (2 test functions)
- `test_d_fail_fast.py::test_cli_exits_2_on_unexpanded_workspace`
- `test_d_fail_fast.py::test_workspace_override_makes_config_usable`

### FR-027 (2 test functions)
- `test_e_config_paths.py::test_legacy_env_paths_follow_config_file`
- `test_e_config_paths.py::test_legacy_env_paths_bound_before_global_changes`

### FR-028 (1 test functions)
- `test_e_config_paths.py::test_gateway_default_identity_from_state_home`

### FR-029 (3 test functions)
- `test_e_config_paths.py::test_launchd_logs_under_config_dir`
- `test_e_config_paths.py::test_launchd_logs_under_workspace`
- `test_e_config_paths.py::test_launchd_logs_under_state_home`

### FR-030 (2 test functions)
- `test_e_config_paths.py::test_path_helpers_accept_data_dir`
- `test_e_config_paths.py::test_path_helpers_default_to_config_dir`

### FR-031 (1 test functions)
- `test_e_config_paths.py::test_pin_config_path`

### FR-032 (1 test functions)
- `test_e_config_paths.py::test_doc_multiple_instances_rules`

### FR-033 (4 test functions)
- `test_f_instance_lock.py::test_writer_holds_flock_and_writes_holder`
- `test_f_instance_lock.py::test_child_writer_blocks_parent`
- `test_f_instance_lock.py::test_cli_writer_refused_with_exit_3`
- `test_f_instance_lock.py::test_gateway_lock_blocks_kernel_on_same_root`

### FR-034 (5 test functions)
- `test_f_instance_lock.py::test_second_writer_in_process_refused`
- `test_f_instance_lock.py::test_second_writer_via_symlink_refused`
- `test_f_instance_lock.py::test_sc003_twenty_refusals_write_nothing`
- `test_f_instance_lock.py::test_child_writer_blocks_parent`
- `test_f_instance_lock.py::test_cli_writer_refused_with_exit_3`

### FR-035 (3 test functions)
- `test_f_instance_lock.py::test_close_releases_lock`
- `test_f_instance_lock.py::test_aclose_releases_lock`
- `test_f_instance_lock.py::test_sigkill_holder_releases_within_2s`

### FR-036 (1 test functions)
- `test_f_instance_lock.py::test_refusal_reason_is_held`

### FR-037 (5 test functions)
- `test_f_instance_lock.py::test_read_only_takes_no_lock_and_writes_nothing`
- `test_f_instance_lock.py::test_writer_may_start_while_read_only_attached`
- `test_f_instance_lock.py::test_read_only_while_writer_runs`
- `test_f_instance_lock.py::test_read_only_missing_state_dir`
- `test_f_instance_lock.py::test_bad_attach_value`

### FR-038 (3 test functions)
- `test_f_instance_lock.py::test_read_only_reads_work`
- `test_f_instance_lock.py::test_read_only_llm_calls_work`
- `test_f_instance_lock.py::test_read_only_mutations_refused_before_writing`

### FR-039 (2 test functions)
- `test_f_instance_lock.py::test_status_command_works_while_locked`
- `test_f_instance_lock.py::test_moeka_sh_read_commands_work_while_locked`

### FR-040 (1 test functions)
- `test_l_floor.py::test_legacy_floor_write_denies_lock_pid_run_files`

### FR-041 (8 test functions)
- `test_g_channel_lock.py::test_token_lock_files_and_secrecy`
- `test_g_channel_lock.py::test_token_lock_cross_process`
- `test_g_channel_lock.py::test_token_lock_context_manager_and_in_process`
- `test_g_channel_lock.py::test_token_lock_released_on_sigkill`
- `test_g_channel_lock.py::test_run_dir_resolution`
- `test_g_channel_lock.py::test_second_manager_reports_locked_without_starting`
- `test_g_channel_lock.py::test_manager_takes_and_releases_lock`
- `test_g_channel_lock.py::test_unexpanded_or_empty_token_takes_no_lock`

### FR-042 (4 test functions)
- `test_g_channel_lock.py::test_token_lock_files_and_secrecy`
- `test_g_channel_lock.py::test_token_lock_cross_process`
- `test_g_channel_lock.py::test_token_lock_context_manager_and_in_process`
- `test_g_channel_lock.py::test_second_manager_reports_locked_without_starting`

### FR-043 (5 test functions)
- `test_g_channel_lock.py::test_conflict_sets_state_and_logs_once`
- `test_g_channel_lock.py::test_other_errors_keep_handling`
- `test_g_channel_lock.py::test_conflict_in_webhook_mode_logs_once_no_state_change`
- `test_g_channel_lock.py::test_conflict_retry_config`
- `test_g_channel_lock.py::test_manager_reports_conflict_state`

### FR-044 (5 test functions)
- `test_h_shared_budget.py::test_surface_and_storage`
- `test_h_shared_budget.py::test_state_shared_by_data_dir_and_id`
- `test_h_shared_budget.py::test_state_shared_across_processes`
- `test_h_shared_budget.py::test_us3_exactly_cap_admitted_exact_amounts`
- `test_h_shared_budget.py::test_us3_literal_scenario`

### FR-045 (7 test functions)
- `test_h_shared_budget.py::test_parity_with_capbudget`
- `test_h_shared_budget.py::test_refusal_is_budget_exceeded_cap_usd`
- `test_h_shared_budget.py::test_unpriced_allowed_and_charged`
- `test_h_shared_budget.py::test_us3_exactly_cap_admitted_exact_amounts`
- `test_h_shared_budget.py::test_us3_literal_scenario`
- `test_h_shared_budget.py::test_sc004_cap_never_overshoots_under_contention`
- `test_h_shared_budget.py::test_kernels_in_processes_share_one_cap`

### FR-046 (5 test functions)
- `test_h_shared_budget.py::test_parity_with_capbudget`
- `test_h_shared_budget.py::test_unpriced_allowed_and_charged`
- `test_h_shared_budget.py::test_late_settle_and_release_true_up`
- `test_h_shared_budget.py::test_late_unpriced_settle_keeps_charge`
- `test_h_shared_budget.py::test_release_twice_noop_and_timeout_charged`

### FR-047 (4 test functions)
- `test_h_shared_budget.py::test_sigkilled_reservation_is_charged_after_lease`
- `test_h_shared_budget.py::test_budget_expire_trace_event`
- `test_h_shared_budget.py::test_late_settle_and_release_true_up`
- `test_h_shared_budget.py::test_late_unpriced_settle_keeps_charge`

### FR-048 (4 test functions)
- `test_h_shared_budget.py::test_admission_fails_closed_when_db_locked`
- `test_h_shared_budget.py::test_no_provider_call_when_budget_unavailable`
- `test_h_shared_budget.py::test_unwritten_settle_is_retried_and_stays_counted`
- `test_h_shared_budget.py::test_unwritten_settle_flushed_at_kernel_close`

### FR-049 (2 test functions)
- `test_h_shared_budget.py::test_cap_mismatch_and_reset`
- `test_h_shared_budget.py::test_cap_mismatch_cross_process`

### FR-050 (4 test functions)
- `test_e_config_paths.py::test_capbudget_docstring_says_in_memory_per_process`
- `test_h_shared_budget.py::test_no_provider_call_when_budget_unavailable`
- `test_h_shared_budget.py::test_kernel_with_shared_budget`
- `test_h_shared_budget.py::test_kernels_in_processes_share_one_cap`

### FR-051 (3 test functions)
- `test_i_usage_store.py::test_sc005_four_processes_fifty_calls`
- `test_i_usage_store.py::test_eight_processes_no_lost_rows`
- `test_i_usage_store.py::test_writes_blocked_by_lock_are_flushed_at_close`

### FR-052 (3 test functions)
- `test_i_usage_store.py::test_sc005_four_processes_fifty_calls`
- `test_i_usage_store.py::test_eight_processes_no_lost_rows`
- `test_i_usage_store.py::test_two_kernels_one_process_shared_store`

### FR-053 (6 test functions)
- `test_i_usage_store.py::test_sc005_four_processes_fifty_calls`
- `test_i_usage_store.py::test_eight_processes_no_lost_rows`
- `test_i_usage_store.py::test_writes_blocked_by_lock_are_flushed_at_close`
- `test_i_usage_store.py::test_two_kernels_one_process_shared_store`
- `test_j_many_kernels.py::test_for_host_data_dir`
- `test_j_many_kernels.py::test_data_dir_inside_work_dir_refused_under_strict`

### FR-054 (2 test functions)
- `test_i_usage_store.py::test_two_kernels_one_process_shared_store`
- `test_j_many_kernels.py::test_two_writer_kernels_one_process`

### FR-055 (2 test functions)
- `test_j_many_kernels.py::test_distinct_memory_keys`
- `test_j_many_kernels.py::test_shared_memory_key_emits_trace_event`

### FR-056 (3 test functions)
- `test_k_containers.py::test_dockerfile_static`
- `test_k_containers.py::test_image_runs_as_1000`
- `test_k_containers.py::test_first_start_seeds_config_and_never_overwrites`

### FR-057 (3 test functions)
- `test_k_containers.py::test_missing_secret_exits_2`
- `test_k_containers.py::test_unwritable_data_exits_nonzero`
- `test_k_containers.py::test_first_start_seeds_config_and_never_overwrites`

### FR-058 (3 test functions)
- `test_k_containers.py::test_compose_static`
- `test_k_containers.py::test_compose_requires_secret`
- `test_k_containers.py::test_two_compose_projects_side_by_side`

### FR-059 (1 test functions)
- `test_k_containers.py::test_deployment_doc`

### FR-060 (2 test functions)
- `test_l_floor.py::test_legacy_floor_denies_keys_env_and_root_env`
- `test_l_floor.py::test_native_floor_with_passed_roots`

### FR-061 (5 test functions)
- `test_l_floor.py::test_legacy_floor_denies_other_instances`
- `test_l_floor.py::test_native_floor_with_passed_roots`
- `test_l_floor.py::test_native_floor_never_discovers`
- `test_l_floor.py::test_root_containing_own_work_dir_is_not_other`
- `test_l_floor.py::test_floor_constructor_accepts_other_roots`

### FR-062 (1 test functions)
- `test_l_floor.py::test_floor_limits_documented`

### SC-001 (1 test functions)
- `test_k_containers.py::test_two_compose_projects_side_by_side`

### SC-002 (2 test functions)
- `test_a_control_script.py::test_stop_one_instance_leaves_others_running`
- `test_a_control_script.py::test_sc002_ten_repetitions`

### SC-003 (1 test functions)
- `test_f_instance_lock.py::test_sc003_twenty_refusals_write_nothing`

### SC-004 (1 test functions)
- `test_h_shared_budget.py::test_sc004_cap_never_overshoots_under_contention`

### SC-005 (1 test functions)
- `test_i_usage_store.py::test_sc005_four_processes_fifty_calls`

### SC-006 (2 test functions)
- `test_f_instance_lock.py::test_sigkill_holder_releases_within_2s`
- `test_h_shared_budget.py::test_sigkilled_reservation_is_charged_after_lease`

### SC-007 (1 test functions)
- `test_b_new_ports.py::test_sc007_five_consecutive_news`

### SC-008 (4 test functions)
- `test_l_floor.py::test_legacy_floor_denies_other_instances`
- `test_l_floor.py::test_legacy_floor_denies_keys_env_and_root_env`
- `test_l_floor.py::test_legacy_floor_allows_controls`
- `test_l_floor.py::test_native_floor_with_passed_roots`
