# Plan: library hygiene and audit fixes (spec 010)

## FR-001 pending skills

- Config: `DreamConfig.skill_writes: Literal["pending", "direct"] = "pending"` (camelCase `skillWrites`).
  `AgentLoop(dream_skill_writes=...)`, passed by `from_config`, sets `MemoryStore.dream_skill_writes`
  (default `"pending"`, so kernel hosts and bare loops stage too).
- Tool mapping: `nanobot/agent/dream_tools.py` subclasses Dream's four file tools with a mixin that overrides
  `_resolve_write` / `_resolve_read` after the normal resolution and floor check:
  - write to `skills/<name>/<rest>` -> `skills-pending/<name>/<rest>`; when `skills/<name>/` exists and has no
    pending copy, the whole directory is copied first (a merge edits the copy; `skills/` is never written);
  - read of `skills/<name>/<rest>` -> the pending copy when it exists (Dream verifies what it wrote), else
    `skills/` as before;
  - the mapped write target is re-checked against the protected floor; memory files are untouched.
  `MemoryStore.build_dream_tools(skill_writes=None)` picks the tool classes; tool names are unchanged.
- Prompt: `dream.md` gains a conditional bullet (`skill_writes_pending`) saying writes are staged and that
  allowlist/guard-bypass skills are rejected. `"direct"` renders byte-identical to the old prompt. A workspace
  prompt override gets no bullet, but the mapping still applies.
- `SkillsLoader` only ever scans `skills/`, so `skills-pending/` is never listed or loaded (tested).
- Review: `nanobot/capabilities/pending.py` (`list_pending`, `accept_pending`, `reject_pending`,
  `PendingError`). Accept requires no `validate_skill` problem and no lint finding (`force_lint` waives lint
  only). A skill being replaced moves to `skills-superseded/<date>/<name>` (never deleted). Reject moves to
  `skills-rejected/<date>/<name>`, suffixing `-1`, `-2` on a collision.
- CLI: `nanobot capabilities pending [--json]`, `accept <name> [--force-lint]` (exit 1 when refused),
  `reject <name>`.

## FR-002 lint

- `nanobot/capabilities/lint.py`: `lint_text(text) -> list[Finding(code, line, excerpt)]`, one finding per
  (code, line), sorted by line then code; `lint_file`, `finding_codes`, `FINDING_CODES`.
  - `bypass-language`: `bypass|circumvent|evad(e|es|ed|ing)` and a control word within 40 characters (gap
    between the matches, either order, across lines). Control words match as stems (`filters`, `restricted`,
    `restrictions`, `deny-pattern`, `allow-list`), a deliberate widening of the spec's literal list;
  - `pipe-to-shell`, `sudo-stdin` (`sudo [opts] -S`, `-kS`, `--stdin`), `secret-path`: per line.
- `CapabilitiesConfig.lint: Literal["off", "warn", "refuse"] = "warn"`. `SkillsLoader(lint_mode=...)` lints
  workspace skills after validation (cached by mtime/size); `warn` logs once per (skill, code), `refuse`
  skips. `ContextBuilder` sets the mode from `capabilities_config`; `AgentLoop` hands it to subagents.
- CLI: `capabilities list` gains a sixth column, `lint:<codes>` or `-` (JSON key `lint`); `capabilities lint
  [PATH]` (file, or every `SKILL.md` under a directory; default the workspace skills) exits 1 on findings.
- SC-001 fixtures: `tests/capabilities/fixtures/live_bypass_skills/` holds copies of the four live skills
  (home path replaced by `/home/user`). Note: the live `hive-remote` skill also gets a `bypass-language`
  warning (it forbids `--permission-mode bypassPermissions`); warn mode only logs it.

## FR-003 metered side calls

- `nanobot/llm_usage/side_calls.py`: `record_side_call(kind, provider, model, ...)` writes one `llm_calls`
  row through `LLMUsageStore.record(call, event)`: `purpose` = kind, `tags` = `{kind, units, unit}`, a
  unique `call_id`, `key_fp` from the key, source from `current_llm_usage_source()`; fail-open (returns
  False). `set_side_call_recorder` lets a host or a test reroute rows.
- Image: `ImageGenerationProvider.__init_subclass__` wraps every concrete `generate` (bundled and third-party
  providers). One row per outer call (a `ContextVar` stops nested `super().generate` double counting);
  tokens and cost from the response's `usage` when present; errors record `finish_reason="error"`; a
  missing-key refusal (no request made) and cancellation record nothing. `usage_data_dir` attribute.
- Transcription: new base `TranscriptionProvider` with the same wrapping for all six adapters (each now has a
  `provider_name`). No row when the call returns early without a request (no key, missing file); an empty
  transcript is an error row; `units` = audio bytes.
- Embedding: `VecStore._embed` records one row per call (`units` = texts); provider `local` unless the
  embedder declares `provider`; local = `tier="local"`, `cost_usd=0.0`. `usage_data_dir` is threaded from
  `MoekaCore` (`env.paths.data_dir`) and from `moeka.Kernel` through `_MemoryRegistry` / `DocStore`.
- `None` data dir = the legacy data dir (`kernel.legacy`, the sanctioned ambient adapter).
- `test_r10_unmetered_model_paths_are_inventoried` now treats `record_side_call(` as metered and expects an
  empty unmetered set; `schemas/ATTRIBUTION.md` "Not covered" updated (still not budget-gated).

## FR-004 host tools guard

- `nanobot/cli/host_tools_guard.py`: `expected_host_tools(config)` (the three, minus a tool the config turns
  off: `run_cli_app` with `tools.cliApps.enable=false`, or excluded by `toolsDeny` / `toolsAllow`),
  `missing_host_tools`, `require_host_tools(loop, config, entry=...)` -> `typer.Exit(2)` with the names.
- Called right after construction in `gateway_runtime._run_gateway`, `serve` (commands.py) and the CLI agent.
  `serve` wires no `CronService`, so it calls the guard with `with_cron=False` and checks only `message` and
  `run_cli_app` (FR-004 as amended by `d0684e29`); gateway and CLI agent require all three. CLI test fakes
  gained a `tools` attribute.

## FR-005 docs

- `docs/PLAN.md`: status block states the current repository facts (Dockerfile + compose.yaml exist for the
  optional container image, Dockerfile.test, no docker-compose.yml / entrypoint.sh).
- `specs/004-rsi-harness/spec.md`: status names what is built (moeka-rsi specs 001-003 merged, 004 on a
  branch; kernel specs 006-008) and FR-008's pin (moeka-rsi `core/` at `fe52e31e`, not `core-slim`).
- `docs/findings/README.md`: local register (R8, R11, CLARIFY Q1, Q16, usage #13 with status/owner/evidence),
  the accepted risk line, and the items this spec closes. `CLAUDE.md` deviations list updated.
