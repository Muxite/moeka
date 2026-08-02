# Python SDK

Use nanobot as a library — no CLI, no gateway, just Python.

## Quick Start

```python
import asyncio

from nanobot import Nanobot


async def main() -> None:
    bot = Nanobot.from_config()
    result = await bot.run("What time is it in Tokyo?")
    print(result.content)


asyncio.run(main())
```

`Nanobot.from_config()` reuses your normal `~/.nanobot/config.json`, so the SDK follows the same provider, model, tools, and workspace defaults as the CLI unless you override them.

## Common Patterns

### Use a specific config or workspace

```python
from nanobot import Nanobot

bot = Nanobot.from_config(
    config_path="~/.nanobot/config.json",
    workspace="/my/project",
)
```

### Isolate conversations with `session_key`

Different session keys keep independent conversation history:

```python
await bot.run("hi", session_key="user-alice")
await bot.run("hi", session_key="task-42")
```

### Attach hooks for observability

Hooks let you inspect tool calls, streaming, and iteration state without modifying nanobot internals:

```python
from nanobot.agent import AgentHook, AgentHookContext


class AuditHook(AgentHook):
    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tc in context.tool_calls:
            print(f"[tool] {tc.name}")


result = await bot.run("Review this change", hooks=[AuditHook()])
```

## API Reference

### `Nanobot.from_config(config_path=None, *, workspace=None)`

Create a `Nanobot` instance from a config file.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `config_path` | `str \| Path \| None` | `None` | Path to `config.json`. Defaults to `~/.nanobot/config.json`. |
| `workspace` | `str \| Path \| None` | `None` | Override the workspace directory from config. |

Raises `FileNotFoundError` if an explicit config path does not exist.

### `await bot.run(message, *, session_key="sdk:default", hooks=None)`

Run the agent once and return a `RunResult`.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `message` | `str` | *(required)* | The user message to process. |
| `session_key` | `str` | `"sdk:default"` | Session identifier for conversation isolation. Different keys get independent history. |
| `hooks` | `list[AgentHook] \| None` | `None` | Lifecycle hooks for this run only. |

### `RunResult`

| Field | Type | Description |
|-------|------|-------------|
| `content` | `str` | The agent's final text response. |
| `tools_used` | `list[str]` | Reserved for richer SDK introspection; may be empty in current versions. |
| `messages` | `list[dict]` | Reserved for richer SDK introspection; may be empty in current versions. |

## Hooks

Hooks let you observe or customize the agent loop. Subclass `AgentHook` and override the methods you need.

### Hook lifecycle

| Method | When |
|--------|------|
| `wants_streaming()` | Return `True` if you want token-by-token `on_stream()` callbacks |
| `before_iteration(context)` | Before each LLM call |
| `on_stream(context, delta)` | On each streamed token when streaming is enabled |
| `on_stream_end(context, *, resuming)` | When streaming finishes |
| `before_execute_tools(context)` | Before tool execution |
| `after_iteration(context)` | After each iteration |
| `finalize_content(context, content)` | Transform final output text |

Useful fields on `AgentHookContext` include:

- `iteration`
- `messages`
- `response`
- `usage`
- `tool_calls`
- `tool_results`
- `tool_events`
- `final_content`
- `stop_reason`
- `error`

### Example: audit tool calls

```python
from nanobot.agent import AgentHook, AgentHookContext


class AuditHook(AgentHook):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tc in context.tool_calls:
            self.calls.append(tc.name)
            print(f"[audit] {tc.name}({tc.arguments})")
```

```python
hook = AuditHook()
result = await bot.run("List files in /tmp", hooks=[hook])
print(result.content)
print(f"Tools observed: {hook.calls}")
```

### Example: receive streaming tokens

```python
from nanobot.agent import AgentHook, AgentHookContext


class StreamingHook(AgentHook):
    def wants_streaming(self) -> bool:
        return True

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        print(delta, end="", flush=True)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        print()
```

### Compose multiple hooks

Pass multiple hooks when you want to combine behaviors:

```python
result = await bot.run("hi", hooks=[AuditHook(), MetricsHook()])
```

Async hook methods are fan-out with error isolation. `finalize_content` is a pipeline: each hook receives the previous hook's output.

### Example: post-process final content

```python
from nanobot.agent import AgentHook


class Censor(AgentHook):
    def finalize_content(self, context, content):
        return content.replace("secret", "***") if content else content
```

## Full Example

```python
import asyncio
import time

from nanobot import Nanobot
from nanobot.agent import AgentHook, AgentHookContext


class TimingHook(AgentHook):
    def __init__(self) -> None:
        super().__init__()
        self._started_at = 0.0

    async def before_iteration(self, context: AgentHookContext) -> None:
        self._started_at = time.perf_counter()

    async def after_iteration(self, context: AgentHookContext) -> None:
        elapsed_ms = (time.perf_counter() - self._started_at) * 1000
        print(f"[timing] iteration {context.iteration} took {elapsed_ms:.1f}ms")


async def main() -> None:
    bot = Nanobot.from_config(workspace="/my/project")
    result = await bot.run(
        "Explain the main function",
        session_key="sdk:demo",
        hooks=[TimingHook()],
    )
    print(result.content)


asyncio.run(main())
```

## MoekaCore — the embeddable agent/RAG core

`Nanobot` (above) wraps the full chat-bot runtime. `nanobot.core.MoekaCore` is a
smaller, channel-free facade over the same agent engine — no gateway, no
channels, no WebUI — meant for embedding in another Python application (e.g. a
content pipeline). The import boundary is enforced by
`tests/core/test_import_boundary.py`: importing `nanobot.core` pulls in zero
chat-runtime dependencies.

```python
from nanobot.core import MoekaCore

core = MoekaCore.create(config_dict={
    "providers": {"openrouter": {"apiKey": "${OPENROUTER_API_KEY}"}},
    "agents": {"defaults": {"model": "google/gemini-3-flash-preview",
                             "provider": "openrouter"}},
})

@core.action
def get_disk_usage(path: str) -> str:
    "Return human-readable disk usage for a path."
    import shutil
    total, used, free = shutil.disk_usage(path)
    return f"{used // 2**30} GiB used, {free // 2**30} GiB free"

result = await core.run("How much disk is free on /?")
print(result.content, result.tools_used)
core.cleanup()
```

`MoekaCore.create()` takes **at most one** config source (`config=`, a built
`Config`; `config_dict=`, a plain dict; or `config_path=`, a file) plus optional
`workspace=`, `model=`, `provider=`, and `profile=`. With no workspace and an
in-memory config it runs in a throwaway temp dir rather than touching
`~/.nanobot`. `MoekaCore.from_config(config, workspace=...)` is the pure data
seam underneath — no file discovery.

See [`docs/core-architecture.md`](core-architecture.md) for the full subsystem
design (`core.py`, `vec.py`, `vec_store.py`, `function_tool.py`).

### Scoped agent profiles

`MoekaCore.scoped(profile=...)` (sync) / `MoekaCore.scoped_async(profile=...)`
context-manage a core whose workspace is guaranteed to be cleaned up, and apply
an `AgentProfileConfig` — a name from `config.profiles`, an
`AgentProfileConfig` instance, or a plain dict:

```python
with MoekaCore.scoped(
    profile={
        "system_prompt": "You are a terse changelog editor.",
        "tools_allow": ["read_file", "web_search"],
        "planning": True,
    },
    config_path="~/.nanobot/config.json",
) as core:
    answer = await core.run("Summarize the diff.")
```

Profile fields: `model_preset`, `system_prompt` / `system_prompt_file`,
`tools_allow` (hard allowlist — tools added later never silently appear) /
`tools_deny`, `skills_include` / `skills_exclude`, `skills_inline`,
`memory_enabled`, `planning`, and `limits` (a `RunnerLimits` override). This
lets one host process run multiple differently-scoped agent personas or
subagents against the same underlying core.

### In-memory bootstrap and inline skills

Personality and skills can be passed as objects rather than round-tripped
through workspace files — an embedded core never writes `AGENTS.md` or
`SKILL.md` into a host's workspace:

```python
from nanobot.config.schema import InlineSkillConfig

core = MoekaCore.create(
    config_dict=cfg,
    bootstrap={"AGENTS.md": persona_text, "USER.md": profile_text},
    skills=[InlineSkillConfig(name="triage", content=SKILL_MD, description="Triage tickets")],
)
core.set_bootstrap("USER.md", updated_profile)   # takes effect next run()
core.add_skill({"name": "deploy", "content": DEPLOY_MD})
```

- `bootstrap` maps a section name to markdown content. A key matching a
  bootstrap file (`AGENTS.md`, `SOUL.md`, `USER.md`, ...) **shadows** the
  workspace file; any other key is appended as an extra section.
- A profile's `system_prompt` / `system_prompt_file` feeds the same channel
  under `"AGENTS.md"` (an explicit `bootstrap` key wins). `system_prompt_file`
  is read once at `create()` — the path is the host's choice, the core only
  sees content. **Behavior note:** the profile persona now beats a pre-existing
  workspace `AGENTS.md`, which fixes stale personas in persistent host
  workspaces (the old seed was written once and never refreshed).
- Inline skills shadow workspace/builtin skills of the same name and bypass
  `skills_include` (the host registered them explicitly), but still honor
  `skills_exclude` and `requires`/`always` metadata. Subagents see the same
  inline set. `skills_include=[]` is the documented opt-out from the builtin
  catalog.

### Documents — hybrid FTS5 + vector RAG

`core.ingest()` / `core.ingest_text()` index host-supplied text or files into a
SQLite-backed store (`<workspace>/memory/vec.db`, via `nanobot/core/vec_store.py`);
`core.retrieve()` / `core.retrieve_documents()` search it back:

```python
core.ingest("Project X ships on Friday.", source="notes", tags=["planning"])
chunks = core.retrieve("when does X ship?", mode="hybrid", k=5)
```

`mode` is one of:

| Mode | Needs `moeka[vec]`? | Description |
|------|---------------------|-------------|
| `"vec"` (default) | Yes | Semantic KNN over sentence-transformer embeddings |
| `"keyword"` | No | FTS5/BM25 keyword search — works with stock `sqlite3` |
| `"hybrid"` | No (degrades to keyword-only without `vec`) | Reciprocal-rank fusion of both |

`tags` and `since` filter by stored metadata; `collection` scopes to a named
corpus (`collection=None` searches all collections); `caller` labels the entry
in the optional retrieval-log (`vec.log_retrievals`). `core.retrieve()` returns
bare strings; `core.retrieve_documents()` returns `RetrievedChunk(text, source,
score)` for source attribution and score-based thresholding. All document
methods degrade to empty/`0`/no-op — never raise — when `moeka[vec]` isn't
installed.

For a standalone embeddings store with no `AgentLoop`/provider/config at all,
use `nanobot.core.vec.open_vec_store(db_path)` directly.

### One-shot completion (no agent loop)

`MoekaCore.complete()` / `MoekaCore.complete_sync()` skip the tool-calling loop
entirely — a single provider call through moeka's model-preset/fallback layer,
usable without any workspace:

```python
text = await MoekaCore.complete("Summarize this README", system="Be terse.")
text = MoekaCore.complete_sync("...")  # sync bridge; raises inside a running loop
```

Streaming variants (`nanobot.api.complete.acomplete_stream` /
`complete_stream`) yield text chunks as they arrive; `complete_stream` runs the
async version in a worker thread with its own event loop so synchronous host
applications (e.g. a pipeline that runs `complete_stream(...)` inside a plain
`for` loop) can consume it without an `asyncio.run()` of their own. Structured
one-shot output is available via `MoekaCore.think_structured(prompt, schema=...)`
or `model_cls=<a pydantic model>` — a provider-agnostic parse-retry loop, no
native JSON mode required.

### Planning & reflection

Independent of `MoekaCore`, two opt-in `AgentLoop`/`AgentRunner` behaviors
apply to both the chat-bot runtime and the embeddable core:

- **Planning** — `agents.defaults.planning: true` (or a profile's
  `planning: true`) runs one extra LLM call before the main turn to produce a
  short execution plan, injected as a plan note.
- **Reflection** — after `limits.tool_failure_reflection_threshold`
  (default `3`) consecutive iterations where every tool call in the turn
  failed, the runner injects a "stop and reassess" reflection message instead
  of continuing to retry blindly. Set the threshold to `0` to disable.
