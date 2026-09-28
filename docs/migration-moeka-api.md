# Migrating to the moeka API

This page maps every deprecated entry point, and the awork patterns built on
them, to the `moeka` API described in [`python-sdk.md`](python-sdk.md).

- The old entry points keep their current behaviour and only add a
  `DeprecationWarning` naming the replacement. They are removed once awork pins
  a migrated commit.
- The new API is a clean break, not a rename: calls raise typed errors, spend
  goes through a `Budget`, stages are trace spans, and agents are specs on a
  kernel. The [behaviour differences](#behaviour-differences) section lists what
  a straight port would get wrong.
- One `Kernel` per process (or per tenant, or per rollout) replaces the
  per-call config objects. Build it once, share it, close it on shutdown.

## Entry points

| Deprecated | Replacement |
|---|---|
| `nanobot.api.complete.acomplete(prompt, system=, images=, config=, model=, preset=, max_tokens=, temperature=, response_format=)` | `(await kernel.llm.complete(prompt, system=, images=, opts=GenerateOptions(model=, sampling=Sampling(max_tokens=, temperature=), response_format=))).text` |
| `nanobot.api.complete.complete(...)` | `kernel.llm.complete_sync(...).text` |
| `nanobot.api.complete.acomplete_json(prompt, schema=, model_cls=, retries=, system=, task_type=, task_payload=)` | `(await kernel.llm.complete_json(prompt, schema=, model_cls=, retries=, system=, task_type=, task_payload=)).parsed` |
| `nanobot.api.complete.complete_json(...)` | `kernel.llm.complete_json_sync(...).parsed` |
| `nanobot.api.complete.acomplete_stream(prompt, ...)` | `kernel.llm.stream([system(...), user(prompt)], opts)`: iterate the deltas, then `await stream.completion()` |
| `nanobot.api.complete.complete_stream(prompt, ...)` | `with kernel.llm.stream_sync(messages, opts) as s: for delta in s: ...` |
| `usage_sink=callable` (any of the above) | `kernel.trace.subscribe("model.call", fn)`, or read `Completion.usage` / `cost_usd` |
| `preset="name"` | `GenerateOptions(model="name")` (a `ModelSpec` alias) |
| `model="provider/raw-id"` | `GenerateOptions(model="provider/raw-id")`: a name that is not an alias goes to the default model's provider |
| `config=` / `config_dict=` / `config_path=` | `Environment.for_host(...)` once; `Environment.from_config(config, state_dir=, work_dir=)` while a legacy `Config` still exists |
| `MoekaCore.create(config=, profile=, bootstrap=, skills=)` | `Kernel(env)` + `kernel.agent(AgentSpec.from_profile(profile, name=...))` (a profile given by name: `config.profiles[name]`), with `bootstrap=` / `inline_skills=` on the spec |
| `MoekaCore.from_config(config, workspace=)` | `Kernel(Environment.from_config(config, state_dir=, work_dir=workspace))` |
| `MoekaCore.scoped(...)` / `scoped_async(...)` | `with Kernel(env) as kernel:` / `async with Kernel(env) as kernel:` |
| `await core.run(message, session_key=, media=, hooks=, on_token=)` | `await agent.run(message, session=, media=)` -> `RunResult`; `agent.stream(...)` for tokens |
| `core.think(message)` | `(await agent.run(message)).content` |
| `MoekaCore.complete` / `complete_sync` / `think_structured` | `kernel.llm.complete` / `complete_sync` / `complete_json` |
| `@core.action` / `core.register_action(fn)` | `AgentSpec(actions=(fn,))` or `agent.add_action(fn, read_only=, capabilities=, output_model=)` |
| `core.set_bootstrap(name, text)` / `core.add_skill(skill)` | a new `AgentSpec` with `bootstrap={name: text}` / `inline_skills=(skill,)` (one agent per spec) |
| `core.ingest(...)` / `ingest_text(...)` | `kernel.memory(scope).add(text, source=, tags=, collection=)` |
| `core.retrieve(...)` / `retrieve_documents(...)` | `kernel.memory(scope).search(query, k=, mode=, tags=, since=, collection=)` -> `[Hit]` |
| `core.count_documents(...)` / `clear_documents(...)` | `DocStore.count(...)` / `DocStore.clear(...)` (see the collection defaults below) |
| `nanobot.core.vec.open_vec_store(db_path)` | `kernel.memory(path=db_path)` |
| `VecStore.add_documents` / `search_documents[_scored]` / `count_documents` / `document_sources` / `clear_documents` | `DocStore.add` / `search` / `count` / `sources` / `clear` |
| `VecStore.get_meta` / `set_meta` / `available` / `keyword_available` | the same names on `DocStore` |
| `AgentHook` subclasses | `kernel.trace.subscribe(...)` for observation, `agent.stream(...)` for live events |
| `AgentProfileConfig` | `AgentSpec.from_profile(profile)`, or build `AgentSpec` directly |

## awork patterns

### `moeka_config(secrets)` -> `Environment.for_host`

- The host keeps reading its own secrets (`keys.env`); the kernel never does.
- The same provider and model, with prices so cost and caps work:

  ```python
  env = Environment.for_host(
      state_dir=data_dir / "moeka-state",
      work_dir=data_dir / "moeka-work",
      credentials={"openrouter": secrets["OPENROUTER_API_KEY"]},
      providers=[ProviderSpec(name="openrouter", credential="openrouter")],
      models=[ModelSpec(name="main", model=chosen_model, provider="openrouter",
                        tier="standard", price_in=..., price_out=...)],
      default_model="main",
      tools={"web": {"search": {"provider": "brave", "apiKey": brave_key}}},
  )
  kernel = Kernel(env, budget=CapBudget(limit_usd=run_cap), cache=AworkCache(...))
  ```

- `tools=` takes the same `tools` config section `moeka_config` built (only
  when the research agent needs web search).
- `moeka_config_source(...)` has no replacement: nothing takes config kwargs
  any more.

### `MoekaLLM.complete` / `complete_json` / `complete_many`

- `llm.complete(prompt, system, model=, images=, max_tokens=, temperature=)` ->
  `kernel.llm.complete_sync(prompt, system=system, images=images,
  opts=GenerateOptions(model=..., sampling=Sampling(max_tokens=..., temperature=...))).text`.
- `llm.complete_json(...)` -> `kernel.llm.complete_json_sync(..., model_cls=...).parsed`.
- `complete_many(calls, max_concurrency=8)` (thread-pool fan-out, results in
  order) -> `kernel.llm.batch_sync([Request(messages, opts=..., model_cls=...)],
  concurrency=8)`:
  - `BatchResult.outcomes` is in input order; each is a `Completion` or that
    item's `LLMError`, so one bad item no longer needs `return_exceptions`.
  - On a systemic stop (auth, quota, budget) the items that already finished
    keep their `Completion`s; only unfinished ones get the error. That replaces
    `last_partial` salvage: read `result.completions`.
  - Rate limits pause the whole batch and retry the item (up to 3 times) instead
    of failing it.

### `usage_sink=ledger.record` / `UsageLedger` -> budget and trace

- Spend caps: the worst-case token reservation awork computes is what the kernel
  now does itself. Use `CapBudget(limit_usd=..., per_tag={"stage": ...})`, or
  implement the three-method `Budget` protocol (`admit(estimate)` /
  `settle(reservation, event)` / `release(reservation)`) over the existing
  ledger. Admission covers every JSON round and every agent model call.
- Metering: subscribe once, `kernel.trace.subscribe("model.call", ledger.on_call)`.
  The event carries `tokens_in`, `tokens_out`, `tokens_cache_read`, `cost_usd`,
  `model`, `alias`, `call_id`, `attempt`, `cached` and `tags` (the stage). One
  event per physical attempt, so retries are counted.
- The field names differ from the legacy `usage_sink` payload
  (`prompt_tokens` / `completion_tokens` / `total_tokens` / `cached_tokens`);
  adapt the ledger's `record`.
- Per call: `Completion.usage` (`input_tokens`, `output_tokens`,
  `cache_read_tokens`, `cache_write_tokens`) and `Completion.cost_usd`.

### `_call_with_timeout(fn, timeout_s=, attempts=)` -> `GenerateOptions`

- `GenerateOptions(timeout_s=..., attempts=...)`. On expiry the provider call is
  cancelled (no worker thread keeps running) and `LLMTimeoutError` is raised
  (also a builtin `TimeoutError`).
- `timeout_s` is one deadline for the whole logical call, retries and JSON
  rounds included, not per attempt. For awork's "N attempts of T seconds",
  pass `timeout_s=N * T`, or retry the kernel's `LLMTimeoutError` in the host
  (import it under an alias: awork has its own class of that name).
- The sync twins are safe from any thread and inside a running event loop, so
  the daemon-thread bridge is not needed.

### `_provider_error_message(text)` -> typed errors

- Nothing to sniff: errors are exceptions, never content.
- awork defines its own `LLMError`, `LLMProviderError` and `LLMTimeoutError`
  (`awork/llm.py`), and two of those names clash with `moeka.errors`. Import the
  kernel's under aliases, for example `from moeka import errors as merr`.

  ```python
  from moeka import errors as merr

  try:
      done = kernel.llm.complete_json_sync(prompt, model_cls=Fit)
  except merr.BudgetExceeded:
      raise BudgetHalt("spend cap reached") from None   # see Behaviour differences
  except (merr.QuotaError, merr.AuthError, merr.RateLimitError) as exc:
      raise LLMProviderError(str(exc)) from exc          # awork's own class
  except merr.LLMTimeoutError as exc:
      raise LLMTimeoutError(str(exc)) from exc           # awork's own class
  except merr.LLMError as exc:    # exc.kind, exc.status, exc.raw, exc.retryable
      ...
  ```

- A legitimate answer that mentions "quota" or "rate limit" can no longer be
  mistaken for an error.

### `stage_scope(llm, stage)` -> `kernel.trace.span`

- `with kernel.trace.span(stage, stage=stage):`. The tag lands on every
  `model.call` event and every budget estimate of the calls inside, so
  `CapBudget(per_tag={"stage": ...})` caps each stage.
- Spans are context variables, so concurrent stages in different asyncio tasks
  (or batch items) never see each other's tag, unlike the shared `set_stage`
  field. A plain thread the host starts itself does not inherit the span: wrap
  its work in `contextvars.copy_context().run(...)`.
- Wall-clock timing per stage stays the host's job (time the `with` block), or
  sum `latency_ms` from `model.call` events by `tags["stage"]`.

### Research agent: `MoekaCore.scoped` + `AgentProfileConfig` + `AgentHook`

```python
spec = AgentSpec.from_profile(_research_profile(memory=False), name="research")
agent = kernel.agent(spec)
result = agent.run_sync(task, session="awork:research", tags={"stage": "research"})
text, tools, usage, cost = result.content, result.tools_used, result.usage, result.cost_usd
```

- `RunResult.usage` and `cost_usd` cover the whole run, so the `_UsageHook`
  that summed `context.usage` per iteration goes away.
- Check `result.stop_reason` (`completed`, `max_iterations`, `budget`,
  `deadline`, ...) instead of guessing from the text; a provider failure is
  `stop_reason="error"` with a typed `result.error`.
- `from_profile` raises `ValueError` for a profile that sets `planning` or
  `limits`: move limits into `AgentSpec(limits=RunLimits(max_iterations=...,
  deadline_s=...))`; planning has no equivalent.
- A profile's `memory_enabled` defaults to True, which gives the agent semantic
  memory (and loads the embedder). Set it False in the profile, or build the
  `AgentSpec` directly, if the research agent should not remember.
- `_research_profile(memory=True)` sets `vec_collections=[RESEARCH_COLLECTION]`,
  which `from_profile` maps to `doc_scopes`. A non-empty `doc_scopes` adds the
  `search_documents` action, and the action is checked against `tools_allow`.
  That profile's `tools_allow` is `RESEARCH_TOOLS`, which lacks it, so
  `kernel.agent(spec)` raises `ValueError`. Add `"search_documents"` to the allow
  list (and the names of the memory actions `_add_memory_actions` registers,
  `remember_fact` / `recall_research`).
- The research memory does not carry over. The legacy agent kept its collection
  inside `<workspace>/memory/vec.db`. A doc scope is its own file,
  `state_dir/memory/<scope>.db`, so the new agent starts empty. If continuity
  matters, copy the old collection once, before the first run. `DocStore` has
  no "list every chunk" call (`search` ranks and `sources` only counts), so do
  one of these:
  - re-ingest from the host's own records into `kernel.memory(RESEARCH_COLLECTION)`;
  - read the text rows of that collection straight from the old file's
    `documents_data` table (`source`, `text`, `collection`) with `sqlite3` in a
    one-off migration script, and `add`
    them to the new scope.
- Host actions become `AgentSpec(actions=(fn, ...))`; with `tools_allow` set,
  list the action names in it too.
- `MoekaCore.scoped` made a temporary workspace and deleted it. The kernel works
  in the `work_dir` the host chose; use a `tempfile.TemporaryDirectory` for it
  when the workspace must not persist. Sessions now persist in `state_dir`.

### `open_vec_store(db)` + counts -> `kernel.memory(path=db)`

```python
docs = kernel.memory(path=vec_db_path)
stale = docs.get_meta(SIG_KEY) != signature or docs.count(collection=public) == 0
if stale:
    docs.clear(collection=None)                    # this file holds only the corpus
    for d in corpus:
        docs.add(d.embed_text, source=d.name, collection=d.collection)
    docs.set_meta(SIG_KEY, signature)
hits = docs.search(query, k=8, collection=public)
```

- `sources(collection=...)` gives chunk counts per source, replacing ad-hoc SQL
  over the store's tables.
- An unopenable file raises `RuntimeError` on first use instead of degrading to
  empty results, so the "store unusable" branch becomes an `except`.
- All stores of one kernel share one embedder: several corpora load the model
  once.

### `llm_cache` -> `ResponseCache`

- Implement `get(key) -> Completion | None` and `put(key, completion)` over the
  existing SQLite table and pass `Kernel(cache=...)`. Keys come from
  `kernel.llm.request_key(request)`, which covers messages, the resolved model,
  sampling, response format, schema and retries.
- The cache sees `Completion` objects (a frozen dataclass): `put` serialises the
  fields the host needs (`text`, `usage`, `model`, `cost_usd`, ...) and `get`
  rebuilds a `Completion`. For JSON calls `text` is the raw reply; the kernel
  re-parses nothing on a hit, so store `parsed` too (or rebuild it from `text`).
- Which calls may be cached stays the host's policy: pass
  `GenerateOptions(cache=False)` for non-deterministic calls (awork caches only
  temperature 0). Only successful, untruncated replies are stored; a hit costs
  nothing and skips the budget.

## Behaviour differences

- `BudgetExceeded` is an ordinary `Exception` (an `LLMError`), but awork's
  `BudgetHalt` is deliberately a `BaseException`, so that its many best-effort
  `except Exception` sites cannot swallow a spend stop. A straight port would
  let those sites catch budget refusals and retry the work serially. At the LLM
  boundary, either translate `BudgetExceeded` into `BudgetHalt` (see the
  `_provider_error_message` example above), or re-raise it above every
  best-effort handler. The same applies to agent runs, where a refusal is
  `result.stop_reason == "budget"` and not an exception: check it and raise
  `BudgetHalt`. In a batch, it is `BatchResult.systemic`.
- Errors are raised, typed. `acomplete` returned provider errors as content
  (`"Error: ..."`, `"Error calling LLM: ..."`); `kernel.llm` raises an `LLMError`
  subclass and never returns error text.
- `complete_json` failures: the legacy call raised `ValueError` after the
  retries; the new one raises `ParseError` (an `LLMError`, not a `ValueError`,
  with `raw` and `attempts`), or `TruncatedError` when the reply was cut at the
  token limit. Update `except ValueError` clauses.
- JSON re-prompt shape: the legacy loop sent one new user prompt holding the
  original prompt, the bad reply and the error. The kernel sends the original
  messages, then the bad reply as an assistant turn, then a user turn with the
  error. Only the latest bad reply is shown.
- A provider that rejects native `response_format` used to consume one of the
  retries; now that round is free (a `retries=0` call still gets a plain-text
  attempt).
- The result is a `Completion`, not a `str` or parsed value: read `.text` or
  `.parsed`.
- Timeouts: `timeout_s` is one deadline per logical call and cancels the
  provider call (see the `_call_with_timeout` section above).
- Sync calls inside a running event loop: the legacy `complete` /
  `complete_json` raised there; the `*_sync` twins work there.
- Document store defaults:
  - `DocStore.search` defaults to `mode="hybrid"`; `VecStore.search_documents`
    and `core.retrieve` defaulted to `mode="vec"`.
  - `DocStore.count()` and `sources()` default to every collection;
    `VecStore.count_documents()` counted only `"default"`. Pass
    `collection="default"` to keep the old count.
  - `DocStore.clear()` clears only the `"default"` collection. Wiping the file
    needs `clear(collection=None)`.
  - `search` returns `Hit(source, text, score, tags, collection)` objects, not
    strings; `score` is lower-is-better in every mode.
- Config: `Environment.for_host` and `from_config` read no environment variable,
  no `~/.nanobot` and no `NANOBOT_*` setting. `from_config` leaves `${VAR}`
  placeholders unexpanded.
- Child processes (the `exec` tool, stdio MCP servers) get their environment
  from `exec_base_env` only; the host process environment is not inherited.
  Supply `PATH` (and `SYSTEMROOT` / `TEMP` on Windows).
- The facts store moves to schema version 2 on first open; code from before this
  API cannot open it afterwards. Keep a copy of `facts.db` if a rollback past
  this release is possible.
