# 04 - Config, providers, MoekaCore, sessions

Audience: a small LLM that edits config presets and sampling parameters, and a harness author embedding the
core. Everything below was read from source on the `core-slim` branch. Citation format is `path:line`, relative
to `nanobot/` unless the path starts with `tests/` or `.agent/` (repo root). Anything not confirmed by reading
code is marked **unverified**. Vendor behaviour (vLLM flags etc.) is outside this repo and marked as such.

---

## 1. Config flow: file/dict -> Config -> AgentLoop

- Every config model accepts camelCase and snake_case keys: `Base` uses `alias_generator=to_camel,
  populate_by_name=True` (config_base.py:12-15).
- Root model `Config` (config/schema.py:427-435) has exactly these top-level sections: `agents`, `display`,
  `providers`, `tools`, `modelPresets`, `profiles`. `display` (config/schema.py:22-27: `send_progress`,
  `send_tool_hints`, `show_reasoning`, all default True) only affects how the interactive CLI prints progress
  (cli/agent.py:160-179). Unknown top-level keys fail validation (comment at config/loader.py:442-444).
- `load_config(path)` (config/loader.py:52-156): first retries the lazy forward-ref rebuild
  (config/loader.py:66-75); a missing file gives `Config()` built from `NANOBOT_*` env vars (config/loader.py:79-99;
  `env_prefix="NANOBOT_"`, nested delimiter `__`, config/schema.py:689-692). Otherwise `json.load` ->
  `_migrate_config` (config/loader.py:142, def at config/loader.py:440) -> `Config.model_validate`
  (config/loader.py:144). Errors become `ConfigLoadError` with `kind` `invalid_json` / `invalid_schema` / `io_error`
  / `invalid_root` (config/loader.py:106-153).
- Retired sections: `_migrate_config` drops `channels`, `gateway`, `api`, `heartbeat`, `transcription` with one
  warning (config/loader.py:31,445-454). Nothing is migrated; a later `save_config` writes the file without them.
  Pinned by tests/config/test_removed_keys.py. HAZARD: running slim tooling that saves against a `config.json`
  shared with the full chat-bot distribution deletes those sections for it too.
- `agents.defaults.dream.cron` is also retired: dropped with a warning (config/schema.py:46-62).
- Env resolution: `resolve_config_env_vars` (config/loader.py:271-287) walks the whole model; regex `${VAR}`
  (config/loader.py:268); a missing variable logs a warning with the dotted field path and leaves the placeholder
  (config/loader.py:425-437). It never raises (moeka deviation; tests/config/test_env_var_warnings.py).
- Log hygiene: `nanobot/security/redact.py` (`redact_text`, `redact_value`) masks `sk-` tokens, Authorization/Bearer
  headers and key/token/secret/password `name=value` pairs. Anything that logs request bodies, headers, URLs, error
  bodies or tool arguments must go through it (image-generation clients and the subagent hook do; tests/security/test_redact.py).
  `resolve_env_refs` (config/loader.py:299-314) is the lenient single-string variant and returns `""` if any
  referenced variable is unset. Its docstring (config/loader.py:302-303) still says the whole-config variant
  "raises on a missing variable"; that is stale. `load_config` itself does not resolve `${VAR}`; callers do:
  `config_from_sources` (config/loader.py:200-210), the CLI (`_load_config_for_cli(..., resolve_env=True)`,
  cli/runtime_config.py:66-78,98) and the SDK facade (nanobot.py:110-119).
- `config_from_sources` (config/loader.py:159-210): accepts at most one of `config` / `config_dict` /
  `config_path`; returns `(Config, from_file)`.
- `save_config` (config/loader.py:221-249) writes atomically; OAuth providers (`openaiCodex`, `xaiGrok`) persist
  only `proxy` and `extra_body`.
- `AgentLoop.from_config` (agent/loop.py:500-573): builds `SqliteSessionStore` + `SessionManager`
  (agent/loop.py:523-532), `make_provider(config)` (agent/loop.py:533), `config.resolve_preset()`
  (agent/loop.py:534), and passes the `agents.defaults` knobs listed in doc 01 section 2.1 (agent/loop.py:542-573).
- `from_config` does NOT pass `vec_config` or `vec_store`. The SDK facade `Nanobot.from_config` passes
  `vec_config` only (nanobot.py:137-141); `MoekaCore.from_config` passes both (core/core.py:302-303). So only
  MoekaCore gets a `VecStore` (and only with the `vec` extra installed, core/core.py:318-337).
- Provider choice: `Config._match_provider` (config/schema.py:520-639). Order: forced provider name
  (config/schema.py:543-557) -> explicit `prefix/` (config/schema.py:563-570) -> custom provider by prefix
  (config/schema.py:572-579) -> keyword match (local providers only if `api_base` set, config/schema.py:581-604)
  -> configured local provider with `api_base` (config/schema.py:606-623) -> first API-gateway (e.g. OpenRouter) or other provider with a key
  (config/schema.py:625-632) -> any custom provider with `api_base` (config/schema.py:634-638).
- `make_provider` (providers/factory.py:273-297) wraps in `FallbackProvider` when
  `agents.defaults.fallback_models` is non-empty. `_make_provider_core` sets
  `provider.generation = preset.to_generation_settings()` (providers/factory.py:239). Generation values
  (temperature, max_tokens, reasoning_effort) come ONLY from the preset (or the implicit `default` preset built from
  `agents.defaults`, config/schema.py:487-494).
- `LLMRuntime.capture` copies `provider.generation` (utils/llm_runtime.py:33-58). The runner reads generation from
  the runtime (agent/runner.py:962-974).
- Effective context window with fallbacks = min(primary, each fallback) (providers/factory.py:385-392). An inline
  fallback inherits `max_tokens`, `context_window_tokens`, `temperature` from the primary if None;
  `reasoning_effort` does NOT inherit (providers/factory.py:243-260).
- Input budget = `context_window_tokens - max_tokens - 1024` (agent/context_governance.py:693-707).

## 2. Behaviour-relevant knobs (default / effect)

`agents.defaults` (config/schema.py:118-167):

| Key (camelCase) | Default | Notes |
|---|---|---|
| workspace | `~/.nanobot` (config/schema.py:121) | Flat layout (moeka): the state home is the workspace. Changing it moves the session store (section 5) |
| modelPreset | None (config/schema.py:122) | Name from `modelPresets`; overrides the fields below. Unknown name fails validation (config/schema.py:464-466) |
| model / provider | `anthropic/claude-opus-4-5` / `auto` (config/schema.py:123-126) | |
| maxTokens / contextWindowTokens / temperature | 8192 / 200000 / 0.1 (config/schema.py:127-129) | |
| fallbackModels | [] (config/schema.py:130) | String entries must exist in `modelPresets` (config/schema.py:470-472) |
| maxToolIterations | 200 (config/schema.py:131) | |
| maxConcurrentSubagents | 4, ge=1 (config/schema.py:132) | |
| maxToolResultChars | 16000 (config/schema.py:133) | |
| providerRetryMode | `standard` or `persistent` (config/schema.py:134) | |
| toolHintMaxLength | 40, 20..500 (config/schema.py:135-141) | CLI display only |
| reasoningEffort | None (config/schema.py:142) | |
| timezone / timezoneMode | `UTC` / `auto` (config/schema.py:143-144) | `auto` overwrites `timezone` with the system zone (config/schema.py:169-183) |
| unifiedSession | False (config/schema.py:147) | |
| disabledSkills / allowedSkills | [] / None=all (config/schema.py:148,163) | `allowedSkills: []` hides every non-inline skill |
| idleCompactAfterMinutes (alias sessionTtlMinutes) | 15 (config/schema.py:149-154) | Idle archiving only runs inside `AgentLoop.run()` (doc 01 2.5) |
| idleCompactCheckIntervalSeconds | 60 (config/schema.py:155-158) | |
| dream | `enabled` True, `intervalH` 2, `modelOverride` None (config/schema.py:30-45) | `enabled`/`intervalH` are advisory; nothing in core reads them (doc 03 4.3) |
| vec | see `VecConfig` (config/schema.py:65-82) | Only used when a VecStore exists |
| toolsAllow / toolsDeny | None=all ([]=none) / [] (config/schema.py:161-162) | |
| planning | False (config/schema.py:164) | |
| limits | `RunnerLimits` (config/schema.py:165-167; agent/runner.py:106-123) | `microcompact*` fields have no effect (doc 01 section 4) |

Other sections:
- `ModelPresetConfig` (config/schema.py:99-107): `model`, `provider` `auto`, `maxTokens` 8192,
  `contextWindowTokens` 200000, `temperature` 0.1, `reasoningEffort` None. The preset name `default` is reserved
  (config/schema.py:462-463).
- `VecConfig` (config/schema.py:65-82): `enable` True, `embeddingModel` all-MiniLM-L6-v2,
  `memorySemanticThreshold` 2048, `historyRecentK` 15, `historySemanticK` 10, `skillsTopK` 10, `memoryTopK` 10,
  `logRetrievals` False.
- `ToolsConfig` (config/schema.py:397-424): `maxSessionMessagesPerMinute` 6, `restrictToWorkspace` False,
  `webuiAllowLocalServiceAccess` True (legacy name, still read by exec), `mcpServers` {}, `ssrfWhitelist` [].
  Tool sub-configs: `exec` (agent/tools/shell.py:174-188), `web` (agent/tools/web.py:63-83), `file`
  (agent/tools/filesystem.py:33-36), `my` (agent/tools/self.py:30-33), `imageGeneration`
  (agent/tools/image_generation.py:50-58). Doc 02 has the per-tool keys.
- `ProviderConfig` (config/schema.py:242-279): `apiKey`, `apiBase`, `apiType` (only `providers.openai` may set it
  to something other than `auto`, config/schema.py:361-372), `extraHeaders`, `extraBody`, `extraQuery`, `proxy`,
  `thinkingStyle` in {`thinking_type`, `enable_thinking`, `reasoning_split`} (config/schema.py:262-279).
- `profiles` (config/schema.py:213-233): named scoping bundles resolved by `MoekaCore.create(profile=...)`.
  `vec_collections` (config/schema.py:231) has no consumer (grep): **unverified/unused**.

Gotchas read from code (not runtime-tested):
- `ExecTool.create` passes `cfg.deny_patterns` (default `[]`, agent/tools/shell.py:185,274); `__init__` builds
  `self.deny_patterns = _FLOOR_DENY_PATTERNS + deny_patterns` (agent/tools/shell.py:328), so config/constructor
  patterns only ADD to the always-on floor (fork bomb + history.jsonl/.dream_cursor guards, shell.py:283-299),
  which `_guard_command` checks before the allow-pattern exemption (shell.py:994-997,1086-1100) so `allowPatterns` cannot
  exempt it. Pinned by tests/tools/test_exec_security.py (`test_fork_bomb_*`) and
  tests/tools/test_exec_allow_patterns.py::test_extra_deny_patterns_add_to_the_floor.
- Non-empty `tools.exec.allowPatterns` = whitelist-only exec (agent/tools/shell.py:332-340).
- `tools.restrictToWorkspace: false` does not open everything to the file tools: a non-configurable floor
  (security/protected_paths.py, wired in `_FsTool._resolve_read/_resolve_write`, agent/tools/filesystem.py:156-218)
  always denies `/proc/<pid>/{environ,mem,maps,root,cwd,exe}`, `<data dir>/auth`, `<data dir>/plugin-data`,
  `<data dir>/sessions` and the default sessions root, and denies WRITES to `memory/history.jsonl`,
  `memory/.dream_cursor` and `.nanobot/workspace-id`. No config key or `extra_*` list lifts it. It does not
  apply to `exec` (see the exec caveat in doc 02, "Workspace and network guards tools enforce").
- sudo is blocked unless `tools.exec.allowSudo` (agent/tools/shell.py:627-629,1139-1152).

## 3. Provider layer

- Request path: `AgentRunner._build_request_kwargs` (agent/runner.py:957-974) -> `provider.chat_with_retry` /
  `chat_stream_with_retry` (providers/base.py:1443-1561) -> `_run_with_retry` (providers/base.py:1737-1917) ->
  `OpenAICompatProvider.chat` / `chat_stream` (providers/openai_compat_provider.py:1957,2016) -> `_build_kwargs`
  (providers/openai_compat_provider.py:929-1105) -> `client.chat.completions.create` -> `_parse`
  (providers/openai_compat_provider.py:1543) or `_parse_chunks` (providers/openai_compat_provider.py:1690).
- Retry, standard mode: delays (1, 2, 4) (providers/base.py:626), i.e. 4 attempts in total, then give up
  (providers/base.py:1873-1892). Persistent mode: unlimited attempts, delay capped at 60 s, stops after 10 identical
  errors (providers/base.py:627-628,1852-1871). `Retry-After` honoured plus a 1 s buffer
  (providers/base.py:1894-1896). A non-transient error with image content gets one retry with images stripped
  (providers/base.py:1810-1850).
- Transient test: `is_transient_response` (providers/base.py:1012-1028). A 429 with quota/billing tokens is
  non-retryable (providers/base.py:649-682,1088-1107); 408/409/429/5xx are retryable; `error_kind` timeout/connection
  retryable. Streaming retries only if nothing was streamed yet (providers/base.py:1508,1778-1802).
- `tool_choice` is only sent when tools are present; value = caller's `tool_choice` or `"auto"`
  (providers/openai_compat_provider.py:1070-1072). The runner does not pass `tool_choice` (grep of `agent/`).
- Tool-call parsing: `parse_tool_arguments` (providers/base.py:110-130); malformed JSON is preserved so the registry
  rejects it (no repair for new calls; doc 02 section 4). Text fallback: `<tool_call>{...}</tool_call>` blocks are
  converted when no structured `tool_calls` exist (providers/openai_compat_provider.py:122,246-296). Missing or
  duplicate ids are replaced with 9-char ids (providers/openai_compat_provider.py:1608-1618). Tool calls only execute
  when `finish_reason` is `tool_calls`/`function_call`/`stop` (providers/base.py:602-608).
- Reasoning fields: non-stream reads `message.reasoning_content`, falling back to `message.reasoning`
  (providers/openai_compat_provider.py:1582-1584); stream reads `delta.reasoning_content` then `delta.reasoning`
  (providers/openai_compat_provider.py:1765-1767). Inline `<think>` tags are stripped by `strip_think`
  (utils/helpers.py:168).
- `reasoning_effort` is sent as top-level `reasoning_effort` unless `"none"`
  (providers/openai_compat_provider.py:1037-1040). `temperature` is dropped for gpt-5/o1/o3/o4 model names or whenever
  `reasoning_effort` is set and not `"none"` (providers/openai_compat_provider.py:898-913,960-961), so a local
  reasoning model with `reasoningEffort` set gets the server's default temperature.
- `max_tokens` is sent as `max_tokens`, or `max_completion_tokens` for gpt-5/o-series
  (providers/openai_compat_provider.py:963-968).
- `extraBody` from `providers.<name>.extraBody` is deep-merged last (providers/openai_compat_provider.py:1097-1101;
  merge function providers/openai_compat_provider.py:465-484). This is the place for server-specific sampling keys
  (`top_p`, `top_k`, `min_p`, `repetition_penalty`, `chat_template_kwargs`; accepted by vLLM per its docs,
  **unverified** in this repo). `top_p` is not a first-class knob anywhere in the schema.
- Local endpoints: `_is_local_endpoint` (providers/openai_compat_provider.py:373-401) is true for a spec marked
  `is_local` or a localhost/loopback/private host; such endpoints get a client with keepalive disabled and no proxy
  (providers/openai_compat_provider.py:581-601). Request timeout 120 s (env `NANOBOT_OPENAI_COMPAT_TIMEOUT_S`,
  providers/openai_compat_provider.py:132,211-213). Stream idle timeout 90 s (env `NANOBOT_STREAM_IDLE_TIMEOUT_S`,
  providers/base.py:28-60).
- vLLM registry entry: name `vllm`, OpenAI-compatible backend, `is_local` True, env key `HOSTED_VLLM_API_KEY`, no
  default `api_base` (providers/registry.py:696-704). The API key is optional for local specs
  (providers/factory.py:117-123). `apiBase` is required for auto-routing to a local provider
  (config/schema.py:594-604). Force `provider: "vllm"` in the preset unless the model name contains "vllm".
- Fallback: `FallbackProvider` (providers/fallback_provider.py). Each candidate exhausts its own retry policy first
  and is forced to `standard` mode (providers/fallback_provider.py:375-381). Fallback happens on
  timeout/connection/server_error/rate_limit/overloaded, auth errors, billing (`_should_fallback`,
  providers/fallback_provider.py:671-711), NOT on content_filter, refusal, context_length, invalid_request, HTTP
  400/404/422 (providers/fallback_provider.py:67-72). Circuit breaker: 3 consecutive failures skip the primary for
  60 s (providers/fallback_provider.py:28-29). A fallback uses its own model, max_tokens, temperature,
  reasoning_effort (providers/fallback_provider.py:558-563).
- Usage recording: providers build an `LLMCallRecord` per physical attempt and hand it to an observer
  (providers/base.py:713,764-799), but in the slim core nothing calls `set_llm_call_observer` (grep; the gateway
  that did is gone), so no `llm_usage.sqlite3` rows are written unless a host attaches `record_llm_call`
  (llm_usage/__init__.py:55).

Worked vLLM example (an example derived from the schema, not a tested config):
```json
{
  "providers": {
    "vllm": { "apiBase": "http://127.0.0.1:8000/v1", "apiKey": "EMPTY" }
  },
  "modelPresets": {
    "local": {
      "model": "openai/gpt-oss-20b",
      "provider": "vllm",
      "maxTokens": 4096,
      "contextWindowTokens": 32768,
      "temperature": 0.2
    }
  },
  "agents": { "defaults": { "modelPreset": "local", "providerRetryMode": "standard" } }
}
```
Rules: `contextWindowTokens` must not exceed the server's max model length (input budget here = 32768 - 4096 - 1024 =
27648); the preset must not be named `default`; `model` must equal the served model name (the provider only strips
prefixes its spec declares, `_request_model_name`, providers/openai_compat_provider.py:877-895); setting
`reasoningEffort` suppresses `temperature` (see above).

## 4. MoekaCore (`core/core.py`)

- `create` (core/core.py:70-153): at most one of `config` / `config_dict` / `config_path`; a profile is applied to a
  deep copy (core/core.py:156-190); bootstrap overrides (core/core.py:193-209) and inline skills
  (core/core.py:212-222).
- Temp workspace: when no workspace is given, the config is not from a file, and the workspace is the default
  `~/.nanobot` or contains `${`, a `mkdtemp("moeka-core-")` directory is used and removed by `cleanup()`
  (core/core.py:135-143,353-383).
- `scoped` / `scoped_async` (core/core.py:226-272): always a temp dir if no workspace is given; cleanup in `finally`.
  `scoped` is a sync context manager but `core.run` is async: use `scoped_async` inside async code.
- `from_config` (core/core.py:275-315) mutates the passed Config's `agents.defaults.workspace` in place when a
  workspace is given (core/core.py:293-294).
- Profile compile (core/core.py:156-190): `model_preset` overrides; `tools_allow` replaces; `tools_deny` unions;
  `skills_include` -> `allowed_skills`; `skills_exclude` unions into `disabled_skills`; `memory_enabled` False ->
  `vec.enable` False; `planning`; `limits`. `system_prompt` / `system_prompt_file` feed the in-memory `AGENTS.md`
  override (core/core.py:193-209).
- `action` / `register_action` (core/core.py:389-437) wrap a callable in `FunctionTool` (core/function_tool.py).
  Actions are registered on the loop's registry after the loader ran, so `tools_allow` does not filter them
  (**unverified** whether `tools_deny` applies; no code path seen).
- `add_skill` (core/core.py:456-480) registers an inline skill, effective next turn. Inline skills ignore
  `skills_include` but honour `skills_exclude` (config/schema.py:197-210).
- `ingest` / `ingest_text` / `retrieve` (core/core.py:487-600) return 0/[] when the vec store is unavailable (needs the
  `vec` extra). The vec db is `<workspace>/memory/vec.db` (core/core.py:331).
- `run` (core/core.py:622-654): default `session_key` `core:default`; swaps `self._loop._extra_hooks` for the duration
  of the call and restores it afterwards (core/core.py:638-646). Two concurrent `run()` calls on one core race on that
  attribute; `process_direct` serializes only per session (agent/loop.py:2469-2471). Treat `run()` as
  non-concurrent per core.
- `complete` / `think_structured` (core/core.py:677-719) are stateless one-shots via `nanobot.api.complete`.
- `cleanup()` (core/core.py:353-383) closes `loop.vec_store` and `loop.sessions` if they have a `close()` method
  (core/core.py:368-375). `SessionManager` defines no `close()` (grep of session/manager.py), so the SQLite
  connection is NOT closed by `cleanup()` despite the docstring; only the vec store is. Committed data is safe
  (section 5); the WAL is simply not checkpointed.

## 5. Sessions and crash behaviour

- Location: NOT `<workspace>/sessions.db`. The default `sessions_root` is `<workspace parent>/<workspace
  name>-sessions` (session/sqlite_store.py:115-134); the db is `<sessions_root>/<workspace-id>/sessions.db`
  (session/sqlite_store.py:176-181), where `<workspace-id>` comes from the marker `<workspace>/.nanobot/workspace-id`
  (docstring session/sqlite_store.py:13-16). The store refuses a `sessions_root` inside the workspace
  (session/sqlite_store.py:158-162). For the default flat workspace `~/.nanobot` that is
  `~/.nanobot-sessions/<id>/sessions.db`.
- A pre-ADR-0001 `<workspace>/sessions.db` is only warned about, never moved (session/sqlite_store.py:258-287);
  moving it is the explicit `nanobot sessions migrate` command. Legacy per-session `.jsonl` files are imported once
  at startup and renamed `*.jsonl.imported` (session/sqlite_store.py:289-339). `dump_jsonl(key)` exports one session
  in the old format (session/sqlite_store.py:687); it lives on `SqliteSessionStore`, not on `SessionManager`.
- Schema: `sessions(key, created_at, updated_at, metadata, last_consolidated)` and
  `messages(session_key, seq, role, created_at, data)` (session/sqlite_store.py:232-250). WAL + `busy_timeout` 10 s
  (session/sqlite_store.py:202-203).
- `save` = full replace of that session's message rows in one transaction (session/sqlite_store.py:454-508);
  `fsync=True` adds `wal_checkpoint(TRUNCATE)` (session/sqlite_store.py:506-508). `provider_state` is stored under
  the metadata key `_provider_state` (session/sqlite_store.py:473-479). `load` skips corrupt rows with a warning
  (session/sqlite_store.py:421-424).
- `SessionManager` caches 128 sessions (session/manager.py:43). `get_history` slices from `last_archived`
  (session/manager.py:343-360). The upstream `JsonlSessionStore` is still constructed but unused when a store is
  passed (session/manager.py:1623-1624).
- Runtime checkpoint: the loop stores it in `session.metadata["runtime_checkpoint"]` and calls
  `sessions.save_runtime_checkpoint` (agent/loop.py:2423-2426); with `SqliteSessionStore` this is a full
  `self.save(session)` (session/manager.py:1762-1772). It is cleared and saved at turn end (agent/loop.py:2113-2115).
- SIGKILL: committed SQLite transactions survive; the WAL is not truncated (docstring session/sqlite_store.py:208-214).
  Lost: the in-flight LLM response, any tool that was running (its side effects may have happened), and in-memory
  state after the last checkpoint. On the next turn start `restore_runtime_checkpoint` (session/recovery.py:159-254)
  materializes the assistant message and completed tool results, turns pending tool calls into
  `"Error: Task interrupted before this tool finished."` results (never re-executed, session/recovery.py:196-216),
  drops `provider_state` unless the checkpoint is synchronized (session/recovery.py:229-249), and clears the
  checkpoint. It is called at agent/loop.py:1865 (restore stage) and agent/loop.py:1528 (cancellation).
- Graceful shutdown: nothing in the slim core calls `flush_all()` (session/manager.py:1815-1831) except the SDK
  client helper (sdk/clients.py:152); every `save()` is already its own committed transaction.
- The LLM usage store (`llm_usage.sqlite3`) is separate and, in the slim core, unused (section 3).

## 6. Safe vs dangerous config edits (for a config editor)

Safe: `temperature`, `maxTokens` (keep <= contextWindowTokens/2), `reasoningEffort`, `toolHintMaxLength`,
`maxToolResultChars` (moderate values), `planning`, `disabledSkills`, `allowedSkills` (never `[]` unless intended),
`toolsDeny` entries, `vec.*K`, `tools.web.search.maxResults`, `tools.exec.timeout`,
`limits.toolFailureReflectionThreshold`, `idleCompactAfterMinutes`, new entries in `modelPresets`.

Careful: `contextWindowTokens` (must match the server; too high -> context_length errors that are NOT fallbackable,
too low -> premature compaction), `maxToolIterations`, `providerRetryMode: persistent` (can retry up to 10 identical
errors and blocks the turn), `fallbackModels` (each name must exist in `modelPresets` or the whole config fails
validation), `toolsAllow` (None = all, `[]` = no tools), `unifiedSession`, `timezone`.

Dangerous: non-empty `tools.exec.allowPatterns` (whitelist-only, breaks the agent), `tools.exec.allowSudo: true`,
`tools.exec.denyPatterns`, `tools.exec.sandbox*`, `tools.restrictToWorkspace`, `tools.ssrfWhitelist`,
`tools.mcpServers` (spawns processes), `providers.*.apiKey/apiBase/proxy/extraBody`, renaming a preset to
`default` (rejected), `agents.defaults.workspace` (moves the session store), any JSON syntax error (the whole load
fails with `ConfigLoadError`).

Validation is fail-closed at load: a bad preset reference or a wrong type stops startup. Edit via a temp file,
validate with `Config.model_validate(json.load(f))` (after `_migrate_config` if the file may contain retired
sections), then replace atomically (`save_config`).

## 7. Tests

- tests/config/: test_model_presets.py, test_env_interpolation.py, test_env_var_warnings.py,
  test_config_load_errors.py, test_config_migration.py, test_config_atomic_save.py, test_dream_config.py,
  test_dream_config_legacy.py, test_removed_keys.py, test_retired_cli_apps_config.py, test_tool_config_boundaries.py,
  test_config_paths.py, test_timezone.py, test_merge_missing_defaults.py, test_watcher.py.
- tests/providers/: retry, error metadata, tool arguments, reasoning content, custom provider, extra_body, local
  endpoint detection, timeouts and more (`ls tests/providers`); fallback behaviour is in
  tests/agent/test_runner_fallback.py.
- tests/core/: test_moeka_core.py, test_agent_profiles.py, test_function_tool.py, test_import_boundary.py (the slim
  core must not import removed packages) and the vec tests.
- tests/session/: store, recovery, WAL hygiene, fsync, cache, db migration, turn continuation (`ls tests/session`).
- Run: `scripts/test-docker.sh pytest tests/config tests/providers tests/core tests/session -q`.

## 8. Verification log

Written on `core-slim` (commit 119e0063 plus docs) from the pre-slim config/providers draft, whose facts were
re-checked here. Method: a throwaway checker resolved every `path:line` citation in this file, confirmed the line is
in range, and printed the cited line(s); each was compared with the claim by eye and mismatches were re-located by
grep. Files changed by the slim (config/schema.py, config/loader.py, agent/loop.py, session/sqlite_store.py,
session/manager.py, session/recovery.py) were re-located from scratch; provider files and core/core.py are
unchanged since the draft and kept its numbers after the same check. Dropped from the draft: the gateway usage
observer (`cli/gateway_runtime.py`), the gateway shutdown flush, `api.*`/`gateway.*`/`channels.*` config rows, the
`CliAppsToolConfig` row, `webui_allow_remote_package_install` (no longer in the schema), and the `sdk/types.py`
open item (the file exists: `ls nanobot/sdk`). New findings: `MoekaCore.cleanup()` does not close the session store;
no LLM-usage observer is attached anywhere; `DreamConfig.enabled`/`interval_h` have no reader;
`resolve_env_refs`'s docstring is stale.
