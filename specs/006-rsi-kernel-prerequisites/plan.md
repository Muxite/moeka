# Implementation Plan: RSI Kernel Prerequisites (harness K1-K6)

**Branch**: `006-rsi-kernel-prerequisites` | **Date**: 2026-10-01 | **Spec**: [spec.md](spec.md)

**Input**: `specs/006-rsi-kernel-prerequisites/spec.md`; `specs/004-rsi-harness/` (FR-001 to FR-006, FR-016, T003-T008);
`.agent/rsi-harness-design.md` section 4.3; `.agent/rsi-harness-impl-plan.md` (K1-K6 status table).

## Summary

Six small kernel changes, each its own commit with tests first, all inside `nanobot/kernel/`, `nanobot/agent/` and
the `moeka` re-export modules. No new module, no host code, no new dependency. Built under the held-out pattern: the
implementer gets this spec, the tester writes hidden tests from it into `~/projects/.heldout/moeka/006-rsi-kernel-
prerequisites/`, and feedback comes through the redacting runner (`007-heldout-runner`) or, until that lands, the
manual RUN.md procedure used for 005.

## Technical Context (as of 2026-10-01, verified against `consolidate/new-main` `cd03700e`)

**Language/Version**: Python 3.11+ (asyncio). **Dependencies**: none new. **Testing**: pytest (`asyncio_mode=auto`),
`scripts/test-docker.sh`. **Target**: Linux; kernel is platform-neutral. **Scale**: about 300-500 changed lines plus
tests.

Code facts each item builds on:

| Item | Where today | What is there |
|---|---|---|
| K1 | `nanobot/kernel/variants.py:178-211` `fingerprint()` | components `system_prompt`, `tools`, `model`, `sampling`; `_canonical()` is `json.dumps(sort_keys, (",", ":"), ensure_ascii=False)` |
| K1 | `nanobot/agent/skills.py:128-181` `SkillsLoader.list_skills` | effective set: inline, workspace, plugin, builtin; `allowed_skills`/`disabled_skills`; `build_skills_summary` uses `filter_unavailable=False` |
| K2 | `nanobot/kernel/variants.py:73-115` `Variant`; `nanobot/agent/tools/loader.py:459-470` `_apply_variant`; `nanobot/agent/tools/base.py:661-675` `set_description_override`, `to_schema` | top-level description override only; `parameters` from `tool_parameters` is a fresh deepcopy per access |
| K3 | `nanobot/kernel/trace_hook.py:163-186` `_tool_event` | `tool.call` keys: `session_key, iteration, tool, call_id, ok, args_valid, error_kind, error, duration_ms`; `tool_call.arguments` reachable from the hook |
| K3 | `nanobot/kernel/trace.py:367-416` `EVENTS` | catalogue strings |
| K4 | `nanobot/kernel/agent.py:196-207` `RunLimits`; `:692-697` `Agent.__init__` raises `NotImplementedError` | `StopReason` already includes `"tool_error"` (`:124-135`); `_FAILED_STOPS` includes it (`:532`) |
| K4 | `nanobot/agent/runner.py:180-238` `PolicyDenialBudget`, `AgentRunSpec.max_policy_denials`; `:690-775` tool batch, fatal `tool_error` path, `policy_denials.exhausted` stop | the ceiling pattern to copy; `BUDGET_STOP_REASONS` finalisation must NOT be reused for `tool_error` (it may make a closing model call) |
| K4 | `nanobot/agent/runner.py:1804-1806` | `AskUserInterrupt` passes through `on_execute_tool_error`, so the trace emits `ok=False` for it: exclude it from the count |
| K4 | `tests/kernel/test_agent.py:137-140` | asserts `NotImplementedError`; replace |
| K5 | `nanobot/kernel/llm.py:144` `GenerateOptions.on_unsupported`; `nanobot/kernel/agent.py:1232-1243` `_request_extras` builds `RequestExtras` without `on_unsupported` | the agent path never asks for `"raise"` |
| K5 | `nanobot/providers/base.py:1546-1610` `_apply_request_extras`; `:1659-1668` `_report_dropped_fields` | raise before send for explicit fields; quiet drop for defaults; `_sampling_support() is None` means pass-through |
| K5 | `nanobot/kernel/hostenv.py:66-86` `ModelSpec` | no unsupported-field declaration |
| K5 | `nanobot/kernel/agent.py:1341-1344` | `except LLMError` -> `result("error", error=exc)`; verify the runner/loop does not swallow `UnsupportedRequestError` into a text reply (`runner.py:413-420` re-raises) |
| K6 | `nanobot/agent/tools/filesystem.py:403-424` `_trace_skill_read`; `:450-457` call site | emits `{skill, path}` after the existence check, before size/permission errors |
| K6 | `nanobot/agent/tools/search.py:838-1320` `GrepTool` | modes `content`, `files_with_matches`, `count`; `_trace_sink` available on `_FsTool` (`filesystem.py:60,164`) |
| K6 | `nanobot/agent/runner.py:1784-1789` `file_read_context(tool_call.id, ...)` | call id is available to `read_file` only; generalise to every tool call so `skill.read.call_id` can be set |

## Constitution Check

*Gate before research and after design.*

- **I Zero ambient reads**: K1 reads skill files from paths the host handed over (`work_dir`, the variant's
  directory) and bundled package data. No environment or home access. PASS (FR-042 test).
- **II Path separation**: no new state; nothing written. PASS.
- **III Unearned knowledge**: not touched. N/A.
- **IV Attenuation**: K6 adds events only; gate verdicts unchanged. K2 changes descriptions only, never capability
  declarations. PASS.
- **V Hard failure limits**: K4 turns a declared-but-raising limit into an enforced one with a typed stop reason.
  PASS (strengthens I5).
- **VI Pareto/cost**: K3 makes repeat calls measurable (waste). Measured, not enforced. PASS.
- **VII Everything is data**: new event fields are content-free (a digest, a tool name, a call id); digest failure
  is fail-open. PASS. Stated limit: a sha256 of low-entropy arguments can be guessed by enumeration; the event is
  still content-free by the kernel's definition (no raw text).
- **VIII Verification independence**: held-out pattern; tester and implementer are different agents. PASS.
- **IX Consumers derive from moeka**: new surfaces are re-exported from `moeka.*`. PASS.
- **Import boundary**: all edits are in kernel or agent modules; no host import. PASS (FR-042).
- **Mutation tiers**: K2 widens tier 1 to parameter descriptions only; structure stays tier 2+. PASS.

## Approach (one commit per item, tests first)

1. **K3** (`trace_hook.py`, `trace.py`, `moeka/trace.py`): add `args_digest()` next to `usage_dict()`; call it in
   `_tool_event` with `getattr(tool_call, "arguments", None)` inside a `try` (FR-019); extend `EVENTS`.
2. **K4** (`agent.py`, `runner.py`, `loop.py`): add `AgentRunSpec.max_tool_errors: int | None`; a per-run counter
   incremented exactly where `on_execute_tool_error` / `on_tool_invalid` are awaited, skipping `AskUserInterrupt`
   (so it matches the trace by construction, FR-021); check after the batch, after the `ask_user` and
   `policy_denials` checks (FR-024), and break without `finalize_on_max_iterations`. Thread the limit from
   `Agent._build` (like `max_policy_denials`, `agent.py:824`) through `AgentLoop`. Drop the `NotImplementedError`.
3. **K5** (`agent.py`, `hostenv.py`, `providers/base.py`, `llm.py`): `AgentSpec.on_unsupported`; pass it into
   `RequestExtras` from `_request_extras` (always build extras when strict, so the provider check runs); under
   `"raise"` treat `_sampling_support() is None` as "nothing supported"; add `ModelSpec.unsupported_sampling` and
   apply it in the kernel before the provider call (both `Agent._request_extras` and the `kernel.llm` path) so it is
   provider-independent. Confirm the exception reaches `except LLMError` in `Agent._run`.
4. **K1** (`variants.py`): `_skills_component(loop)` from `loop.context.skills.list_skills(filter_unavailable=False)`
   plus the loader's inline map; records per FR-003/FR-004. Update the `Fingerprint` docstring.
5. **K2** (`variants.py`, `loader.py`, `base.py`, `moeka/errors.py`): new field and grammar check; at load, the loader
   deep-copies the tool's `parameters` once, applies overrides, and stores a per-instance parameters override used by
   `to_schema()` and `Agent.tools()`; validation keeps using the base schema (FR-013). `VariantError(ValueError)` with
   `variant`, `tool`, `path`.
6. **K6** (`filesystem.py`, `search.py`, `runner.py`): set a tool-call-id context for every tool call (generalise
   `file_read_context`); `read_file` emits after a successful read only; `grep` collects distinct skill files among
   returned lines in content mode. Add `via`, `call_id`.
7. Docs: `.agent/moeka-kernel-design.md` I5 text (FR-027), `docs/python-sdk.md` strict-sampling limit (FR-034),
   K1-K6 status lines in `specs/004-rsi-harness/plan.md`.

## Project Structure

### Documentation (this feature)

```text
specs/006-rsi-kernel-prerequisites/
├── spec.md
├── plan.md
└── tasks.md
```

### Source Code (repository root)

```text
nanobot/kernel/variants.py      # K1, K2
nanobot/kernel/trace_hook.py    # K3
nanobot/kernel/trace.py         # EVENTS (K3, K6)
nanobot/kernel/agent.py         # K4, K5
nanobot/kernel/hostenv.py       # K5 ModelSpec.unsupported_sampling
nanobot/kernel/llm.py           # K5 kernel.llm path
nanobot/agent/runner.py         # K4 ceiling, K6 call-id context
nanobot/agent/loop.py           # K4 plumbing
nanobot/agent/tools/loader.py   # K2
nanobot/agent/tools/base.py     # K2 per-instance parameters override
nanobot/agent/tools/filesystem.py, search.py   # K6
nanobot/providers/base.py       # K5 fail-closed pass-through
moeka/trace.py, moeka/errors.py # re-exports
tests/kernel/test_variants.py, test_trace_spans.py, test_agent.py, test_llm_engine.py, test_agent_tools.py
```

**Structure Decision**: edit existing kernel and agent modules only; tests extend the existing kernel test files.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| New `ModelSpec.unsupported_sampling` field (K5, FR-033) | the only way a host can make a backend that ignores `seed` visible | "strict only on declared provider support" leaves the Ollama/vLLM case silent, which is the case the harness hits; pending owner Q (spec marker) |
| Per-instance parameters override on `Tool` (K2) | descriptions must change per kernel without touching the class | mutating `tool_parameters`' class-level schema leaks across kernels in one process (Variant contract) |
