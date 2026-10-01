# Feature Specification: RSI Kernel Prerequisites (harness K1-K6)

**Feature Branch**: `006-rsi-kernel-prerequisites`

**Created**: 2026-10-01

**Status**: Draft (implementation-ready; held-out pattern)

**Input**: Plan step M10 (`keep-out-strict-number-gleaming-island.md`): close the six kernel gaps the RSI harness
needs (harness design section 4.3, `004` FR-001 to FR-006, tasks T003-T008). K1 skills in the fingerprint, K2
tool parameter-description overrides, K3 `args_digest` on `tool.call`, K4 `RunLimits.max_tool_errors`, K5 strict
sampling on `AgentSpec`, K6 `skill.read` coverage.

**Held-out pattern** (`004` FR-016): a tester agent writes hidden tests from this spec only; a different implementer
builds from the same spec and never sees those tests. Hidden tests use only the surfaces in the
[Test Harness Contract](#test-harness-contract). Feedback to the implementer is per requirement id and count only.
Ids "K1"-"K6" below are the harness design's prerequisite ids, not the kernel design's assumptions K1-K11.

## Terms

- **Run**: one `Agent.run` / `Agent.run_sync` call (one user message, any number of model calls and tool calls).
- **Iteration**: one model call of a run plus the tool calls of its response.
- **Effective skill set**: every skill an agent's skills summary can name or its context can load in full: inline
  skills (`AgentSpec.inline_skills`), workspace skills (`<work_dir>/skills/<name>/SKILL.md`), agent plugin skills,
  and built-in skills (from `Variant.builtin_skills_dir` when set, else the bundled skills), after inline shadowing
  and after `skills_include` / `skills_exclude`. Skills whose requirements are unmet ("unavailable") are members.
- **Skill file**: a regular file named exactly `SKILL.md` whose grandparent directory is named `skills`, or whose
  grandparent directory is the agent's built-in skills root (`Variant.builtin_skills_dir` when set). The skill's
  name is its parent directory's name.
- **Tool error**: a tool call of a run whose `tool.call` trace event has `ok=False`, except a call ended by an
  `ask_user` interruption.
- **Explicit sampling**: the `Sampling` a run asks for: the run's `sampling=` argument when given, else
  `AgentSpec.sampling`. Fields set on `ModelSpec.sampling` are **model defaults**, not explicit.

## User Scenarios & Testing *(mandatory)*

Consumers: the RSI harness (one kernel per rollout, scoring from traces and fingerprints), and any embedder that
caches results by fingerprint or needs reproducible, bounded runs.

### User Story 1 - A fingerprint that sees every skill byte (Priority: P1)

The harness keys its result cache and its lineage on `agent.fingerprint().digest`. Editing a skill that is not
always-on must change the digest; moving the same tree to another directory must not.

**Why this priority**: without it two different candidates share a cache key and a mutation can be scored as its
parent (harness M4 verification).

**Independent Test**: build two kernels over byte-identical skill trees in different directories, then edit one
`SKILL.md` body; compare `fingerprint().components["skills"]`.

**Acceptance Scenarios**:

1. **Given** a variant whose `builtin_skills_dir` holds skill `alpha` (not always-on), **When** one byte of
   `alpha/SKILL.md` changes, **Then** `components["skills"]` and `digest` change and the other four components do
   not.
2. **Given** the same skills tree copied to two directories, **When** each is the `builtin_skills_dir` of a kernel,
   **Then** both `components["skills"]` are equal.
3. **Given** `skills_exclude=("alpha",)`, **When** `alpha`'s files change, **Then** `components["skills"]` does not
   change.

---

### User Story 2 - Bounded, attributable tool behaviour (Priority: P1)

A rollout ends at a declared tool-error ceiling with a typed stop reason, and every tool call carries a stable
digest of its arguments so the harness counts repeats without parsing transcripts.

**Why this priority**: error-recovery tasks (harness M5) and loop/repeat metrics (M4) depend on it; constitution V
forbids a declared limit that is not enforced.

**Independent Test**: script a fake model to call a failing tool repeatedly with `RunLimits(max_tool_errors=3)`;
count model calls and read `RunResult.stop_reason` and the `tool.call` events.

**Acceptance Scenarios**:

1. **Given** `max_tool_errors=3` and a model that calls an unknown tool every iteration, **When** the run executes,
   **Then** exactly 3 model calls are made and `stop_reason == "tool_error"`.
2. **Given** two calls with arguments `{"a": 1, "b": 2}` and `{"b": 2, "a": 1}`, **When** both execute, **Then**
   their `tool.call.args_digest` values are equal and equal `moeka.trace.args_digest({"a": 1, "b": 2})`.

---

### User Story 3 - Reproducible sampling or a hard failure (Priority: P1)

A rollout that asks for `seed=7` either sends it or fails before anything is sent.

**Why this priority**: a dropped seed makes a rollout non-reproducible and silently corrupts noise estimates (harness
M3).

**Independent Test**: a fake provider that declares no `seed` support; an agent with `on_unsupported="raise"` and
`Sampling(seed=7)`; check that no provider call is recorded and the run ends with `stop_reason == "error"`.

**Acceptance Scenarios**:

1. **Given** `AgentSpec(on_unsupported="raise", sampling=Sampling(seed=7))` on a provider without `seed`, **When**
   the run starts, **Then** zero provider calls are made and `RunResult.error` is an `UnsupportedRequestError` with
   `fields == ("seed",)`.
2. **Given** the same with `on_unsupported="drop"`, **When** the run executes, **Then** the request is sent without
   `seed` and one `sampling.dropped` event lists `seed`.

---

### User Story 4 - Mutable parameter descriptions, immutable schemas (Priority: P2)

A candidate variant rewrites the description of a tool parameter; the model sees the new text; the parameter's
type, constraints and requiredness are unchanged.

**Why this priority**: parameter descriptions enter mutation scope at harness M5; structural changes are tier 2+.

**Independent Test**: `Variant(tool_param_descriptions={"read_file": {"path": "X"}})`; read `agent.tools()` and the
tool definitions in the fake provider's recorded request.

**Acceptance Scenarios**:

1. **Given** the variant above, **When** an agent lists its tools, **Then** `read_file`'s `parameters["properties"]
   ["path"]["description"] == "X"` and the schema minus descriptions equals the base schema.
2. **Given** a path `"nonexistent"` for `read_file`, **When** the agent is built, **Then** a `VariantError` naming
   the tool and path is raised.

---

### User Story 5 - Skill reads are observed whatever tool reads them (Priority: P3)

`skill.read` fires when skill content reaches the model through `read_file` or `grep`, with the tool and call id.

**Why this priority**: skill-selection accuracy and per-skill contribution (harness metrics) use it; until then it
is a lower bound. Optional for M5.

**Independent Test**: script a `grep` (content mode) over a skills directory; count `skill.read` events and join
them to the `tool.call` by `call_id`.

**Acceptance Scenarios**:

1. **Given** a `grep` whose result contains lines from `skills/alpha/SKILL.md` and `skills/beta/SKILL.md`, **When**
   it executes, **Then** two `skill.read` events with `via="grep"` and the call's `call_id` are emitted.
2. **Given** a `grep` in `files_with_matches` mode over the same tree, **When** it executes, **Then** no `skill.read`
   is emitted.

### Edge Cases

- A tool call with `arguments` given as a JSON string, as an unparsable string, as `None`, or containing `NaN`
  (FR-015).
- Tool errors spread over iterations versus several in one response (FR-023).
- The tool-error ceiling, the policy-denial ceiling and `ask_user` in the same iteration (FR-024).
- A provider that accepts `seed` on the wire and ignores it (FR-033, FR-034).
- A fallback provider whose primary supports a field and a fallback candidate does not (FR-032).
- An unreadable file inside a skill directory (FR-006).
- A variant naming a tool the agent does not have (FR-011).
- A skill directory containing `__pycache__` or dotfiles (FR-003).

## Requirements *(mandatory)*

### Functional Requirements

#### K1: skills in the fingerprint

- **FR-001**: `Fingerprint.components` MUST have exactly the keys `system_prompt`, `tools`, `model`, `sampling`,
  `skills`. `Fingerprint.digest` MUST stay the sha256 hex of the `name=hex` lines of all components sorted by name
  and joined by `"\n"`.
- **FR-002**: `components["skills"]` MUST be computed over the effective skill set of the agent's loop (Terms),
  including always-on and unavailable skills, and excluding skills removed by `skills_include` / `skills_exclude`.
- **FR-003**: Each member contributes records `(key, value)`:
  - a file-based skill `N` with directory `D` (the parent of its `SKILL.md`): one record per regular file `F` under
    `D`, recursively, following symlinks that resolve to regular files, and skipping any path with a component named
    `__pycache__`, any component starting with `.`, and any file ending in `.pyc`; key `"file:" + N + "/" + <F
    relative to D, POSIX separators>`, value the sha256 hex of `F`'s bytes;
  - an inline skill `N`: one record with key `"inline:" + N` and value the sha256 hex of the UTF-8 canonical JSON
    (`sort_keys=True`, separators `(",", ":")`, `ensure_ascii=False`) of the object `{"name", "description",
    "content", "metadata"}` with the skill's values (`null` when unset).
- **FR-004**: `components["skills"]` MUST equal the sha256 hex of the UTF-8 bytes of the concatenation, over records
  sorted by key (Python `str` ordering), of `key + "\n" + value + "\n"`. An empty effective skill set gives the
  sha256 of the empty byte string.
- **FR-005**: The `skills` component MUST NOT depend on absolute paths, file modification times or directory
  iteration order. Consequences tests check: byte-identical trees in different directories give equal components;
  changing, adding or removing any counted file of a member changes it; changes inside `__pycache__`, dot paths or
  non-member skill directories do not change it.
- **FR-006**: `Agent.fingerprint()` MUST read skill files at call time (an edit between two calls is reflected), MUST
  NOT emit `skill.read`, and MUST raise `OSError` (never return a partial digest) when a counted file cannot be read.

#### K2: tool parameter-description overrides

- **FR-007**: `Variant` MUST accept `tool_param_descriptions: Mapping[str, Mapping[str, str]]` (tool name -> {parameter
  path -> description}), default empty. It MUST be stored frozen, keep `Variant` hashable and equal by value, and
  `Variant()` MUST equal `Variant(tool_param_descriptions={})`.
- **FR-008**: Construction MUST raise `TypeError` when the value is not a mapping of non-empty `str` to a mapping of
  `str` to `str`, and `ValueError` when a parameter path does not match the grammar: `path = segment ("." segment)*`;
  `segment = name ("[]")*`; `name` = one or more characters, none of `.`, `[`, `]`. Valid: `path`,
  `edits[].old_text`, `grid[][]`. Invalid: `""`, `.a`, `a.`, `a..b`, `[]`, `a[`, `a[0]`.
- **FR-009**: For a tool the agent's loader builds, each path MUST resolve against the tool's parameters schema: for
  each `name`, the current node MUST be a mapping whose `properties` mapping has `name`; for each `[]`, the current
  node MUST have an `items` mapping, which becomes the current node. The final node's `description` MUST be set to
  the text (added when absent). No other key anywhere in the schema may change: the schema with every overridden
  `description` key removed MUST deep-equal the base schema with the same keys removed.
- **FR-010**: The override MUST be visible in `Agent.tools()` (`ToolInfo.parameters`), in the tool definitions sent
  to the provider, and therefore in `components["tools"]`. It MUST apply per tool instance: two kernels in one
  process with different variants each see only their own override, and the base tool class is not modified.
- **FR-011**: An entry for a tool name the agent does not load MUST be ignored. A path that does not resolve for a
  loaded tool MUST make the agent build fail with `moeka.errors.VariantError` (a `ValueError` subclass) whose
  `variant`, `tool` and `path` attributes name the variant, tool and path. The build happens on the first
  `agent.tools()`, `agent.fingerprint()` or run; that call raises.
- **FR-012**: Coverage MUST equal `tool_descriptions` coverage: tools built by the tool loader (built-ins and kernel
  plugins) are overridden; MCP tools and `AgentSpec.actions` are not.
- **FR-013**: Argument validation MUST be unchanged: a call that was valid or invalid against the base schema stays
  so with any parameter-description override.

#### K3: argument digest on `tool.call`

- **FR-014**: Every `tool.call` event MUST carry `args_digest`: a 64-character lowercase hex string, or `None` when the
  arguments cannot be canonicalised (FR-015).
- **FR-015**: Canonicalisation: take `A` = the tool call's arguments as the model produced them (before casting or
  validation). If `A` is `None`, use `{}`. If `A` is a `str`, use `json.loads(A)` when that succeeds, else the string
  itself as a JSON string value. Serialise with `json.dumps(value, sort_keys=True, separators=(",", ":"),
  ensure_ascii=False, allow_nan=False)`; `args_digest` is the sha256 hex of its UTF-8 bytes. If serialisation raises
  (non-JSON type, `NaN`/`Infinity`, circular), `args_digest` is `None`. No numeric normalisation (`1` and `1.0`
  differ).
- **FR-016**: `moeka.trace.args_digest(arguments) -> str | None` MUST implement FR-015 exactly, and every event value
  MUST equal `args_digest(<that call's arguments>)`.
- **FR-017**: `args_digest` MUST be present for every kind of `tool.call`: success, error result, `result_invalid`,
  tool exception, and `invalid_args` (unknown tool or failed validation).
- **FR-018**: The event MUST NOT carry raw arguments, and `args_digest` is the only key added to `tool.call`.
- **FR-019**: Computing the digest MUST NOT raise out of the trace hook or change the run's outcome (fail-open,
  constitution VII).

#### K4: `RunLimits.max_tool_errors`

- **FR-020**: `kernel.agent(AgentSpec(..., limits=RunLimits(max_tool_errors=N)))` MUST succeed for every positive int
  `N`; `NotImplementedError` MUST NOT be raised anywhere for this field. `RunLimits` validation stays: `0`, negative,
  `bool` and non-int values raise `ValueError`.
- **FR-021**: The run's tool-error count MUST equal the number of `tool.call` events of that run with `ok=False`,
  excluding `ask_user` interruptions. It therefore counts `invalid_args`, tool exceptions, error results and
  `result_invalid`, including exec-guard refusals that are also counted toward `max_policy_denials`. Calls denied
  by the capability gate and calls the runner skips without executing emit no `tool.call` and are not counted.
- **FR-022**: The count MUST be per run: it starts at 0 for each run, and history from earlier runs in the same
  session does not count.
- **FR-023**: When, after all tool calls of an iteration have finished, the count is at least `N`, the run MUST end
  with `stop_reason == "tool_error"` and MUST NOT make another model call. All tool calls of that iteration's model
  response execute (none is cancelled because the ceiling was reached mid-response).
- **FR-024**: Precedence within one iteration: `ask_user` first, then `policy_denials`, then `tool_error`.
  `tool_error` is decided before the iteration budget, so reaching the ceiling on the last allowed iteration ends
  with `tool_error`, not `max_iterations`.
- **FR-025**: On a `tool_error` stop by this ceiling, `RunResult.error` MUST equal the string
  `"max_tool_errors: {count} tool errors (limit {N})"`; `RunResult.iterations` equals the number of model calls made;
  the run's `run.completed` event (exactly one) has `stop_reason == "tool_error"`; a stream ends with `run.failed`
  whose metadata `stop_reason` is `tool_error`.
- **FR-026**: With `max_tool_errors=None` (default), behaviour MUST be unchanged: tool errors never end a run by
  themselves. A run with `N-1` tool errors that then completes MUST end with `stop_reason == "completed"`.
- **FR-027**: The kernel design's I5 text MUST list the tool-error ceiling as enforced (documentation; checked in
  review).

#### K5: strict sampling on `AgentSpec`

- **FR-028**: `AgentSpec` MUST accept `on_unsupported: Literal["drop", "raise"] = "drop"`; any other value raises
  `ValueError`. It takes part in `AgentSpec` equality and hash.
- **FR-029**: With `"drop"`, behaviour MUST be unchanged: an explicit sampling field that is unsupported for a model
  call is removed from that request, one `sampling.dropped` event per affected call lists the removed fields, and
  the request is sent.
- **FR-030**: With `"raise"`, when any explicit sampling field is unsupported for the run's model call, no request is
  sent for it, and the run MUST end with `stop_reason == "error"`, `RunResult.error` an instance of
  `moeka.errors.UnsupportedRequestError` whose `fields` is the tuple of unsupported explicit field names in
  `Sampling` field order, no `sampling.dropped` event for that call, and `run.completed.stop_reason == "error"`.
  Because explicit sampling is fixed for a run, a run that fails this way makes zero provider calls.
- **FR-031**: Model defaults (`ModelSpec.sampling`) that are unsupported MUST still be dropped quietly under `"raise"`
  (no error, no event), as `GenerateOptions.on_unsupported` already does; only explicit fields are strict.
- **FR-032**: A field is unsupported for a call when any of these holds: (a) the provider (for a fallback provider,
  its primary) does not declare it supported for that model and reasoning effort; (b) under `"raise"` only, the
  provider declares no support information at all (pass-through), which fails closed; (c) the call's `ModelSpec`
  lists it in `unsupported_sampling` (FR-033). Fallback candidates keep dropping what they cannot send.
- **FR-033**: `ModelSpec` MUST accept `unsupported_sampling: tuple[str, ...] = ()`, the sampling fields the host
  knows the backend ignores; each name MUST be a `Sampling` field name, else `ValueError`. It applies to agent runs
  and to `kernel.llm.complete` with `GenerateOptions(on_unsupported=...)` alike: dropped and reported under
  `"drop"`, raised under `"raise"`. [NEEDS CLARIFICATION: add this host declaration now, or defer it and leave
  silently-ignored fields undetected? Recommended default: add it; it is the only way a host can make "the backend
  ignores seed" visible, and it costs one optional field.]
- **FR-034**: Stated limit: a backend that accepts a field on the wire and ignores it, when neither the provider nor
  `ModelSpec.unsupported_sampling` declares it unsupported, cannot be detected in-process. The `AgentSpec` docstring
  and `docs/python-sdk.md` MUST say so, and the kernel MUST NOT emit any event or field claiming a seed was honoured
  (documentation; checked in review and by a grep that no `sampling.honoured`-style event exists in `EVENTS`).

#### K6: `skill.read` coverage

- **FR-035**: `skill.read` MUST be emitted when a tool call's successful result returns content of a skill file
  (Terms) to the model, for `read_file` (any successful read, including an offset/limit range) and for `grep` in
  `output_mode="content"` (one event per distinct skill file with at least one returned line, match or context).
- **FR-036**: The event MUST carry `skill` (the skill name), `path` (the resolved absolute path as a string, as
  today), `via` (the tool name, `"read_file"` or `"grep"`) and `call_id` (the model's tool-call id, equal to the same
  call's `tool.call.call_id`; `None` when the tool runs outside a runner).
- **FR-037**: Exactly one `skill.read` per (tool call, skill file); each is emitted before that call's `tool.call`
  event.
- **FR-038**: No `skill.read` MUST be emitted for: a failed call (not found, blocked, too large, any error result);
  a call denied by the capability gate; `grep` in `files_with_matches` or `count` mode; files that are not skill files
  (another file in a skill directory, a `SKILL.md` outside a skills root); fingerprint computation; the skills summary
  and always-on loading (those are `skill.listed`).
- **FR-039**: Reads through `exec`, `exec_session`, MCP tools and other tools are not covered; `EVENTS["skill.read"]`
  MUST say the event is a lower bound for those. [NEEDS CLARIFICATION: also infer `skill.read` from shell commands
  that name a `SKILL.md` (heuristic, marked `inferred=True`)? Recommended default: no; shell reads stay invisible and
  the lower bound is stated, because a heuristic would claim more than the layer can see (constitution II).]

#### Cross-cutting

- **FR-040**: `moeka.trace.EVENTS["tool.call"]` MUST mention `args_digest`, and `EVENTS["skill.read"]` MUST mention
  `via` and `call_id`.
- **FR-041**: Public exports MUST include `moeka.trace.args_digest`, `moeka.errors.VariantError`, and the new fields
  `AgentSpec.on_unsupported`, `Variant.tool_param_descriptions`, `ModelSpec.unsupported_sampling`.
- **FR-042**: The import boundary (`tests/core/test_import_boundary.py`) and the ambient-read guard
  (`tests/kernel/test_no_ambient_reads.py`) MUST pass with no new allow-list entry; nothing in this spec reads the
  process environment or home directory.
- **FR-043**: The repo suite (`scripts/test-docker.sh`) and the accepted held-out suite `tests/heldout/005-multi-
  instance` MUST stay green; the one existing test that expects `NotImplementedError` for `max_tool_errors` is
  replaced by tests of FR-020 to FR-026.

### Key Entities

- **Fingerprint**: digest plus five component hashes of what the model sees and can read.
- **Variant**: per-kernel text overrides, now including parameter descriptions.
- **RunLimits / AgentSpec**: per-run ceilings and strictness.
- **ModelSpec**: the host's model declaration, now including fields the backend ignores.
- **Trace events**: `tool.call` (with `args_digest`), `skill.read` (with `via`, `call_id`), `run.completed`,
  `sampling.dropped`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Each of K1-K6 has a failing-then-passing test in the kernel suite and a passing held-out group (`004`
  SC-001); the held-out suite for this spec passes 100%.
- **SC-002**: Changing one byte of one non-always-on `SKILL.md` changes `fingerprint().digest`; copying the tree to
  another directory changes nothing (harness M4 verification).
- **SC-003**: In a scripted run with calls `{"q":"x"}`, `{"q": "x"}` (string form), `{"q":"x"}` and `{"q":"y"}`,
  the `tool.call` events carry exactly 2 distinct `args_digest` values.
- **SC-004**: With `max_tool_errors=3` and a model that only ever calls an unknown tool, exactly 3 provider calls are
  made; with `max_tool_errors=None`, the same script never ends with `tool_error`.
- **SC-005**: With `on_unsupported="raise"` and an unsupported `seed`, 0 provider calls are made; with `"drop"`, 1
  call and 1 `sampling.dropped`.
- **SC-006**: A `grep` over a skills tree with k matching skill files yields exactly k `skill.read` events, each
  joined to its `tool.call` by `call_id`.
- **SC-007**: `scripts/test-docker.sh` exits 0 and `scripts/run-heldout.sh 005-multi-instance` passes on the
  implementation commit.

## Assumptions

- The harness drives agents only through `moeka` public APIs (`004` FR-007); these prerequisites add no
  harness-only private hook.
- Every existing fingerprint digest changes once (the new component); nothing persists digests yet (the harness is
  not built), so no migration or version field is needed.
- Kernel invariants I1-I6 hold unchanged; K4 strengthens I5.
- The legacy gateway loop is not changed in behaviour: its defaults (`max_tool_errors=None`, `on_unsupported="drop"`)
  are today's behaviour.

## Test Harness Contract

Hidden tests MUST use only these surfaces, MUST run offline (no network, no real model), and MUST NOT touch the real
`$HOME` or the live checkout.

### Python APIs

- `from moeka.agents import Agent, AgentSpec, RunLimits, RunResult, ToolInfo, StopReason`;
  `from moeka.variants import Variant, Fingerprint`; `from moeka.errors import UnsupportedRequestError,
  VariantError, LLMError`; `from moeka.trace import MemoryTraceSink, EVENTS, args_digest`;
  `from moeka.testing import FakeProvider, reply, error`.
- `from moeka import Environment, Kernel, ModelSpec, ProviderSpec, Sampling`; `from moeka.llm import
  GenerateOptions`.
- `Environment.for_host(state_dir=, work_dir=, credentials=, providers=, models=, default_model=, data_dir=None,
  trace=<MemoryTraceSink>, ...)`; `Kernel(env, variant=Variant(...))`; `kernel.llm.register_provider(alias, fake,
  ModelSpec(...))`; `kernel.llm.complete(...)` with `GenerateOptions(sampling=, on_unsupported=)`.
- `kernel.agent(spec)` -> `Agent`: `.tools() -> list[ToolInfo]`, `.fingerprint() -> Fingerprint`,
  `.run_sync(message, *, session=None, sampling=None, ...) -> RunResult`, `await .run(...)`, `.stream_sync(...)`.
- Scripted tool calls: `nanobot.providers.base.LLMResponse(content="", tool_calls=[ToolCallRequest(id=, name=,
  arguments=)], finish_reason="tool_calls", usage=LLMUsage.reported(...))`. `FakeProvider.calls` records each
  provider call (`kwargs["tools"]` holds the tool definitions). `FakeProvider.supported_sampling_fields` may be set
  per instance to narrow declared support.
- `AgentSpec(..., tools_allow=(...), skills_include=, skills_exclude=, inline_skills=, sampling=, limits=,
  on_unsupported=)`; `RunLimits(max_iterations=, max_policy_denials=, max_tool_errors=)`.

### Trace events (observed on the `trace=` sink)

- `tool.call`: `session_key, iteration, tool, call_id, ok, args_valid, error_kind, error, duration_ms, args_digest`.
- `skill.read`: `skill, path, via, call_id`.
- `run.completed`: `stop_reason` and the existing keys; `sampling.dropped`: `provider, model, fields`.

### File locations

- Built-in skills: `<Variant.builtin_skills_dir>/<name>/SKILL.md` plus supporting files (tests create these in temp
  directories).
- Workspace skills: `<work_dir>/skills/<name>/SKILL.md`.
- No environment variables and no CLI are part of this contract.
