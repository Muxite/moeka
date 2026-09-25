# 01 - Agent loop: turn machinery, prompt assembly, limits

Audience: a small LLM that will edit ONLY skills, prompts (templates), tool
descriptions/schemas and config. Everything below was read from source on the
`core-slim` branch.
Citation format is `path:line`, relative to `nanobot/` unless the path starts with
`tests/`, `docs/` or `.agent/` (those are relative to the repo root). Line numbers were
re-verified against the slim tree; if a cited line no longer matches, search for the
quoted identifier. Anything not read is marked **unverified**.

---

## 1. Overview and data flow

The slim core has no chat channels. A caller reaches the agent in one of two ways:

- `AgentLoop.process_direct(...)` (agent/loop.py:2438): used by `MoekaCore.run`
  (core/core.py:642), the CLI single-message mode (cli/agent.py:195) and Dream
  (agent/dream.py:65). It takes the per-session lock and calls `_process_message`
  directly (agent/loop.py:2469-2496).
- `MessageBus`: the caller publishes an `InboundMessage` and `AgentLoop.run()` consumes it
  (used by the interactive CLI, cli/agent.py:252). `run()` starts one asyncio task per message
  (`_dispatch`). Messages for the same session run one at a time; a message that arrives while
  a turn is active is routed into that turn's pending queue and injected mid-turn.

Each turn goes through fixed stages (restore, compact, command, build, run, save, respond).
In the `run` stage `AgentRunner` loops: build the provider request (system prompt + history +
current message), call the LLM, and if the reply has tool calls execute them, append results,
and call the LLM again, until the model answers without tool calls, or `max_tool_iterations`
is hit. The final text becomes an `OutboundMessage` (returned by `process_direct`, or published
on the bus).

```
InboundMessage -> bus.inbound            bus/queue.py:37,41 ; bus/events.py:25
  AgentLoop.run()  consume_inbound       agent/loop.py:1323,1331
    routing: priority cmd / pending-queue injection / new task      agent/loop.py:1359-1439
  _dispatch(msg)  session lock, pending queue (maxsize 20)             agent/loop.py:1453,1480-1484
    _process_message(msg)  builds TurnContext, runs stages             agent/loop.py:1662,1747-1754
      restore  -> _restore_turn         session, tool restrictions      agent/loop.py:1816
      compact  -> _compact_session      AutoCompact.prepare_session     agent/loop.py:1873
      command  -> _dispatch_command     slash commands short-circuit    agent/loop.py:1881
      build    -> _build_turn           history, runtime-context blocks agent/loop.py:1933
      run      -> _run_turn -> _run_agent_loop -> AgentRunner.run()      agent/loop.py:2042,992,1231
          per iteration (runner._run_core, agent/runner.py:478):
            request_messages -> ContextBuilder.build_transcript          agent/context.py:477
            provider.chat_stream_with_retry(...)                         agent/runner.py:1096,1105
            tool calls? -> _execute_tools -> _run_tool -> tool.execute   agent/runner.py:530,570,1458,1530
            append tool results, loop again                              agent/runner.py:589-690
            no tool calls -> final content, break                        agent/runner.py:867-900
      save     -> _persist_turn -> _save_turn                           agent/loop.py:2075,2260
      respond  -> _prepare_outbound                                     agent/loop.py:2124
  -> OutboundMessage via delivery.complete                             agent/loop.py:1497
```

Design constraints that apply to editors (`.agent/design.md`): core files
`agent/loop.py` and `agent/runner.py` are the critical path and should change
minimally; new capability belongs in tools, skills or MCP servers;
configuration must be declared in `config/schema.py`; prefer a smaller prompt or
tighter tool contract over new code. `.agent/gotchas.md`: never run `ruff format`;
template/skill/tool-description changes alter behaviour as directly as code; text
written into memory/history is replayed into future prompts, so keep it bounded
and free of internal markers, local paths and tool-call echoes.

---

## 2. Per-file reference

### 2.1 `agent/loop.py` (2508 lines) - orchestration

Purpose: owns sessions, routing, turn stages, persistence, and wiring
(tools, hooks, subagents, autocompact). Contains no LLM iteration logic; that is in
the runner.

Key items:
- `TurnKind` USER / SYSTEM (agent/loop.py:125). `TurnContext` dataclass carrying one turn's state
  (msg, session, history, `transcript_input`, `final_content`, `stop_reason`, `suppress_response`,
  `pending_queue`, hooks, tools...) (agent/loop.py:130-191).
- `AgentLoop.__init__(...)` (agent/loop.py:267). `max_iterations` falls back to
  `AgentDefaults().max_tool_iterations` (agent/loop.py:334-336). It builds `ContextBuilder` (agent/loop.py:393),
  `SubagentManager` (agent/loop.py:439), `Consolidator` (agent/loop.py:476), `AutoCompact` (agent/loop.py:486),
  `CommandRouter` (agent/loop.py:497).
- `AgentLoop.from_config(config, ...)` maps config keys onto constructor args (agent/loop.py:500-573):
  `max_tool_iterations`, `max_concurrent_subagents`, `context_window_tokens`, `max_tool_result_chars`,
  `provider_retry_mode`, `tool_hint_max_length`, `disabled_skills`, `allowed_skills`, `tools_allow`,
  `tools_deny`, `planning`, `limits` (as `runner_limits`), `session_ttl_minutes`,
  `idle_compact_check_interval_seconds`, `dream.model_override` (as `dream_model_preset`).
- `run_dream()` (agent/loop.py:256-260) runs one Dream pass via `agent/dream.py:run_dream`. Core has no
  scheduler; see doc 03 section 4.3.
- `_register_default_tools` (agent/loop.py:666): uses `ToolLoader().load(ctx, self.tools, allow=tools_allow, deny=tools_deny)`
  (agent/loop.py:688-689); the `my` tool is registered manually when `tools.my.enable` (agent/loop.py:694-704).
- `run()` main loop (agent/loop.py:1323): waits 1 s for inbound (agent/loop.py:1331); on timeout checks idle
  sessions (agent/loop.py:1333). Priority commands are handled immediately (agent/loop.py:1359-1364).
  Non-command follow-ups for a session with an active turn go to `self._pending_queues[key]`
  (agent/loop.py:1401-1435). The loop over `self._automation_turn_coordinators` (agent/loop.py:1366-1380)
  is dead scaffolding: the tuple is always empty in the slim core (agent/loop.py:468-470).
- `_dispatch` (agent/loop.py:1453): per-session `asyncio.Lock` (agent/loop.py:1473,1480; lock factory
  agent/loop.py:2502), pending queue `maxsize=20` (agent/loop.py:1483). On exit, leftover queued messages are
  re-published to the bus (agent/loop.py:1552-1575). Exceptions in a turn are logged and the turn is failed,
  not crashed (agent/loop.py:1542-1550). Cancellation restores a checkpoint (agent/loop.py:1504-1541).
- `_process_message` (agent/loop.py:1662): the seven stages listed in section 1.
- `_build_turn` (agent/loop.py:1933): loads `session.get_history(...)` (agent/loop.py:1955), resolves
  runtime-context blocks for user turns (agent/loop.py:1979-1980), persists the user message early
  (agent/loop.py:2027-2032), then `ctx.transcript_input = self._build_transcript_input(ctx)`
  (agent/loop.py:2039; def at agent/loop.py:773).
- `_run_agent_loop` (agent/loop.py:992): builds `transcript_builder = partial(self.context.build_transcript, ...)`
  (agent/loop.py:1173-1184), builds the hook chain (agent/loop.py:1213-1230), and calls
  `self.runner.run(AgentRunSpec(...))` (agent/loop.py:1231-1281). Notable spec fields:
  `max_iterations=self.max_iterations` (agent/loop.py:1235), `concurrent_tools=True` (agent/loop.py:1240),
  `limits=self.runner_limits` (agent/loop.py:1266), `continuation_callback=_goal_continue` (agent/loop.py:1268),
  `finalize_on_max_iterations=...` (agent/loop.py:1269). `include_memory_recent_history=not ephemeral`
  (agent/loop.py:1181).
- `_maybe_plan` (agent/loop.py:2189): only when `planning` is true (agent/loop.py:1208-1209). One extra non-tool
  LLM call (max_tokens 600, temperature 0.2, 90 s timeout) if the user text is at least 80 chars
  (agent/loop.py:2183,2213-2226); the plan is appended to `transcript_input.history` as a user message that
  starts with `[Planning note ...]` (agent/loop.py:2184-2187,2232-2234). The planning system prompt is a
  Python string (agent/loop.py:2178-2182), i.e. code, not an editable template.
- `_run_turn` (agent/loop.py:2042): runs the agent loop, copies the result into the turn context, then
  `turn_continuation.maybe_continue_turn(ctx)` for user turns (agent/loop.py:2072-2073).
- `_persist_turn` (agent/loop.py:2075): if a user turn ends with empty `final_content` and is not suppressed,
  it is replaced by `EMPTY_FINAL_RESPONSE_MESSAGE` (agent/loop.py:2079-2084). `_save_turn` (agent/loop.py:2260)
  drops empty assistant messages (agent/loop.py:2331-2332), drops tool results whose `tool_call_id` was not
  declared or is already fulfilled (agent/loop.py:2333-2347), and stamps timestamps (agent/loop.py:2370).

Gotchas: `AgentLoop.__init__` has moeka-only params `bootstrap_overrides` and `inline_skills`
(agent/loop.py:313-314) that `MoekaCore` depends on; do not remove. The `dream:` session-key prefix is treated
as internal (agent/loop.py:1939; agent/autocompact.py:27). Comments at agent/loop.py:1387-1390 and
agent/loop.py:1516-1519 still name the removed WebUI/`RecoveryCoordinator`; the `recovery_admission` path they
guard is only active when a caller passes `recovery_admission=` (agent/loop.py:315,322).

### 2.2 `agent/runner.py` (1811 lines) - the LLM/tool iteration loop

Purpose: "Run a tool-capable LLM loop without product-layer concerns" (agent/runner.py:181).

- `RunnerLimits` (pydantic, camelCase aliases) (agent/runner.py:106-123). Fields and defaults in section 4.
- `AgentRunSpec` (agent/runner.py:127-154): everything one run needs (tools, runtime, `max_iterations`,
  `max_tool_result_chars`, `transcript_input`+`transcript_builder` OR `initial_messages`, hook, callbacks,
  `limits`, `finalize_on_max_iterations`).
- `AgentRunResult` (agent/runner.py:158-177): `final_content`, `messages`, `tools_used`, `usage`, `stop_reason`,
  `error`, ...
- `AgentRunner.run` (agent/runner.py:349): wraps `_run_core` with hook `before_run` / `on_error` / `after_run`
  / `on_finally`. Exactly one of `transcript_input` or `initial_messages` must be given
  (agent/runner.py:403-425).
- `_run_core` (agent/runner.py:427): the loop `for iteration in range(spec.max_iterations)` (agent/runner.py:478).
  Per iteration:
  1. `hook.before_iteration` (agent/runner.py:484), `_request_model` (agent/runner.py:491) which does
     prepare/governance, streaming and the provider call (agent/runner.py:976-1186).
  2. Reasoning is extracted from `<think>`-style tags and native fields (agent/runner.py:510-528).
  3. If `response.should_execute_tools` (agent/runner.py:530): truncate the call list at the first `ask_user`
     (agent/runner.py:532-534); append the assistant message (agent/runner.py:539-549); checkpoint
     (agent/runner.py:550); `hook.before_execute_tools` (agent/runner.py:562); `_execute_tools`
     (agent/runner.py:570); append one `role:"tool"` message per call, content passed through
     `context_governor.normalize_tool_result` (agent/runner.py:589-604); fatal error handling
     (agent/runner.py:605-631); failure-reflection (agent/runner.py:658-680); drain injections
     (agent/runner.py:683-686); `continue` (agent/runner.py:690).
  4. Otherwise treat as a final answer: empty-response retries (agent/runner.py:699-740), length recovery
     (agent/runner.py:742-767), injection/continuation drain (agent/runner.py:798-824), error branch
     (agent/runner.py:826-847), empty branch (agent/runner.py:848-865), success (agent/runner.py:867-900).
  5. If the `for` loop is exhausted without `break`: `stop_reason="max_iterations"` and a final no-tools
     call (agent/runner.py:901-934).
- `_execute_tools` (agent/runner.py:1458) partitions calls into batches; with `concurrent_tools` (True for the
  main loop) only tools with `concurrency_safe` run in parallel, everything else runs alone
  (agent/runner.py:1788-1811).
- `_run_tool` (agent/runner.py:1530) is the single place where tool errors are shaped, see section 5.
- Runner never edits the persisted session; `AgentLoop._save_turn` does.

Invariants:
- Every tool call gets a `role:"tool"` result (except `ask_user`, which interrupts) (agent/runner.py:590-591).
- Tool-result text is what the model sees next. Error results get the hint
  `"\n\n[Analyze the error above and try a different approach.]"` appended
  (agent/runner.py:1542,1581,1615,1633).
- `ask_user` ends the turn: the question becomes `final_content`, `stop_reason="ask_user"`
  (agent/runner.py:606-614).

### 2.3 `agent/context.py` (571 lines) - system prompt and message construction

See section 3 for the full assembly. Classes: `ContextBuilder` (agent/context.py:99), `TranscriptInput`
(agent/context.py:83-96), `PersistedPromptContextResolver` (agent/context.py:62-80).
- `BOOTSTRAP_FILES = ["AGENTS.md","SOUL.md","USER.md"]` (agent/context.py:102). `_MAX_HISTORY_TOKENS = 8_000`
  (agent/context.py:105).
- `build_system_prompt` (agent/context.py:133), `_build_history_section` (agent/context.py:208),
  `_behavioral_guidelines` (agent/context.py:296), `_get_identity` (agent/context.py:325),
  `_load_bootstrap_files` (agent/context.py:365), `build_transcript` (agent/context.py:477),
  `build_current_message` (agent/context.py:518), `build_user_content` (agent/context.py:542).
- `build_transcript` returns `[system, *history, current_user_message]` (agent/context.py:490-516).

### 2.4 `agent/hook.py` (310 lines) and `agent/turn_hooks.py` (88 lines) - lifecycle hooks

- `AgentHook` base class with no-op async methods: `before_run`, `after_run`, `on_error`, `on_finally`,
  `before_iteration`, `on_stream`, `on_stream_end`, `before_execute_tools`, `before_execute_tool`,
  `after_execute_tool`, `on_execute_tool_error`, `emit_reasoning`, `after_iteration`, and the sync
  `finalize_content(context, content)` (agent/hook.py:66-151).
- `CompositeHook` fans out to hooks in order; each hook's exception is caught and logged unless the hook was
  built with `reraise=True` (agent/hook.py:157-183). `finalize_content` is a pipeline with no isolation
  (agent/hook.py:268-271).
- `AgentHookContext` (per iteration), `AgentRunHookContext` (per run), `AgentTurnHookContext` (per turn
  construction inputs) (agent/hook.py:17-63). `SDKCaptureHook` records tools used/messages for SDK results
  (agent/hook.py:274-310).
- `build_agent_turn_hook(spec)` (agent/turn_hooks.py:43) order: progress hook first, then registered factories,
  registered hooks, per-turn factories, per-turn hooks (agent/turn_hooks.py:65-88). Factory exceptions are
  logged and skipped (agent/turn_hooks.py:67-75). Ephemeral turns get only the progress hook unless
  `run_extra_hooks_for_ephemeral` (agent/turn_hooks.py:51-52).
- Editors cannot add hooks from prompts/skills/config; hooks are Python (`AgentLoop(hooks=...)`,
  agent/loop.py:284,382, or `MoekaCore.run`, core/core.py:638-646). **Unverified**: whether any config key
  registers hooks (none seen in `config/schema.py`).

### 2.5 `agent/autocompact.py` (134 lines) - idle-session compaction

- `AutoCompact(sessions, consolidator, session_ttl_minutes=0, bind_events=None)` (agent/autocompact.py:29).
- `check_expired` (agent/autocompact.py:68): for sessions idle at least TTL minutes with unarchived messages,
  and not currently running a turn, schedules `consolidator.compact_idle_session` in the background
  (agent/autocompact.py:93-114). `dream:*` sessions are skipped (agent/autocompact.py:27,66,78).
- `prepare_session(session, key)` (agent/autocompact.py:116): called in the `compact` stage (agent/loop.py:1875)
  and in build (agent/loop.py:1948); returns `(session, SessionSummary|None)`. The summary is later appended to
  the system prompt as `[Archived Context Summary]` (agent/context.py:199-204).
- `_ttl <= 0` disables it (agent/autocompact.py:41). The config default is 15 minutes (`session_ttl_minutes`,
  alias `idleCompactAfterMinutes`, config/schema.py:149-154); scan interval default 60 s
  (config/schema.py:155-158; agent/loop.py:1311-1321). Idle scans only run inside `AgentLoop.run()`
  (agent/loop.py:1332-1333), so a `process_direct`-only caller (MoekaCore) never auto-compacts idle sessions.

### 2.6 `agent/subagent.py` (596 lines) - background/inline subagents

- `SubagentManager.spawn` (agent/subagent.py:236) starts a background task and returns immediately with
  "Subagent [...] started (id: ...)". `run_inline` (agent/subagent.py:300) waits and returns the result.
- Concurrency limited by `_run_slots` (agent/subagent.py:378); default `max_concurrent_subagents` = 4
  (config/schema.py:132).
- The subagent runs `AgentRunner` with `initial_messages=[system, user(task)]`, the same `max_iterations` as
  the main loop, `finalize_on_max_iterations=False`, `max_iterations_message="Task completed but no final
  response was generated."` (agent/subagent.py:436-453).
- Subagent tools come from `ToolLoader().load(..., scope="subagent", allow=tools_allow, deny=tools_deny)`
  (agent/subagent.py:230-233); its config only carries `exec`, `web`, `file`, `restrict_to_workspace`
  (agent/subagent.py:202-209).
- System prompt: `templates/agent/subagent_system.md` rendered by `_build_subagent_prompt`
  (agent/subagent.py:542-564). It contains no bootstrap files and no memory, only the workspace, history log
  path and skills summary.
- Result is published back on the bus as a `channel="system"`, `sender_id="subagent"` inbound message
  rendered from `templates/agent/subagent_announce.md` (agent/subagent.py:510-539). If the parent turn is still
  running it is injected into that turn; `_wait_for_pending` blocks up to 300 s for running subagents before
  the parent turn ends (agent/loop.py:122,1126-1158).

### 2.7 Goal/continuation: `session/goal_state.py`, `session/turn_continuation.py`, `agent/goal_permission.py`

- Goal state lives in session metadata under `"goal_state"` (legacy key `"thread_goal"`)
  (session/goal_state.py:13,17,21-26). Statuses seen: `active`, `blocked`, plus terminal ones written by the tool
  (session/goal_state.py:42,99). `MAX_GOAL_OBJECTIVE_CHARS = 4000` (session/goal_state.py:15).
- `goal_state_runtime_lines(metadata)` returns `["Goal (active):", objective, "Summary: ..."]` for the runtime
  context block (session/goal_state.py:77-93).
- `explicit_goal_requested` is true when message metadata has `goal_requested` or `original_command == "/goal"`
  (session/goal_state.py:45-51). The `/goal` command sets that and adds `goal_mutation_permission(True)` to the
  turn scopes (command/builtin.py:791-829).
- `goal_permission.py`: a `ContextVar` `goal_mutation_allowed()`; `create_goal` and `update_goal(action=replace)`
  are refused unless it is true (agent/tools/long_task.py:73-74,206,323); it is revoked after a goal ends
  (agent/tools/long_task.py:364). So the model cannot start a goal on its own; only a user `/goal` command
  grants it.
- Budget-boundary continuation (`session/turn_continuation.py`): when a turn stops with
  `stop_reason == "max_iterations"` AND a goal is active AND a pending queue exists AND fewer than
  `_MAX_GOAL_CONTINUATION_ROUNDS = 12` rounds were used (session/turn_continuation.py:33,158-170,200-214),
  `maybe_continue_turn` queues a new internal inbound message from sender `system:continuation` whose text is
  built in `_goal_continuation_prompt` (session/turn_continuation.py:107-142,240-257), suppresses the visible
  response and strips the synthetic last assistant message (session/turn_continuation.py:129-131,260-274).
  In that case the runner does NOT spend the extra no-tools finalization call
  (`should_finalize_on_max_iterations`, session/turn_continuation.py:86-104; agent/loop.py:1269).
  A pending queue exists only for bus-driven turns (`_dispatch`), so `process_direct` callers never get this
  continuation.
- In-run goal nudge: when the model produces a final answer while a goal is active, the runner asks the loop's
  `_goal_continue()` for a user message and injects it, so the model keeps working
  (agent/loop.py:1196-1205; agent/runner.py:806-807 `allow_continuation`; agent/runner.py:265-276). The text is
  a Python string (code), not a template. **Caveat, derived from reading, not from a test:** `allow_continuation`
  is only False for `refusal`/`content_filter`, so with an active goal, an answer without
  `update_goal(complete/cancel/block)` keeps being followed by this nudge until the iteration budget is spent.
- Goal guidance shown to the model is the template `templates/agent/goal_runtime.md` (editable), injected as a
  runtime-context block by the goal tool's provider (agent/tools/long_task.py:170-193).

### 2.8 `bus/` (only what is needed)

- `MessageBus` has `inbound` and `outbound` `asyncio.Queue`s, `publish_inbound`/`consume_inbound`
  (bus/queue.py:31-45). `InboundMessage` fields: `channel, sender_id, chat_id, content, media, metadata,
  session_key_override, require_existing_session, input_role` (bus/events.py:25-38). `session_key` = override or
  `"{channel}:{chat_id}"` (bus/events.py:40-42). `is_user_input` is False for `channel == "system"` unless
  `input_role` says otherwise (bus/events.py:45-50). System messages (subagent results, continuations) therefore
  use `TurnKind.SYSTEM`. In the slim core `channel` is just a routing label (`cli`, `core`, `system`, ...);
  there are no channel adapters.

---

## 3. How the system prompt and context are assembled

Entry: `ContextBuilder.build_system_prompt` (agent/context.py:133), called by `build_transcript`
(agent/context.py:493) for the first request and rebuilt whenever the runner rebuilds the transcript
(`transcript_builder`). Parts are joined with `"\n\n---\n\n"` (agent/context.py:206). In order:

| # | Section in prompt | Origin (file) | Editable content or code? |
|---|---|---|---|
| 1 | Identity: `## Runtime` (OS, Python version), `## Workspace` (paths to SOUL.md, USER.md, memory/MEMORY.md, history.jsonl, skills dir), platform policy, `## Format Hint` (only when `channel == "cli"`), `## External Content` | `templates/agent/identity.md` rendered at agent/context.py:333-340; includes `templates/agent/platform_policy.md` and `templates/agent/_snippets/untrusted_content.md`; runtime string built in Python at agent/context.py:330-331 | Template = editable text. Variables (`runtime`, paths, `channel`) come from code. |
| 2 | Bootstrap files, each as `## <filename>` then content: `AGENTS.md` (from project root/workspace), `SOUL.md`, `USER.md` (both from the agent workspace, default `~/.nanobot`) | Files on disk in the workspace; fallback bundled `templates/SOUL.md` if SOUL.md equals the legacy template (agent/context.py:397-401); `AGENTS.md`/`USER.md` are skipped if identical to the bundled template (agent/context.py:404-407); empty files skipped (agent/context.py:402-403). In-memory `bootstrap_overrides` shadow a same-named file (agent/context.py:388-392) and unknown names are appended (agent/context.py:410-414) | Files are editable content (workspace files, outside the source tree). |
| 3 | `# Working style` (long-running-job etiquette via `exec` `yield_time_ms` + `exec_session`, "default to short") | `ContextBuilder._behavioral_guidelines`, hard-coded string (agent/context.py:295-324) | CODE (Python string). It names only registered tools (`exec`, `exec_session`) and does not promise automatic wake-ups; a test pins that `bg_shell` is absent (tests/agent/test_context_builder.py). |
| 4 | `# Tool Usage Notes` (general tool contract, discovery, file workflow, exec, web, media) | `templates/agent/tool_contract.md`, rendered at agent/context.py:154 | Editable template. |
| 5 | `# Current Project` (only when the effective workspace differs from the agent workspace) | Python f-string, agent/context.py:156-162 | CODE. |
| 6 | `# Memory` -> `## Long-term Memory` from `memory/MEMORY.md` | `MemoryStore.get_memory_context` (agent/memory.py:265); may be semantically trimmed only if VecStore is available and file > threshold (default 2048 chars, `top_k` 10) (agent/context.py:164-174; agent/memory.py:288-297). Skipped if identical to `templates/memory/MEMORY.md` (agent/context.py:173) | Data file, written by Dream/consolidation. The template says only Dream tasks may edit it (templates/agent/identity.md:18). |
| 7 | `# Active Skills`: full bodies (frontmatter stripped) of skills with `always: true` | `SkillsLoader.get_always_skills` / `load_skills_for_context` (agent/skills.py:391-401,178-193); `always` is read from top-level frontmatter or from `metadata` | Skill files. No bundled skill in `nanobot/skills/*/SKILL.md` has an `always` key (grep). |
| 8 | `# Skills` summary: one line per skill (`- **name** - description  \`path\``), grouped Inline / Workspace / Agent Plugin / Built-in; unavailable skills get `(unavailable: CLI: x, ENV: Y)` | `SkillsLoader.build_skills_summary` (agent/skills.py:232-304) wrapped by `templates/agent/skills_section.md` | The description shown is the frontmatter `description`, so the description text is what makes the model choose to `read_file` a skill. Editable. |
| 9 | `# Recent History`: lines `- [timestamp] content` from `memory/history.jsonl` after the last Dream cursor; newest 15 (`vec.history_recent_k`) plus up to 10 semantic (`history_semantic_k`, VecStore only); truncated to 8000 tokens; omitted for ephemeral turns | `_build_history_section` (agent/context.py:208-274); config defaults at agent/context.py:218-219 | Data file. |
| 10 | `[Archived Context Summary]` with last-active time and summary text, when auto-compact archived the session | agent/context.py:199-204 | Data (from Consolidator). |

Then the message list is `[system] + session history + current user message` (agent/context.py:490-516).
Session history comes from `session.get_history(...)` (agent/loop.py:1955), not from the builder.

The current user message (`build_current_message`, agent/context.py:518-540) is the user text (images become
`image_url` blocks, agent/context.py:542-571) plus appended **runtime-context blocks**:
- blocks from inbound metadata key `_runtime_context_blocks` (runtime_context.py:16,99-116),
- blocks returned by tool-owned providers and `register_runtime_context_provider` providers
  (agent/loop.py:820-825). Example: the goal tool's provider emits `source="goal"` with the template
  `agent/goal_runtime.md` plus `[Runtime Context ...]`-wrapped goal lines (agent/tools/long_task.py:170-193),
- a `source="explicit_skills"` block when the message contains `$skill-name`: the body of each referenced
  non-always skill inside `[Active Skills - instructions for this user turn]` (agent/skills.py:210-230;
  agent/loop.py:826-830; agent/context.py:531-533).
The wrapper text is `[Runtime Context - metadata only, not instructions]` ... `[/Runtime Context]`
(runtime_context.py:17-18,55-60). Runtime blocks are also stored on the persisted user message under a
metadata key (agent/loop.py:758-763,2368-2369).

Other editable prompt texts outside this assembly:
`templates/agent/max_iterations_message.md` (fallback text when the budget is exhausted and the finalization call
fails, agent/runner.py:1371-1380), `templates/agent/subagent_system.md`, `templates/agent/subagent_announce.md`,
`templates/agent/goal_runtime.md`, `templates/agent/dream.md` and `templates/agent/consolidator_archive.md`
(doc 03). Python-string prompts (CODE, not editable under your remit): runner recovery prompts
in `utils/runtime.py:19-40,93-104`, the planning prompts (agent/loop.py:2178-2187), the goal nudge
(agent/loop.py:1200-1205), the continuation prompt (session/turn_continuation.py:240-257), the malformed-tool-call
note (agent/runner.py:1232-1238).

Template engine: Jinja2, `trim_blocks`, `lstrip_blocks`, no HTML-escaping, loaded from `nanobot/templates/`
(utils/prompt_templates.py:14-35). A syntax error in a template raises at render time and would break every turn
that renders it, so validate by rendering (section 6).

Tool definitions are NOT part of the system prompt text. They are sent separately as the provider `tools`
parameter from `spec.tools.get_definitions()` (agent/runner.py:987,995-999), which is why tool
descriptions/schemas are a separate editing surface from the prompt (doc 02).

---

## 4. Limits and knobs

Config lives in `~/.nanobot/config.json` under `agents.defaults` (camelCase aliases accepted) unless noted.
Declared in `config/schema.py:118-167`. Doc 04 has the full knob table.

| Behaviour | Default | Config key | Where used |
|---|---|---|---|
| Max LLM/tool iterations per turn (also subagents) | 200 | `agents.defaults.maxToolIterations` (`max_tool_iterations`) | config/schema.py:131; agent/loop.py:334-336,1235; agent/subagent.py:146-149,440 |
| Tool-result size cap before offload/truncation | 16000 chars | `maxToolResultChars` | config/schema.py:133; agent/runner.py:467; agent/context_governance.py:709-760 (`read_file` exempt, agent/context_governance.py:68) |
| Context window used for budgeting | 200000 tokens | `contextWindowTokens` (or preset) | config/schema.py:128; agent/runner.py:468 |
| Output tokens reserved per call | 8192 | `maxTokens` | config/schema.py:127; input budget = window - max_output - 1024 (agent/context_governance.py:693-707; `SNIP_SAFETY_BUFFER` at agent/context_governance.py:67) |
| Temperature | 0.1 | `temperature` | config/schema.py:129 |
| Provider retry mode | `standard` (or `persistent`) | `providerRetryMode` | config/schema.py:134; agent/runner.py:968 |
| Empty-response retries | 2 | `agents.defaults.limits.maxEmptyRetries` | agent/runner.py:82,115; check at agent/runner.py:705-706 |
| Length-truncation continuations | 3 | `limits.maxLengthRecoveries` | agent/runner.py:83,116,743 |
| Injected follow-ups drained per drain | 3 | `limits.maxInjectionsPerTurn` | agent/runner.py:84,117,329-336 |
| Injection cycles per run | 5 | `limits.maxInjectionCycles` | agent/runner.py:85,118,217. Note: the terminal-wait path compares against the module constant `_MAX_INJECTION_CYCLES`, not the config value (agent/runner.py:224-228) |
| Tool-failure reflection threshold | 3 consecutive all-failed iterations; 0 disables | `limits.toolFailureReflectionThreshold` | agent/runner.py:123,660-680 |
| Microcompact keep/min chars | 10 / 500 | `limits.microcompactKeepRecent`, `limits.microcompactMinChars` | **No effect (verified).** The fields are declared at agent/runner.py:119-120 and nothing in `nanobot/` reads them: `grep -rn microcompact nanobot/` hits only agent/runner.py:86,119-120, and the comment at agent/runner.py:86-91 says `ContextGovernor` uses its own constants |
| Repeat-lookup throttle | 2 free repeats per identical `web_fetch` URL / `web_search` query per turn | code constant | utils/runtime.py:13,123-144 |
| Repeat boundary/exec-guard throttle | third same-target violation escalates | code constant | utils/runtime.py:16,187-215,240-260 |
| Idle compaction TTL | 15 min (0 = off) | `idleCompactAfterMinutes` / `sessionTtlMinutes` | config/schema.py:149-154 |
| Idle scan interval | 60 s | `idleCompactCheckIntervalSeconds` | config/schema.py:155-158 |
| Concurrent subagents | 4 | `maxConcurrentSubagents` | config/schema.py:132 |
| Goal continuation rounds | 12 | code constant | session/turn_continuation.py:33 |
| Wait for running subagents before ending turn | 300 s | code constant | agent/loop.py:122 |
| Pending-queue size per session | 20 | code constant | agent/loop.py:1483 |
| Concurrent turns across sessions | unlimited unless env `NANOBOT_MAX_CONCURRENT_REQUESTS` > 0 | env var | agent/loop.py:471-475 |
| Planning pre-step | off; min user text 80 chars | `planning` | config/schema.py:164; agent/loop.py:1208,2183 |
| Skill filtering | all skills | `disabledSkills`, `allowedSkills` (None = all, list = only those) | config/schema.py:148,163; agent/skills.py:139-149 |
| Tool filtering | all tools | `toolsAllow` (None = all, `[]` = none), `toolsDeny` | config/schema.py:161-162; agent/loop.py:688-704 |

Context governance (`agent/context_governance.py`, `ContextGovernor`): before every provider call
`prepare_request` (agent/context_governance.py:593) (a) normalizes the transcript copy: removes placeholder
assistant messages, malformed tool calls, orphan tool results, backfills missing tool results with
`"[Tool result unavailable - call was interrupted or lost]"`, and caps tool results
(agent/context_governance.py:316-334,762-950); (b) measures pressure (agent/context_governance.py:384-418);
(c) if over budget with no compaction state, snips old history to fit (`fit_to_budget`,
agent/context_governance.py:336-356, `snip_history`, agent/context_governance.py:951-999), which keeps the system
message and the newest messages that fit; if still too big raises `ContextWindowExceededError`
(agent/context_governance.py:358-382); with compaction state it summarizes history via the consolidator
(`_compact_request_history`, agent/context_governance.py:521). The source transcript is left untouched; only the
model-facing copy is cut. A very long system prompt (bootstrap files, memory, skills summary) directly reduces
the space left for history, and system messages are never snipped (agent/context_governance.py:976-999).

---

## 5. Failure modes and how the code reacts

| Situation | Reaction | Cite |
|---|---|---|
| Tool call with missing/non-string name | Dropped from the response. If all calls were dropped and the finish reason was tool_calls, the request is retried once with a user note telling the model to use valid tool names from the list; if it happens again, a no-tools request is made | agent/runner.py:1142-1186,1188-1245 |
| Unknown tool name | Error text `Tool '<name>' not found. Did you mean ...? Available: ...` returned as the tool result | agent/tools/registry.py:116-124 |
| Params not a JSON object, or fail schema validation | Error text `Invalid parameters for tool ...: <errors>` plus the retry hint. String arguments are JSON-decoded first, and an `{"arguments": ...}` wrapper is unwrapped; params are type-cast before validation | agent/tools/registry.py:131-147,149-180; agent/runner.py:1555-1581 |
| Tool raises an exception | `Error: <Type>: <msg>` + hint returned to the model; `on_execute_tool_error` hooks called | agent/runner.py:1595-1615 |
| Tool returns an error result (`ToolResult.is_error`) | Result text + hint returned as the tool message; event status `error` | agent/runner.py:1617-1633; agent/tools/registry.py:15-16 |
| Same web_fetch URL / web_search query more than twice in one turn | Blocked: `Error: repeated external lookup blocked...` | agent/runner.py:1543-1554; utils/runtime.py:134-144 |
| Private/internal URL (SSRF markers) | Non-retryable error with a boundary note; the model is told to stop trying | agent/runner.py:1647-1659,1698-1705 |
| Workspace boundary violation | Soft error + hint; escalation message after repeated attempts on the same target | agent/runner.py:1662-1669,1728-1745; utils/runtime.py:187-215 |
| exec deny-pattern or allowlist denial | Returned verbatim (no "try a different approach" hint); after more than 2 denials of the same class the message escalates | agent/runner.py:1707-1726; utils/runtime.py:223-260 |
| N consecutive iterations where every tool call failed (default 3) | One `[System note] Tool calls have now failed for N iterations in a row. Stop and reassess...` user message injected, only once per run | agent/runner.py:658-680; utils/runtime.py:93-104 |
| Model returns empty content (no tools) | Retry up to `maxEmptyRetries` (2, so one actual retry since check is `< 2`); then a no-tools "Please provide your response to the user based on the conversation above." call; if still blank, stop_reason `empty_final_response` with the fixed message "I completed the tool steps but couldn't produce a final answer..." | agent/runner.py:699-740,848-865; utils/runtime.py:19-26 |
| Output cut by max_tokens (`finish_reason == "length"`) | Up to 3 continuation rounds, each asking to continue from the exact endpoint and passing the last 64 chars as an immutable tail | agent/runner.py:742-767; utils/runtime.py:17,36-41,78-90 |
| Provider error (`finish_reason == "error"`) | `stop_reason="error"`, final text is the provider text or `Sorry, I encountered an error calling the AI model.`, quota errors get a fixed billing message; a placeholder assistant message keeps history well-formed | agent/runner.py:76-81,826-847 |
| Tool text sent together with tool calls | Text is kept in the assistant message with the calls. The tool contract tells the model not to include the final answer with tool calls (templates/agent/tool_contract.md:10) | agent/runner.py:539-544 |
| Tool calls returned under a non-tool finish reason (e.g. `stop`, `length`) | Ignored with a warning, treated as a final answer | agent/runner.py:692-697 |
| Iteration budget (200) exhausted | `stop_reason="max_iterations"`; queued injections drained; then a no-tools call with the budget-exhausted prompt; if that fails/returns tools/blank, the `max_iterations_message.md` text is used | agent/runner.py:901-934,1275-1329,1371-1380 |
| Loop with active goal at budget | Internal continuation (see 2.7) | session/turn_continuation.py:107-142 |
| `ask_user` tool | Turn ends; question returned to the user | agent/runner.py:532-534,606-614 |
| Request still over budget after snipping | `ContextWindowExceededError` raised, turn fails with an error | agent/context_governance.py:358-382 |
| Any exception in a stage (bus path) | Logged; `delivery.fail` published; loop keeps running | agent/loop.py:1542-1550 |
| Hook exception | Logged and ignored (unless `reraise`) | agent/hook.py:174-183 |
| User cancels (`/stop`) | Partial checkpoint restored into history | agent/loop.py:1504-1541 |
| Repeating identical failing calls (loop) | Handled only by: lookup/violation throttles above, the reflection note at 3 failed iterations, and the 200-iteration cap. Any other identical-call loop is not specifically detected (reading of code, **unverified** by test) | agent/runner.py:658-680 |

Notes: tool hint text `[Analyze the error above and try a different approach.]` is in code
(agent/runner.py:1542). Tool-result offloading: results larger than `maxToolResultChars` are persisted to a file
and replaced by a reference/truncation (agent/context_governance.py:709-760; helper `maybe_persist_tool_result`
**unverified**, not read).

---

## 6. What a prompt/skill/config editor can and cannot change here

Can change (content surfaces):
- Templates in `nanobot/templates/agent/*.md` (identity, tool_contract, skills_section, platform_policy,
  goal_runtime, subagent_system, subagent_announce, max_iterations_message, dream, consolidator_archive,
  `_snippets/untrusted_content.md`). The Jinja environment is `lru_cache`d (utils/prompt_templates.py:17-35);
  **unverified** whether an already-running process picks up an edited file, so restart to be safe.
- Bundled skills `nanobot/skills/<name>/SKILL.md` (frontmatter `name` must equal the directory name and match
  `^[a-z0-9-]+$`, no `--`, `description` 1-1024 chars: agent/skills.py:22,39-48) and workspace skills in
  `<workspace>/skills/`. Workspace skills shadow bundled ones of the same name (agent/skills.py:117-137).
  `always: true` puts the whole body in the system prompt on every turn (cost!) (agent/skills.py:391-401).
- Tool `description` and JSON-schema `parameters` in `nanobot/agent/tools/*.py` (doc 02 lists the exact
  lines per tool). These are model-facing, but the runner validates arguments against the same schema
  (agent/tools/registry.py:141-147), so tightening a schema changes what is accepted, not just what is suggested.
- Config keys in section 4 and doc 04.
- Workspace bootstrap files `AGENTS.md`, `SOUL.md`, `USER.md` (outside repo; see rule below on memory files).

Cannot (or should not) change from these surfaces:
- The turn stage order, hook wiring, runner branches, retry/recovery prompts, the goal nudge and continuation prompt,
  `_behavioral_guidelines`, the planning prompts: these are Python.
- `RunnerLimits.microcompact_*` do nothing (section 4). Do not tune them expecting an effect.
- Goal creation cannot be granted by a prompt: it requires the user `/goal` command (`goal_mutation_permission`,
  agent/goal_permission.py:22-29; command/builtin.py:821). Do not write skills that tell the model to call
  `create_goal` on its own initiative; the call will return an "unavailable" error.
- Do not edit `memory/MEMORY.md`, `SOUL.md`, `USER.md` content programmatically as part of self-improvement:
  templates/agent/identity.md:18 says only Dream consolidation may edit profile and long-term memory.
- Do not set a non-empty `tools.exec.allowPatterns` unless you intend whitelist-only exec (`.agent/gotchas.md`); a
  leftover value blocks all normal commands and causes long retry loops (agent/runner.py:1707-1726 shows the
  throttle).
- Config `${VAR}` behaviour (verified in config/loader.py:271-287,425-437): a missing variable logs a warning
  naming the dotted config path and leaves the `${VAR}` placeholder in place; it never raises. A placeholder left
  in an API key therefore reaches the provider as a literal string. Avoid introducing new `${VAR}` references.
- Do not run `ruff format` (gotchas). Do not put timestamps, local paths, tool-call syntax or internal markers
  (`[Runtime Context`, `[Planning note`, `_meta`) into skills/prompts/examples; they are replayed and imitated
  (gotchas: "Context Pollution Persists"). Note `[Runtime Context ...]` markers are parsed by code
  (runtime_context.py:17-18), so do not reproduce them in prose.

How to check a change is safe (all are read-only or test-only; run from repo root; `docs/core-map/README.md`
has the consolidated list):
1. Templates: render them. `python -c "from nanobot.utils.prompt_templates import render_template as r; print(r('agent/identity.md', runtime='x', workspace_path='/w', agent_workspace_path='/w', platform_policy='p', channel='cli'))"`
   (variables per agent/context.py:333-340); `r('agent/tool_contract.md')`; `r('agent/skills_section.md', skills_summary='x')`;
   `r('agent/goal_runtime.md', goal_start_requested=True, goal_active=True)`. Any Jinja error means broken.
2. Skills: load with `SkillsLoader(Path('<workspace>')).list_skills()` and confirm the skill appears with a
   valid description; check `valid_skill_metadata` (agent/skills.py:39) passes; view the summary via
   `build_skills_summary()`.
3. Whole prompt: `ContextBuilder(workspace).build_system_prompt(channel="cli")` and read the output; check its size
   (`len(...)//4` approximates tokens) against `contextWindowTokens - maxTokens - 1024`.
4. Tool schema: `ToolRegistry.prepare_call(name, args)` returns `(tool, params, error)`
   (agent/tools/registry.py:110-147); verify a valid example returns no error and an invalid one returns a clear
   message.
5. Tests (Docker): `scripts/test-docker.sh pytest tests/agent/test_context_builder.py tests/agent/test_runner_core.py tests/agent/test_runner_goal_continue.py tests/agent/test_context_governance.py -v`;
   `scripts/test-docker.sh ruff check nanobot/ tests/`.
6. Config: `Config` is pydantic; unknown/invalid values are rejected at load. Keep `agents.defaults.limits` keys
   camelCase or snake_case (agent/runner.py:113 `populate_by_name=True`).

---

## 7. Glossary

- **Turn**: one inbound message processed through the stages in `_process_message` (agent/loop.py:1747-1754).
- **Iteration**: one provider call plus tool execution inside a run (`for iteration in range(max_iterations)`,
  agent/runner.py:478).
- **Run / AgentRunner**: the loop executing iterations for one turn (agent/runner.py:180,349).
- **Session / session key**: persisted conversation, key `"{channel}:{chat_id}"` unless overridden
  (bus/events.py:40-42); `process_direct` defaults to `cli:direct` (agent/loop.py:2441) and `MoekaCore.run` to
  `core:default` (core/core.py:626).
- **Transcript**: `[system prompt] + history + current message` (agent/context.py:477-516).
- **Bootstrap files**: `AGENTS.md`, `SOUL.md`, `USER.md` injected into the prompt (agent/context.py:102).
- **Runtime context block**: metadata text appended to the current user message, wrapped in `[Runtime Context ...]`
  (runtime_context.py).
- **Skill**: a `SKILL.md` with YAML frontmatter; summarized in the prompt, read on demand with `read_file`, or inlined
  if `always` or referenced by `$name`.
- **Injection**: a follow-up user message appended mid-turn from the pending queue (agent/runner.py:194-262).
- **Pending queue**: per-session `asyncio.Queue(maxsize=20)` for follow-ups during an active turn (agent/loop.py:1483).
- **Checkpoint**: crash/cancel recovery state saved during a run (agent/runner.py:550,640; agent/loop.py:1021-1035).
- **Compaction / snip**: shrinking the model-facing transcript to fit the context window
  (agent/context_governance.py:336,951). **Auto-compact**: archiving idle sessions (agent/autocompact.py).
- **Consolidator / Dream**: memory summarization components (agent/memory.py, agent/dream.py); see doc 03 section 4.
- **Sustained goal**: long-running objective stored in session metadata, started only by user `/goal`
  (session/goal_state.py; command/builtin.py:791).
- **Internal continuation**: automatic follow-up turn when a goal turn hits the iteration cap
  (session/turn_continuation.py:107).
- **Ephemeral turn**: turn that is not persisted and skips recent-history injection (agent/loop.py:1181).
- **Hook**: Python lifecycle callback object (agent/hook.py).
- **stop_reason values seen in scope**: `completed`, `max_iterations`, `error`, `tool_error`, `empty_final_response`,
  `ask_user` (agent/runner.py:440,608,617,831,850,902), `cancelled`/`error` on hook context only
  (agent/runner.py:362,368).
