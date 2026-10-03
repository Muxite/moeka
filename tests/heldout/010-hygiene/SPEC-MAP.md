# SPEC-MAP: 010-hygiene held-out tests

Written from `specs/010-hygiene/spec.md` (branch `feat/010-hygiene`, spec commit `1163df0e`). Source
was read only from `git archive 1163df0e`: the Dream code and `tests/agent/test_dream*.py` (how Dream
runs with a fake provider), `image_generation.py`, `transcription.py`, `core/vec_store.py`, the
`llm_usage` store, `gateway_runtime.py` and the `serve` / `agent` CLI entry points, `SkillsLoader`,
and `nanobot/capabilities` from spec 011. Feature modules (`nanobot.capabilities.lint`) are imported
inside each test, so a tree without the feature collects cleanly and fails per requirement id.

Files: `test_a_lint.py` (FR-002 finding codes, SC-001), `test_b_lint_modes.py` (FR-002 load modes,
`capabilities list` lint column, `capabilities lint` CLI, SC-001 through the CLI),
`test_c_pending.py` (FR-001, SC-002), `test_d_metering.py` (FR-003, SC-003),
`test_e_host_tools.py` (FR-004), `test_f_docs.py` (FR-005). Helpers: `_h010.py` (lazy lint access,
skills on disk, the `python -m nanobot capabilities` runner, Dream loops driven by a scripted fake
provider, ContextBuilder with a `capabilities` block, the `llm_usage` row reader), `conftest.py`
(isolation, loguru capture, per-id summary). Fixtures: `fixtures/live_skills/<name>/SKILL.md`,
byte-identical read-only copies of the four live skills in `~/.nanobot/skills/`
(`bypass-command-allowlist`, `bypass-filter-piping`, `bypass-filter-resource-check`,
`background-du-bypass`; sha256 checked equal when copied on 2026-10-03).

## Baseline

| file | tests collected | baseline (spec commit 1163df0e) |
|---|---|---|
| test_a_lint.py | 108 | 107 fail, 1 pass |
| test_b_lint_modes.py | 19 | 17 fail, 2 pass |
| test_c_pending.py | 32 | 26 fail, 6 pass |
| test_d_metering.py | 11 | 8 fail, 3 pass |
| test_e_host_tools.py | 12 | 8 fail, 4 pass |
| test_f_docs.py | 21 | 20 fail, 1 pass |
| **total** | **203** | **186 fail, 17 pass, 0 error** |

Collection is clean on the baseline (203 collected, no collection errors). Baseline failures are
feature-missing: `ModuleNotFoundError: nanobot.capabilities.lint`, missing config fields
(`DreamConfig.skill_writes`, `CapabilitiesConfig.lint`), Dream writing into `skills/`, no usage
rows, `No such command 'pending'/'accept'/'reject'/'lint'`, no exit 2 from the entry points, stale docs.

Tests that pass on the baseline pin behaviour the spec keeps: the bundled skills exist (SC-001
precondition), `lint: off` leaves skills alone, Dream may still read `skills/` and edit memory files,
`direct` mode writes to `skills/` as today (the baseline ignores the unknown key), the loader never
lists `skills-pending/` or `skills-rejected/`, a recording failure does not fail a side call, the
gateway-built loop has the three host tools, `host_tools=False` still constructs, gateway and
`agent` reach startup when nothing is missing, and the repo has `Dockerfile` and `compose.yaml`.

Tester self-check: all 203 tests also passed against a private throwaway reference implementation of
the spec written by the tester in a scratch copy of the spec commit (deleted; never shared), so a
failure against an implementation is not a harness bug by construction of that check. Mutation
checks on that reference: a 50-char window fails FR-002 (1); not mapping `edit_file` in pending mode
fails FR-001 (1); exit 1 instead of 2 fails FR-004 (7); `--force-lint` also skipping validation fails
FR-001 (1).

## Requirement -> tests

Parametrised tests count once per parameter in the run summary. Prefixes: a lint, b lint modes,
c pending, d metering, e host tools, f docs.

| id | tests |
|---|---|
| FR-001 | c:test_skill_writes_default_is_pending<br>c:test_skill_writes_accepts_both_modes_and_spellings (param)<br>c:test_skill_writes_rejects_unknown_value<br>c:test_sc002_default_config_dream_create_goes_to_pending<br>c:test_bare_loop_default_is_pending<br>c:test_pending_mode_apply_patch_add_goes_to_pending<br>c:test_pending_mode_direct_write_into_skills_pending_is_allowed<br>c:test_pending_mode_merge_rewrite_goes_to_pending<br>c:test_pending_mode_merge_edit_goes_to_pending<br>c:test_pending_mode_dream_can_still_read_skills<br>c:test_pending_mode_memory_edits_are_unaffected<br>c:test_pending_mode_prompt_mentions_skills_pending<br>c:test_dream_template_mentions_skills_pending<br>c:test_direct_mode_writes_to_skills<br>c:test_direct_mode_merge_rewrites_in_place<br>c:test_loader_never_lists_or_loads_pending<br>c:test_capabilities_list_does_not_show_pending<br>c:test_pending_lists_each_with_validation_and_lint<br>c:test_pending_with_none_is_not_an_error<br>c:test_accept_valid_clean_moves_into_skills<br>c:test_accepted_skill_is_listed_by_the_loader<br>c:test_accept_with_lint_findings_exits_one_and_stays_pending<br>c:test_accept_force_lint_moves_linty_skill<br>c:test_accept_invalid_exits_one_and_stays_pending<br>c:test_accept_invalid_with_force_lint_still_exits_one<br>c:test_accept_unknown_name_fails<br>c:test_reject_moves_to_dated_rejected_dir<br>c:test_reject_does_not_touch_other_pending<br>c:test_reject_unknown_name_fails |
| FR-002 | a:test_finding_type_and_fields<br>a:test_clean_text_has_no_findings<br>a:test_empty_text_has_no_findings<br>a:test_all_four_codes_together<br>a:test_line_is_one_based_line_of_the_match<br>a:test_line_numbers_for_each_code<br>a:test_bypass_language_every_trigger_keyword_pair (param 3x8)<br>a:test_bypass_language_keyword_before_trigger (param)<br>a:test_bypass_language_is_case_insensitive (param)<br>a:test_bypass_language_substring_and_multiline (param)<br>a:test_window_within_40_chars_matches (param gaps 1,2,20,39,40)<br>a:test_window_beyond_40_chars_does_not_match (param gaps 41,60,200)<br>a:test_window_with_words_and_spaces<br>a:test_bypass_language_negative (param)<br>a:test_pipe_to_shell_positive / _negative (param)<br>a:test_sudo_stdin_positive / _negative (param)<br>a:test_secret_path_positive / _negative (param)<br>a:test_live_bypass_skills_each_have_bypass_language (param)<br>a:test_bundled_skill_has_zero_findings (param)<br>b:test_lint_config_default_is_warn<br>b:test_lint_config_accepts_modes (param)<br>b:test_lint_config_rejects_unknown_mode<br>b:test_default_loader_warns_and_keeps_skill<br>b:test_warn_mode_logs_once_per_skill_and_code<br>b:test_refuse_mode_skips_skill<br>b:test_off_mode_keeps_skill_without_lint_warnings<br>b:test_list_shows_lint_codes_in_warn_mode<br>b:test_list_refuse_mode_omits_skill<br>b:test_list_off_mode_has_no_lint_column<br>b:test_lint_cli_file_with_findings_exits_one<br>b:test_lint_cli_clean_file_exits_zero<br>b:test_lint_cli_skill_directory<br>b:test_lint_cli_skills_root_names_the_bad_skill<br>b:test_lint_cli_reports_pipe_and_sudo<br>b:test_lint_cli_live_fixtures_exit_one<br>b:test_lint_cli_bundled_skills_exit_zero |
| FR-003 | d:test_image_generation_adds_exactly_one_row<br>d:test_image_row_fields<br>d:test_image_row_records_tokens_when_reported<br>d:test_image_recording_failure_does_not_fail_the_call<br>d:test_transcription_adds_exactly_one_row<br>d:test_transcription_row_fields<br>d:test_transcription_recording_failure_does_not_fail_the_call<br>d:test_embedding_call_adds_exactly_one_row<br>d:test_local_embedding_row_is_local_tier_cost_zero<br>d:test_embedding_recording_failure_does_not_fail_the_call<br>d:test_three_side_calls_add_three_rows_one_per_kind |
| FR-004 | e:test_gateway_loop_registers_the_three_host_tools<br>e:test_kernel_host_without_host_tools_still_constructs<br>e:test_gateway_starts_past_the_guard<br>e:test_gateway_missing_host_tool_exits_2_naming_it (param)<br>e:test_serve_registers_host_tools_and_starts<br>e:test_serve_missing_host_tool_exits_2_naming_it (param)<br>e:test_agent_registers_host_tools_and_starts<br>e:test_agent_missing_host_tool_exits_2_naming_it (param) |
| FR-005 | f:test_repo_has_dockerfile_and_compose<br>f:test_plan_no_longer_claims_there_is_no_dockerfile<br>f:test_plan_names_the_current_dockerfile_and_compose_file<br>f:test_spec004_status_no_longer_says_nothing_is_built<br>f:test_spec004_status_names_what_is_built (param)<br>f:test_spec004_status_says_where_the_harness_lives<br>f:test_spec004_fr008_pin_is_corrected<br>f:test_register_lists_open_item (param x5)<br>f:test_register_item_has_status_and_owner (param x5)<br>f:test_register_is_local_not_only_a_pointer<br>f:test_register_records_accepted_sudo_risk |
| SC-001 | a:test_live_bypass_skills_each_have_bypass_language (param x4)<br>a:test_bundled_skill_list_is_not_empty<br>a:test_bundled_skill_has_zero_findings (param, one per bundled skill)<br>b:test_lint_cli_live_fixtures_exit_one<br>b:test_lint_cli_bundled_skills_exit_zero |
| SC-002 | c:test_sc002_default_config_dream_create_goes_to_pending<br>c:test_bare_loop_default_is_pending |
| SC-003 | d:test_image_generation_adds_exactly_one_row<br>d:test_transcription_adds_exactly_one_row<br>d:test_embedding_call_adds_exactly_one_row<br>d:test_three_side_calls_add_three_rows_one_per_kind |

## Interpretations pinned

1. **Config locations.** `dream.skill_writes` is `agents.defaults.dream.skill_writes` (the existing
   `DreamConfig`; camelCase `skillWrites` accepted like every config key), `Literal["pending",
   "direct"]`, default `"pending"`, unknown values rejected. `capabilities.lint` is a
   `CapabilitiesConfig` field, `Literal["off","warn","refuse"]`, default `"warn"`, unknown rejected.
2. **Dream runs.** Through `AgentLoop.run_dream()` with a scripted `MagicMock` provider exactly as
   `tests/agent/test_dream_direct.py` does. "Default config" = `AgentLoop.from_config(Config())` (SC-002)
   and also a bare `AgentLoop(...)` with no config. Direct mode = `from_config` with
   `skill_writes="direct"`.
3. **"The filesystem tools Dream uses map the target."** In pending mode a Dream `write_file`,
   `apply_patch` add, or `edit_file` aimed at `skills/<name>/...` lands in
   `skills-pending/<name>/...`; `skills/` is byte-identical afterwards. A merge via `edit_file` on an
   existing skill produces the edited copy in `skills-pending/<name>/SKILL.md` (original untouched).
   Writing straight to `skills-pending/<name>/` (what the prompt tells Dream) is also allowed.
   `read_file` of `skills/` keeps working. Memory files are unaffected. The prompt seen by the model in
   a default run, and the raw `nanobot/templates/agent/dream.md`, mention `skills-pending`.
4. **Loader.** `SkillsLoader` never lists, loads, summarises or `$`-invokes anything under
   `skills-pending/` (or `skills-rejected/`); `capabilities list` does not show them.
5. **CLI surface.** `pending`, `accept <name> [--force-lint]`, `reject <name>` and `lint [PATH]` are
   `nanobot capabilities` subcommands; `pending`/`accept`/`reject` take `--workspace` like the 011
   siblings. `pending` exits 0 with nothing pending and prints each pending name with its lint codes,
   and for an invalid one either the `validate_skill` problem text or the word "invalid". `accept`:
   exit 0 and the directory moves byte-identical into `skills/` when valid and lint-clean; exit 1 and
   nothing moves on a lint finding (unless `--force-lint`) or a validation problem (`--force-lint`
   never overrides validation); an unknown name exits non-zero and changes nothing. `reject`: exit 0,
   moved to `skills-rejected/<date>/<name>/SKILL.md` where the first component contains today's date
   (ISO `YYYY-MM-DD` or `YYYYMMDD`, local or UTC); unknown name exits non-zero.
6. **Lint semantics.** `Finding` has `code: str`, `line: int` (1-based line where the match starts),
   `excerpt: str` (contains the matched words). `bypass-language`: substring match (so `filters`,
   `bypass-command-allowlist` match), either order, across newlines, case-insensitive; "within 40
   characters" = at most 40 characters *between* the two words (gap 40 matches, gap 41 does not).
   `background-du-bypass` only satisfies SC-001 via keyword-before-trigger across lines
   (`filters.\n---\n\n# background-du-bypass`, gap 24). `pipe-to-shell`: `curl`/`wget` or `base64 -d`
   then `| sh` / `| bash` as a command word on the same line (`| sha256sum`, `| jq`, `cat x | bash` are
   not findings). `sudo-stdin`: `sudo -S` (case-sensitive flag; `sudo -s` is not). `secret-path`:
   substrings `keys.env`, `.ssh/id_`, `auth.json`. Clean text returns `[]`. Finding counts per code
   are not pinned.
7. **Lint at load.** Applies to workspace skills. `warn` (also the bare `SkillsLoader` default): the
   skill stays listed and loadable; exactly one WARNING log record per (skill, code) names both, across
   repeated listings. `refuse`: the skill is not listed, loaded or summarised. `off`: listed, no lint
   warnings. In-process mode plumbing is the 011 construction: a `ContextBuilder` whose host env carries
   `Config(capabilities=...)` (and whose constructor gets the same block when it takes one).
   `capabilities list` (plain text) appends a `lint:<codes>` field (codes comma- or otherwise
   joined, up to the next tab) to a linty skill's line in `warn` mode only; `refuse` omits the skill;
   `off` has no `lint:`.
8. **`capabilities lint [PATH]`.** PATH may be a SKILL.md file, a skill directory or a skills root
   (searched recursively for `SKILL.md`). Exit 1 and the codes printed when any finding exists
   (a skills root also names the offending skill); exit 0 otherwise. The no-PATH default is not tested.
9. **SC-001 bundled.** Every `nanobot/skills/*/SKILL.md` has zero findings of any code (stronger
   than "no bypass-language"; true at the spec commit).
10. **Metering.** A "usage row" is a row in the `llm_usage` store's `llm_calls` table (what
    `count()`/`recent_calls()` read), found in any `llm_usage.sqlite3` under the per-test HOME (the
    default legacy data dir) or `tmp_path`. kind/purpose = the row's `purpose` column (or a `kind`
    column if one is added) equal to `image` / `transcription` / `embedding`. Image = calling
    `OpenRouterImageGenerationClient(api_key=...).generate(...)` from `image_generation.py`;
    transcription = `OpenAITranscriptionProvider(api_key=...).transcribe(path)`; both with HTTP answered
    in process by patching `httpx.AsyncClient.send`. Rows carry provider (`openrouter` / `openai`,
    case-insensitive), model, and `key_fp = sha256(key.strip())[:12]` (spec 009), never the key. When
    the image response reports `usage` tokens, the row carries them. Embedding = one `VecStore` embedding
    call with an injected fake `Embedder` (`model_name="all-MiniLM-L6-v2"`): the public `embed()` if one
    exists, else `VecStore._embed` (the single funnel; every public VecStore embedding path also needs
    sqlite-vec and numpy, which the suite must not install). One call embedding three texts = exactly
    one row, `tier="local"`, `cost_usd == 0.0` (not NULL), `key_fp` NULL. A broken store (every
    `LLMUsageStore` write method raising) never fails any of the three calls.
11. **Host tools guard.** A missing tool is forced in process by wrapping `ToolLoader.load` to
    unregister the named tools after loading. Gateway = `nanobot.cli.gateway_runtime._run_gateway(config,
    health_server_enabled=False, webui_static_dist=False, webui_bundle_mode="skip",
    unconfigured_provider_error=...)` stopped by a sentinel `ChannelManager`; `serve` / `agent -m` =
    in-process `typer` `CliRunner` over `nanobot.cli.commands.app`, stopped by a sentinel `create_app` /
    `AgentLoop.process_direct`. "Exit 2" = `typer.Exit(2)`/`SystemExit(2)` (CliRunner exit code 2) and
    the sentinel not reached; "naming the missing tools" = each missing name appears in the
    stdout/stderr, the exception text or a WARNING+ log record. With nothing forced, all three entry
    points must start, and the `serve` and `agent` loops must carry `message`, `cron` and `run_cli_app`.
    **Note:** at the spec commit `serve` builds its loop without a `CronService`, so `cron` is not
    registered there; FR-004 therefore requires `serve` to register `cron` (baseline fails
    `test_serve_registers_host_tools_and_starts` for that reason).
12. **Docs.** PLAN.md: no "has no `Dockerfile`" claim remains; `compose.yaml` is named; the status
    block above the first `##` mentions Dockerfile and compose. Spec 004: the `**Status**:` paragraph
    no longer says nothing is built, mentions containment, held-out and sandbox, `moeka-rsi` and
    `001-005` (`-`, en/em dash, "to" or "through"); the FR-008 bullet no longer carries the old pin
    sentence (`core-slim` `6f80c392` until `003` moves it to `main`) and still talks about the pin.
    Findings register: each of R8 (mentions CI), R11 (mentions overlap), CLARIFY Q1, Q16 and usage
    review #13 is a table row under a header with Status and Owner columns (both cells non-empty) or a
    list item / heading section that says status and owner; the accepted risk names the live gateway,
    `allowSudo=true`, `restrictToWorkspace=false` (tolerant of `:`/backticks/snake_case), "accepted risk",
    the RSI VM and the ops agent.
