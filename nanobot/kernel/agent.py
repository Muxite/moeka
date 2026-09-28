"""Agents (Task 9): ``kernel.agent(AgentSpec(...))`` runs an agent loop on the kernel.

An :class:`Agent` is an ``AgentLoop`` built from the kernel's environment with the
spec applied, living on the kernel's loop thread:

- **Config.** ``env.config`` scoped by the spec, through the same profile compiler
  ``MoekaCore.create(profile=...)`` uses (:mod:`nanobot.config.profile`): tool
  allow/deny, skills include/exclude/inline, persona (``system_prompt`` fills
  ``AGENTS.md`` unless ``bootstrap`` has one), memory.
- **Memory** (Task 11). ``spec.memory`` gives the loop's semantic memory the kernel
  document store ``kernel.memory(f"agent:{name}")`` (under ``state_dir``, the kernel's
  shared embedder, pinned open while the agent is). Every kernel agent keeps its
  memory files (MEMORY.md, history.jsonl, cursors) in
  ``<state_dir>/agents/<name>/memory`` (:func:`agent_memory_dir`), never in
  ``work_dir`` and never shared with another agent; only a ``memory=True`` agent's
  prompt includes them (the long-term memory and the "Recent History" section).
  Rewinding, forking into or deleting a session drops that key's archived history
  entries when they no longer match the transcript (see ``Kernel``). ``spec.doc_scopes`` adds the
  read-only ``search_documents(query, scope=None, k=5)`` action over those scopes
  (hybrid search), registered like any action: the tool scope must admit it.
- **Model.** ``spec.model`` resolves like ``kernel.llm`` (an alias from
  ``env.models`` / ``register_provider``, else a raw id on the default model's
  provider; ``None`` = the default model). The provider comes from the kernel's
  pool, so under a budget it is ``BudgetedProvider``-wrapped: every agent call is
  admitted, metered and ledgered like a one-shot.
- **Shared state.** One ``SessionManager`` per kernel (under ``env.paths.state_dir``),
  shared by all its agents; ``kernel.core_env`` (the kernel ``Tracer`` as trace);
  ``kernel.variant``; the policy is the kernel's and the spec's intersected.
- **Runs.** Each :meth:`Agent.run` is a ``kernel.trace.span("agent.run", agent=<name>,
  **tags)`` (a root span, so a fresh ``trace_id``, unless the caller has one open)
  and passes its own hooks to ``process_direct`` (a ``TraceHook`` and a capture
  hook), so concurrent runs never share hook state.

Stop reasons (:data:`StopReason`) come from the runner, plus ``deadline`` (the run's
``asyncio.timeout`` expired), ``budget`` (the budget refused a call; ``error`` is the
:class:`~nanobot.kernel.llm_errors.BudgetExceeded`) and ``cancelled`` (the agent or
kernel was closed mid-run). A caller cancelling its own ``run`` task gets
``CancelledError`` as usual. A provider error ends the run with ``error`` set to the
typed :class:`~nanobot.kernel.llm_errors.LLMError`.

Tools (Task 9B):

- **Scope.** The effective ``tools_allow`` / ``tools_deny`` (the kernel config's with the
  spec's applied) is the agent registry's ``admit`` check, so it holds for every tool
  whoever registers it: built-ins, plugins, MCP tools (``mcp_<server>_<tool>``) and
  actions.
- **Default deny.** A kernel agent never gets the session tools (``list_sessions``,
  ``read_session``, ``search_sessions``, ``send_session_message``: they reach every
  session on the kernel's shared store), ``my``, ``spawn``, ``create_goal``,
  ``update_goal`` or ``defer_action`` unless ``spec.tools_allow`` names them. Naming a
  bus tool (``spawn``, the goal tools, ``defer_action``, ``send_session_message``)
  raises ``ValueError``: they deliver through the gateway's message bus, which a
  kernel does not run. Hosts should pass an explicit ``tools_allow``.
- **Actions.** ``spec.actions`` (tools or callables, wrapped as ``FunctionTool``) are
  registered when the loop is built; :meth:`Agent.add_action` adds one later. An action
  the scope excludes, whose declared capabilities the agent's policy denies everywhere,
  that declares a network capability offline, or whose name is taken raises
  ``ValueError``; nothing is ever dropped silently.
- **MCP.** ``spec.mcp_servers`` (``MCPServer`` configs, or dicts validated into them)
  connect through an ``MCPProvider`` on the first run (or ``tools``/``fingerprint``),
  from those servers only (never the config files). A server that fails is logged and
  traced as ``mcp.error``; the agent runs without it. ``aclose`` closes them.
- **Plugins.** With ``Kernel(plugins=registry)``, the agent's and its sub-agents' tool
  loaders run in kernel mode (only registry-active plugins load).
- **Offline** (``spec.offline`` or ``env.offline``): no web tools and no tool declaring
  a network capability, no MCP (``mcp_servers`` with offline raises ``ValueError``),
  and :class:`~nanobot.kernel.policy.OfflinePolicy` intersected into the policy, so a
  network request any remaining tool makes is denied with ``OFFLINE_MARKER``. ``exec``
  is not network-sandboxed by this: the container or sandbox is the real boundary.

Streaming: :meth:`Agent.stream` is :meth:`Agent.run` (same span, hooks, session lock,
deadline, budget and cancellation) with the SDK's typed ``StreamEvent`` events,
through the same ``start_streamed_run`` as ``Nanobot.run_streamed``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

from loguru import logger

from nanobot.agent.hook import AgentHookContext, AgentRunHookContext, SDKCaptureHook
from nanobot.kernel.frozen import FrozenMap, freeze, thaw
from nanobot.kernel.llm import Usage
from nanobot.kernel.llm_errors import BudgetExceeded, LLMError, classify
from nanobot.kernel.policy import (
    OfflinePolicy,
    is_network_capability,
    policy_denies_everywhere,
)
from nanobot.kernel.sampling import Sampling
from nanobot.kernel.strict import fully_denied

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path
    from types import TracebackType

    from pydantic import BaseModel

    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools.base import Tool
    from nanobot.agent.tools.mcp import MCPProvider
    from nanobot.config.schema import AgentProfileConfig
    from nanobot.kernel.kernel import Kernel
    from nanobot.kernel.memory import DocStore
    from nanobot.kernel.policy import CapabilityRequest, PermissionPolicy
    from nanobot.kernel.sessions import Session
    from nanobot.kernel.variants import Fingerprint
    from nanobot.sdk.streaming import StreamWiring
    from nanobot.sdk.types import StreamEvent


StopReason = Literal[
    "completed",
    "cancelled",
    "error",
    "ask_user",
    "tool_error",
    "policy_denials",
    "empty_final_response",
    "max_iterations",
    "deadline",
    "budget",
]
_STOP_REASONS: frozenset[str] = frozenset(get_args(StopReason))

# Built-in web tools an offline agent never gets (by name; any other tool declaring
# a network capability is refused by its surface).
_WEB_TOOLS: frozenset[str] = frozenset({"web_search", "web_fetch"})

# Built-ins a kernel agent does not get unless its spec names them in ``tools_allow``:
# the session tools read and write any session on the kernel's shared store (other
# users' or personas' transcripts), and ``my`` inspects and edits the agent's own
# runtime settings.
_KERNEL_DEFAULT_DENY: frozenset[str] = frozenset({
    "list_sessions", "read_session", "search_sessions", "send_session_message",
    "spawn", "create_goal", "update_goal", "defer_action", "my",
})
# Of those, the ones that deliver through the gateway's message bus. A kernel has no
# consumer for it (sub-agent results and goal/deferred wake-ups would be lost), so
# naming one in ``tools_allow`` is an error rather than a silent no-op.
_BUS_TOOLS: frozenset[str] = frozenset({
    "spawn", "create_goal", "update_goal", "defer_action", "send_session_message",
})


# Per-agent state lives in ``<state_dir>/agents/<dirname>/``.
AGENTS_DIRNAME = "agents"
_MAX_AGENT_DIRNAME = 200


def agent_dirname(name: str) -> str:
    """A file-system-safe, injective directory name for agent *name*.

    Percent-encoded (dots too, so ``.``/``..`` cannot escape); a name whose encoding
    is too long becomes an encoded prefix plus a sha256 suffix of the whole name.
    """
    import hashlib
    from urllib.parse import quote

    encoded = quote(name, safe="-_").replace(".", "%2E")
    if len(encoded) <= _MAX_AGENT_DIRNAME:
        return encoded
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    prefix = encoded[:64]
    cut = prefix.rfind("%", len(prefix) - 2)
    if cut != -1:  # never end on half an escape
        prefix = prefix[:cut]
    return f"{prefix}~{digest}"


def agent_memory_dir(state_dir: Path, name: str) -> Path:
    """Where agent *name* keeps MEMORY.md, history.jsonl and its cursors."""
    return state_dir / AGENTS_DIRNAME / agent_dirname(name) / "memory"


def _opt_positive(value: Any, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"RunLimits.{name} must be a positive int or None, got {value!r}")


@dataclass(frozen=True)
class RunLimits:
    """Per-run ceilings. ``None`` = the loop's default (config ``maxToolIterations``)."""

    max_iterations: int | None = None
    max_policy_denials: int = 6
    max_tool_errors: int | None = None
    deadline_s: float | None = None

    def __post_init__(self) -> None:
        _opt_positive(self.max_iterations, "max_iterations")
        _opt_positive(self.max_policy_denials, "max_policy_denials")
        _opt_positive(self.max_tool_errors, "max_tool_errors")
        _check_deadline(self.deadline_s, "RunLimits.deadline_s")


def _check_deadline(value: Any, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds or None, got {value!r}")


def _str_tuple(value: Any, name: str, *, optional: bool = False) -> tuple[str, ...] | None:
    if value is None:
        if optional:
            return None
        raise TypeError(f"AgentSpec.{name} must be a sequence of str, got None")
    if isinstance(value, str):
        raise TypeError(f"AgentSpec.{name} must be a sequence of str, not a str")
    items = tuple(value)
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"AgentSpec.{name} items must be str, got {item!r}")
    return items


def _mcp_server(name: Any, server: Any) -> FrozenMap:
    from nanobot.config.schema import MCPServerConfig

    if not isinstance(name, str) or not name:
        raise TypeError(f"AgentSpec.mcp_servers keys must be non-empty str, got {name!r}")
    if not isinstance(server, MCPServerConfig):
        server = MCPServerConfig.model_validate(
            thaw(server) if isinstance(server, Mapping) else server
        )
    return FrozenMap(server.model_dump())


def _inline_skill(skill: Any) -> FrozenMap:
    from nanobot.config.schema import InlineSkillConfig

    if not isinstance(skill, InlineSkillConfig):
        skill = InlineSkillConfig.model_validate(dict(skill) if isinstance(skill, Mapping)
                                                 else skill)
    return FrozenMap(skill.model_dump())


@dataclass(frozen=True)
class AgentSpec:
    """What an agent is (hashable: ``kernel.agent(spec)`` caches one agent per spec).

    Mappings are stored frozen, sequences as tuples; ``inline_skills`` and
    ``mcp_servers`` values are validated (``InlineSkillConfig`` / ``MCPServer``) and
    stored as frozen dicts. ``actions`` are ``Tool`` instances or callables (see
    :meth:`Agent.add_action`). ``doc_scopes`` names ``kernel.memory`` scopes the
    agent can search (the ``search_documents`` action). ``offline``: see the module
    docstring.
    """

    name: str
    system_prompt: str | None = None
    bootstrap: Mapping[str, str] = field(default_factory=FrozenMap)
    model: str | None = None
    sampling: Sampling | None = None
    tools_allow: tuple[str, ...] | None = None
    tools_deny: tuple[str, ...] = ()
    actions: Sequence[Any] = ()
    mcp_servers: Mapping[str, Any] = field(default_factory=FrozenMap)
    skills_include: tuple[str, ...] | None = None
    skills_exclude: tuple[str, ...] = ()
    inline_skills: Sequence[Any] = ()
    memory: bool = False
    doc_scopes: tuple[str, ...] = ()
    limits: RunLimits = field(default_factory=RunLimits)
    policy: PermissionPolicy | None = None
    offline: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("AgentSpec.name must be a non-empty string")
        if self.system_prompt is not None and not isinstance(self.system_prompt, str):
            raise TypeError("AgentSpec.system_prompt must be a str or None")
        if self.model is not None and (not isinstance(self.model, str) or not self.model):
            raise TypeError("AgentSpec.model must be a non-empty str or None")
        if self.sampling is not None and not isinstance(self.sampling, Sampling):
            raise TypeError(f"AgentSpec.sampling must be a Sampling, got {self.sampling!r}")
        if not isinstance(self.limits, RunLimits):
            raise TypeError(f"AgentSpec.limits must be a RunLimits, got {self.limits!r}")
        if not isinstance(self.bootstrap, Mapping):
            raise TypeError("AgentSpec.bootstrap must be a mapping of str -> str")
        for key, text in self.bootstrap.items():
            if not isinstance(key, str) or not isinstance(text, str):
                raise TypeError(f"AgentSpec.bootstrap maps str -> str, got {key!r}: {text!r}")
        if not isinstance(self.mcp_servers, Mapping):
            raise TypeError("AgentSpec.mcp_servers must be a mapping")
        if self.policy is not None and not callable(getattr(self.policy, "decide", None)):
            raise TypeError("AgentSpec.policy must implement decide(principal, request, ctx)")
        set_ = object.__setattr__
        set_(self, "bootstrap", FrozenMap(self.bootstrap))
        set_(self, "mcp_servers", FrozenMap({
            name: _mcp_server(name, server) for name, server in self.mcp_servers.items()
        }))
        set_(self, "tools_allow", _str_tuple(self.tools_allow, "tools_allow", optional=True))
        set_(self, "tools_deny", _str_tuple(self.tools_deny, "tools_deny"))
        set_(self, "skills_include",
             _str_tuple(self.skills_include, "skills_include", optional=True))
        set_(self, "skills_exclude", _str_tuple(self.skills_exclude, "skills_exclude"))
        set_(self, "doc_scopes", _str_tuple(self.doc_scopes, "doc_scopes"))
        if isinstance(self.actions, (str, bytes)) or not isinstance(self.actions, Sequence):
            raise TypeError("AgentSpec.actions must be a sequence of tools or callables")
        for action in self.actions:
            if not callable(action) and not _is_tool(action):
                raise TypeError(f"AgentSpec.actions items must be Tools or callables, "
                                f"got {action!r}")
        set_(self, "actions", tuple(self.actions))
        set_(self, "inline_skills", tuple(_inline_skill(s) for s in self.inline_skills))
        set_(self, "memory", bool(self.memory))
        set_(self, "offline", bool(self.offline))

    @classmethod
    def from_profile(cls, profile: AgentProfileConfig, *, name: str = "default") -> AgentSpec:
        """Convert a legacy ``AgentProfileConfig``.

        ``model_preset`` -> ``model``; ``system_prompt`` (or the text of
        ``system_prompt_file``) -> ``system_prompt``; ``skills_inline`` ->
        ``inline_skills``; ``memory_enabled`` -> ``memory``; ``vec_collections`` ->
        ``doc_scopes``; tools and skills lists map one to one. ``planning`` and the
        runner tuning ``limits`` have no ``AgentSpec`` equivalent: a profile that sets
        them raises ``ValueError`` instead of losing them.
        """
        from nanobot.config.profile import read_system_prompt_file
        from nanobot.config.schema import AgentProfileConfig

        if not isinstance(profile, AgentProfileConfig):
            profile = AgentProfileConfig.model_validate(profile)
        unmapped = [
            key for key, set_ in (("planning", profile.planning),
                                  ("limits", profile.limits is not None)) if set_
        ]
        if unmapped:
            raise ValueError(
                f"AgentProfileConfig field(s) {unmapped} have no AgentSpec equivalent"
            )
        prompt = profile.system_prompt
        if not prompt and profile.system_prompt_file:
            prompt = read_system_prompt_file(profile.system_prompt_file)
        return cls(
            name=name,
            system_prompt=prompt or None,
            model=profile.model_preset or None,
            tools_allow=profile.tools_allow,
            tools_deny=tuple(profile.tools_deny),
            skills_include=profile.skills_include,
            skills_exclude=tuple(profile.skills_exclude),
            inline_skills=tuple(profile.skills_inline),
            memory=profile.memory_enabled,
            doc_scopes=tuple(profile.vec_collections),
        )


@dataclass(frozen=True)
class AskUser:
    """The model asked the user a question (``stop_reason == "ask_user"``)."""

    question: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolInfo:
    """One tool as the agent's model sees it (description after variant overrides)."""

    name: str
    description: str
    read_only: bool
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class RunResult:
    """The outcome of one :meth:`Agent.run`."""

    content: str
    stop_reason: StopReason
    iterations: int
    usage: Usage
    cost_usd: float | None
    tools_used: tuple[str, ...]
    error: LLMError | str | None
    question: AskUser | None
    session_key: str
    trace_id: str | None
    messages: tuple[Mapping[str, Any], ...]


# -- per-run capture ----------------------------------------------------------------


class _RunCapture(SDKCaptureHook):
    """``SDKCaptureHook`` plus the iteration count, final text, ask_user options and
    a typed error for a provider error response."""

    def __init__(self, *, model: str, provider: str) -> None:
        super().__init__()
        self._model = model
        self._provider = provider
        self.iterations = 0
        self.final_content: str | None = None
        self.options: list[str] | None = None
        self.llm_error: LLMError | None = None

    async def after_iteration(self, context: AgentHookContext) -> None:
        await super().after_iteration(context)
        self.iterations = max(self.iterations, context.iteration + 1)
        if context.final_content is not None:
            self.final_content = context.final_content
        if context.ask_user_options is not None:
            self.options = list(context.ask_user_options)
        response = context.response
        if response is not None and response.finish_reason == "error":
            self.llm_error = classify(response, model=self._model, provider=self._provider)

    async def after_run(self, context: AgentRunHookContext) -> None:
        await super().after_run(context)
        if context.final_content is not None:
            self.final_content = context.final_content


# The run whose ``model.call`` costs a subscriber is summing (events are delivered
# synchronously in the emitting call's context, which is the run's).
_ACTIVE_RUN: ContextVar[_RunCost | None] = ContextVar("moeka_agent_run_cost", default=None)


class _RunCost:
    def __init__(self) -> None:
        self.total: float | None = None

    def on_event(self, event: dict[str, Any]) -> None:
        if _ACTIVE_RUN.get() is not self:
            return
        cost = event.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.total = (self.total or 0.0) + float(cost)


class _Pending:
    """What ``_run`` knew when it was cancelled (see :meth:`Agent.run`)."""

    result: RunResult | None = None


def _combine_policies(*policies: PermissionPolicy | None) -> PermissionPolicy | None:
    present = [p for p in policies if p is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    from nanobot.kernel.policy import IntersectionPolicy

    return IntersectionPolicy(*present)


# -- actions ------------------------------------------------------------------------


def _is_tool(value: Any) -> bool:
    from nanobot.agent.tools.base import Tool

    return isinstance(value, Tool)


def _as_tool(
    action: Any,
    *,
    name: str | None = None,
    description: str | None = None,
    read_only: bool = False,
    capabilities: Sequence[CapabilityRequest | str] = (),
    output_model: type[BaseModel] | None = None,
) -> Tool:
    """*action* itself when it is a ``Tool``, else a ``FunctionTool`` wrapping it."""
    if _is_tool(action):
        if name is not None or description is not None or read_only or capabilities \
                or output_model is not None:
            raise TypeError(
                "name/description/read_only/capabilities/output_model apply to a callable "
                "action; a Tool declares its own"
            )
        return action
    if not callable(action):
        raise TypeError(f"an action is a Tool or a callable, got {action!r}")
    from nanobot.core.function_tool import FunctionTool

    return FunctionTool(
        action, name=name, description=description, read_only=read_only,
        capabilities=capabilities, output_model=output_model,
    )


def _surface(tool: Any) -> frozenset[str] | None:
    """``tool.capability_surface()`` (``None`` = unknown, also when it raises)."""
    surface_fn = getattr(tool, "capability_surface", None)
    try:
        return surface_fn() if callable(surface_fn) else None
    except Exception:  # noqa: BLE001 - a broken declaration is an unknown surface
        return None


def _declares_network(tool: Any) -> bool:
    return any(is_network_capability(c) for c in _surface(tool) or ())


# -- streaming -----------------------------------------------------------------------

# Stop reasons a stream reports as ``run.failed`` (the result is still attached).
_FAILED_STOPS: frozenset[str] = frozenset({"error", "tool_error", "deadline", "budget",
                                           "cancelled"})


def _final_event(result: RunResult) -> StreamEvent:
    """``run.completed``, or ``run.failed`` for a failed stop reason, carrying *result*."""
    from nanobot.sdk.types import (
        STREAM_EVENT_RUN_COMPLETED,
        STREAM_EVENT_RUN_FAILED,
        StreamEvent,
    )

    failed = result.stop_reason in _FAILED_STOPS
    error = None
    if failed:
        error = str(result.error) if result.error is not None else result.stop_reason
    return StreamEvent(
        type=STREAM_EVENT_RUN_FAILED if failed else STREAM_EVENT_RUN_COMPLETED,
        content=result.content,
        result=result,  # type: ignore[arg-type] - the kernel RunResult
        usage=result.usage,  # type: ignore[arg-type] - the kernel Usage
        error=error,
        metadata={
            "stop_reason": result.stop_reason,
            "session_key": result.session_key,
            "trace_id": result.trace_id,
        },
    )


class _StreamOut:
    """Where a stream's generator leaves its result (never a reference to its owner)."""

    __slots__ = ("error", "result")

    def __init__(self) -> None:
        self.result: RunResult | None = None
        self.error: BaseException | None = None

    def final(self) -> RunResult:
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise RuntimeError("stream was closed before it completed")
        return self.result


class AgentStream:
    """The typed ``StreamEvent`` events of one :meth:`Agent.stream` run.

    An async iterator (``run.started``, ``text.delta``/``text.completed``,
    ``reasoning.*``, ``tool.started``/``tool.completed``/``tool.failed``, then
    ``run.completed`` or ``run.failed``); ``await result()`` drains it and returns the
    :class:`RunResult` ``run()`` would have. The run starts on the first iteration.
    Use ``async with`` (or ``aclose()``) so an early exit cancels the run; a stream
    dropped mid-run without closing is finalised by the kernel loop, which cancels it.
    """

    def __init__(self, agent: Agent, gen: AsyncIterator[StreamEvent], out: _StreamOut) -> None:
        self._agent = agent
        self._gen = gen
        self._out = out
        self._finished = False

    def __aiter__(self) -> AgentStream:
        return self

    async def __anext__(self) -> StreamEvent:
        if self._finished:
            raise StopAsyncIteration
        gen = self._gen

        async def _next() -> StreamEvent:
            return await gen.__anext__()

        try:
            return await self._agent._on_loop(_next())
        except BaseException:
            self._finished = True
            raise

    async def result(self) -> RunResult:
        """Drain the remaining events and return the run's :class:`RunResult`."""
        async for _ in self:
            pass
        return self._out.final()

    async def aclose(self) -> None:
        """Stop the stream early (cancels the run). Idempotent."""
        self._finished = True
        if self._agent._kernel.closed:
            return  # the kernel close already cancelled the run
        gen = self._gen

        async def _close() -> None:
            await gen.aclose()  # type: ignore[attr-defined]

        await self._agent._on_loop(_close())

    async def __aenter__(self) -> AgentStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


class SyncAgentStream:
    """Sync twin of :class:`AgentStream` (:meth:`Agent.stream_sync`).

    Iterate the events, then ``result()``; use ``with`` (or ``close()``) so an early
    exit cancels the run.
    """

    def __init__(self, stream: AgentStream) -> None:
        self._stream = stream
        self._iter: Iterator[StreamEvent] = stream._agent._kernel._bridge.iterate(stream._gen)

    def __iter__(self) -> SyncAgentStream:
        return self

    def __next__(self) -> StreamEvent:
        return next(self._iter)

    def result(self) -> RunResult:
        """Drain the remaining events and return the run's :class:`RunResult`."""
        for _ in self._iter:
            pass
        return self._stream._out.final()

    def close(self) -> None:
        """Stop the stream early (cancels the run). Idempotent."""
        close = getattr(self._iter, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> SyncAgentStream:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _check_run_args(sampling: Any, deadline_s: Any) -> None:
    if sampling is not None and not isinstance(sampling, Sampling):
        raise TypeError(f"sampling must be a Sampling, got {type(sampling).__name__}")
    _check_deadline(deadline_s, "deadline_s")


class Agent:
    """An agent on a kernel. Build with ``kernel.agent(spec)``; see the module docstring."""

    def __init__(self, kernel: Kernel, spec: AgentSpec) -> None:
        if spec.limits.max_tool_errors is not None:
            raise NotImplementedError(
                "RunLimits.max_tool_errors: the runner has no tool-error ceiling yet"
            )
        self._kernel = kernel
        self._spec = spec
        self._offline = spec.offline or kernel.env.offline
        if self._offline and spec.mcp_servers:
            raise ValueError(
                f"agent {spec.name!r} is offline"
                + (" (env.offline)" if not spec.offline else "")
                + ": it cannot have mcp_servers"
            )
        # The effective tool scope: the kernel config's lists with the spec's applied
        # (as ``apply_profile`` merges them), plus the web tools when offline, plus
        # the kernel default-deny set minus what the spec's tools_allow names.
        named = frozenset(spec.tools_allow or ())
        unsupported = sorted(named & _BUS_TOOLS)
        if unsupported:
            raise ValueError(
                f"agent {spec.name!r}: tools {unsupported} need the gateway's message bus "
                "(sub-agent results, goals and deferred actions are delivered through it) "
                "and are not supported on a kernel agent"
            )
        defaults = kernel.env.config.agents.defaults
        allow = spec.tools_allow if spec.tools_allow is not None else defaults.tools_allow
        self._tools_allow: frozenset[str] | None = (
            None if allow is None else frozenset(allow)
        )
        self._tools_deny: frozenset[str] = frozenset({
            *defaults.tools_deny, *spec.tools_deny, *(_WEB_TOOLS if self._offline else ()),
            *(_KERNEL_DEFAULT_DENY - named),
        })
        self._policy = _combine_policies(
            kernel.policy, spec.policy, OfflinePolicy() if self._offline else None,
        )
        self._actions: list[Tool] = []
        for action in spec.actions:
            tool = _as_tool(action)
            if any(t.name == tool.name for t in self._actions):
                raise ValueError(f"AgentSpec.actions has two actions named {tool.name!r}")
            self._check_action(tool)
            self._actions.append(tool)
        if spec.doc_scopes:
            from nanobot.kernel.memory import search_documents_tool

            tool = search_documents_tool(kernel.memory, spec.doc_scopes)
            if any(t.name == tool.name for t in self._actions):
                raise ValueError(
                    f"AgentSpec.actions has an action named {tool.name!r}, which "
                    "doc_scopes also provides"
                )
            self._check_action(tool)
            self._actions.append(tool)
        self._loop: AgentLoop | None = None
        self._route: Any = None
        self._memory: DocStore | None = None
        self._mcp: MCPProvider | None = None
        self._mcp_ready = False
        self._mcp_lock: asyncio.Lock | None = None
        self._runs: set[asyncio.Task[Any]] = set()
        self._closed = False

    @property
    def spec(self) -> AgentSpec:
        return self._spec

    @property
    def closed(self) -> bool:
        return self._closed

    # -- building (on the kernel loop thread) ----------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"agent {self._spec.name!r} is closed")
        if self._kernel.closed:
            raise RuntimeError("kernel is closed")

    def _ensure_loop(self) -> AgentLoop:
        """The agent's loop, built on first use (always on the kernel loop thread)."""
        self._check_open()
        if self._loop is None:
            self._loop = self._build()
        return self._loop

    def _build(self) -> AgentLoop:
        from nanobot.agent.loop import AgentLoop
        from nanobot.agent.tools.registry import ToolRegistry
        from nanobot.config.profile import (
            apply_profile,
            build_bootstrap_overrides,
            build_inline_skills,
        )
        from nanobot.config.schema import AgentProfileConfig, InlineSkillConfig

        kernel, spec = self._kernel, self._spec
        env = kernel.env
        route = kernel.llm._route(spec.model)
        profile = AgentProfileConfig(
            system_prompt=spec.system_prompt,
            tools_allow=list(spec.tools_allow) if spec.tools_allow is not None else None,
            tools_deny=list(spec.tools_deny),
            skills_include=(
                list(spec.skills_include) if spec.skills_include is not None else None
            ),
            skills_exclude=list(spec.skills_exclude),
            skills_inline=[InlineSkillConfig.model_validate(s.as_dict())
                           for s in spec.inline_skills],
            memory_enabled=spec.memory,
            vec_collections=list(spec.doc_scopes),
        )
        cfg, prof = apply_profile(env.config, profile)
        d = cfg.agents.defaults
        d.tools_allow = sorted(self._tools_allow) if self._tools_allow is not None else None
        d.tools_deny = sorted(self._tools_deny)
        # No preset selection: the loop would build its own (unmetered) provider for
        # it. The kernel pool's provider and resolved model are passed explicitly.
        cfg.agents.defaults.model_preset = None
        if spec.limits.max_iterations is not None:
            cfg.agents.defaults.max_tool_iterations = spec.limits.max_iterations
        extra: dict[str, Any] = {
            "provider": route.provider,
            "model": route.model,
            "session_manager": kernel._session_manager(),
            # Per-agent memory files under state_dir (never shared, never in work_dir);
            # only a memory=True agent's prompt reads them back.
            "memory_dir": agent_memory_dir(env.paths.state_dir, spec.name),
            "inject_memory": spec.memory,
            "variant": kernel.variant,
            "max_policy_denials": spec.limits.max_policy_denials,
            "plugin_registry": kernel.plugins,
        }
        if route.spec is not None and route.spec.context_window is not None:
            extra["context_window_tokens"] = route.spec.context_window
        overrides = build_bootstrap_overrides(prof, dict(spec.bootstrap))
        if overrides:
            extra["bootstrap_overrides"] = overrides
        inline = build_inline_skills(prof, None)
        if inline:
            extra["inline_skills"] = inline
        if spec.memory:
            # The kernel's store for this agent, pinned: the loop uses its VecStore
            # directly, so it must never be released by the LRU under the loop.
            # Pinned once per agent (a retried build reuses the pin).
            if self._memory is None:
                store = kernel.memory(f"agent:{spec.name}")
                store._pin()
                self._memory = store
            extra["vec_store"] = self._memory._pin_store()
            extra["vec_config"] = cfg.agents.defaults.vec
        # Runtime preset switches (/model, the my tool, the Dream model override)
        # resolve through the kernel pool too, so they stay metered and ledgered.
        extra["preset_snapshot_loader"] = self._preset_loader(cfg)
        registry = ToolRegistry()
        registry.admit = self._admits
        loop = AgentLoop.from_config(
            cfg,
            tool_registry=registry,
            env=kernel.core_env,
            policy=self._policy,
            **extra,
        )
        self._route = route
        for tool in self._actions:
            self._register_action(loop, tool, replace=False)
        if spec.mcp_servers:
            from nanobot.agent.tools.mcp import MCPProvider
            from nanobot.config.schema import MCPServerConfig

            servers = {
                name: MCPServerConfig.model_validate(server.as_dict())
                for name, server in spec.mcp_servers.items()
            }
            core = kernel.core_env
            # An explicit in-memory loader: never the ambient one (it reads config files).
            self._mcp = MCPProvider(
                servers, registry,
                server_loader=lambda: dict(servers),
                data_dir=core.paths.data_dir,
                media_dir=core.paths.media_dir,
                base_env=core.exec_base_env,
            )
        return loop

    # -- tool scope and actions --------------------------------------------------------

    def _admits(self, tool: Tool) -> bool:
        """The agent registry's ``admit`` check: the tool scope, and offline's no-net."""
        name = tool.name
        if name in self._tools_deny:
            return False
        if self._tools_allow is not None and name not in self._tools_allow:
            return False
        return not (self._offline and _declares_network(tool))

    def _check_action(self, tool: Tool) -> None:
        """Raise ``ValueError`` when *tool* can never run on this agent."""
        name = tool.name
        if name in self._tools_deny:
            raise ValueError(f"action {name!r} is excluded by the agent's tools_deny")
        if self._tools_allow is not None and name not in self._tools_allow:
            raise ValueError(f"action {name!r} is not in the agent's tools_allow")
        surface = _surface(tool)
        if self._offline and _declares_network(tool):
            raise ValueError(
                f"action {name!r} declares a network capability and the agent is offline"
            )
        policy = self._policy
        if policy is None or not surface:
            return
        from nanobot.core.function_tool import FunctionTool

        if isinstance(tool, FunctionTool):
            # Every call requests the whole static declaration: one capability denied
            # everywhere denies every call.
            denied = sorted(c for c in surface if policy_denies_everywhere(policy, c))
        else:
            denied = sorted(surface) if fully_denied(policy, surface) else []
        if denied:
            raise ValueError(
                f"action {name!r}: the agent's policy denies {denied} for every resource"
            )

    def _register_action(self, loop: AgentLoop, tool: Tool, *, replace: bool) -> None:
        self._check_action(tool)
        name = tool.name
        if loop.tools.has(name) and not replace:
            raise ValueError(
                f"agent {self._spec.name!r} already has a tool named {name!r} "
                "(pass replace=True to replace it)"
            )
        if not loop.tools.register(tool):
            raise ValueError(
                f"action {name!r} was not registered (strict mode drops a tool whose "
                "capabilities the policy denies everywhere)"
            )

    def add_action(
        self,
        action: Tool | Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        read_only: bool = False,
        capabilities: Sequence[CapabilityRequest | str] = (),
        output_model: type[BaseModel] | None = None,
        replace: bool = False,
    ) -> str:
        """Register *action* (a ``Tool``, or a callable wrapped as a ``FunctionTool``
        with these options) and return its name. Builds the agent if needed; safe from
        any thread (it runs on the kernel loop).

        Raises ``ValueError`` when the tool scope excludes it, the agent's policy
        denies its declared capabilities everywhere, it declares a network capability
        while offline, or a tool of that name exists (unless ``replace=True``).
        """
        self._check_open()
        tool = _as_tool(
            action, name=name, description=description, read_only=read_only,
            capabilities=capabilities, output_model=output_model,
        )

        def add() -> str:
            self._register_action(self._ensure_loop(), tool, replace=replace)
            return tool.name

        if threading.current_thread() is self._kernel._bridge._thread:
            return add()

        async def add_on_loop() -> str:
            return add()

        return self._sync(add_on_loop())

    async def _ensure_mcp(self) -> None:
        """Connect the spec's MCP servers once (on the kernel loop); trace failures."""
        provider = self._mcp
        if provider is None or self._mcp_ready:
            return
        if self._mcp_lock is None:
            self._mcp_lock = asyncio.Lock()
        async with self._mcp_lock:
            if self._mcp_ready:
                return
            await provider.connect()
            self._mcp_ready = True
            status = provider.runtime_status()
            for server in sorted(self._spec.mcp_servers):
                state = status.get(server)
                if state == "connected":
                    continue
                error = ("waiting for OAuth authorization" if state is None
                         else "failed to connect (see the log for the cause)")
                logger.warning("agent {}: MCP server {!r} {}; running without it",
                               self._spec.name, server, error)
                self._kernel.trace.emit({
                    "event": "mcp.error", "agent": self._spec.name, "server": server,
                    "error": error,
                })

    def _preset_loader(self, cfg: Any) -> Callable[[str], Any]:
        """A ``preset_snapshot_loader`` backed by the kernel LLM pool.

        Preset ``"default"`` is ``env.default_model``; any other name must be a kernel
        model alias (``env.models`` / ``register_provider``), else ``KeyError``.
        """
        from nanobot.providers.factory import ProviderSnapshot

        kernel = self._kernel

        def load(name: str) -> ProviderSnapshot:
            alias = kernel.env.default_model if name == "default" else name
            route = kernel.llm._route(alias)
            if route.alias is None:
                raise KeyError(f"model preset {name!r} is not a model of this kernel")
            preset = cfg.model_presets.get(name) or cfg.resolve_default_preset()
            window = preset.context_window_tokens
            if route.spec is not None and route.spec.context_window is not None:
                window = route.spec.context_window
            return ProviderSnapshot(
                provider=route.provider,
                model=route.model,
                context_window_tokens=window,
                signature=("kernel", route.alias, route.model),
                generation=preset.to_generation_settings(),
                model_preset=name,
            )

        return load

    async def _on_loop(self, coro: Any) -> Any:
        """Await *coro* on the kernel loop thread (directly when already on it)."""
        bridge = self._kernel._bridge
        if threading.current_thread() is bridge._thread:
            return await coro
        return await asyncio.wrap_future(bridge.submit(coro))

    def _sync(self, coro: Any) -> Any:
        return self._kernel._bridge.run(coro)

    # -- introspection ---------------------------------------------------------------

    async def _tools(self) -> list[ToolInfo]:
        loop = self._ensure_loop()
        await self._ensure_mcp()
        infos = []
        for schema in loop.tools.get_definitions():
            fn = schema.get("function", {})
            tool = loop.tools.get(fn.get("name", ""))
            infos.append(ToolInfo(
                name=fn.get("name", ""),
                description=fn.get("description") or "",
                read_only=bool(getattr(tool, "read_only", False)),
                parameters=FrozenMap(fn.get("parameters") or {}),
            ))
        return infos

    @property
    def tools(self) -> list[ToolInfo]:
        """The tools the model sees (builds the agent on first use)."""
        self._check_open()
        return self._sync(self._tools())

    async def _fingerprint(self) -> Fingerprint:
        from nanobot.kernel.variants import fingerprint

        loop = self._ensure_loop()
        await self._ensure_mcp()
        return fingerprint(loop, model=self._route.model, sampling=self._spec.sampling)

    def fingerprint(self) -> Fingerprint:
        """``variants.fingerprint`` of this agent's loop, resolved model and spec sampling."""
        self._check_open()
        return self._sync(self._fingerprint())

    # -- runs ------------------------------------------------------------------------

    async def run(
        self,
        message: str,
        *,
        session: Session | str | None = None,
        media: Sequence[str] = (),
        sampling: Sampling | None = None,
        deadline_s: float | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Run one turn. ``sampling`` replaces the spec's sampling for this run only;
        ``deadline_s`` overrides ``spec.limits.deadline_s``; ``tags`` are added to the
        run's span (so to every event and to budget ``per_tag`` caps). ``session`` is a
        key or a ``kernel.sessions`` handle and defaults to ``"agent:<name>"``.

        Returns a :class:`RunResult` for every stop reason. Cancelling the task
        awaiting this raises ``CancelledError`` as usual; closing the agent or kernel
        mid-run instead returns ``stop_reason="cancelled"``.
        """
        self._check_open()
        session = self._session_key(session)
        pending = _Pending()
        coro = self._run(message, session, media, sampling, deadline_s, tags, pending)
        try:
            return await self._on_loop(coro)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if pending.result is not None and task is not None and not task.cancelling():
                return pending.result  # the run was cancelled, not this caller
            raise

    def run_sync(
        self,
        message: str,
        *,
        session: Session | str | None = None,
        media: Sequence[str] = (),
        sampling: Sampling | None = None,
        deadline_s: float | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Blocking :meth:`run` (safe from any thread, including inside ``asyncio.run``)."""
        import concurrent.futures

        self._check_open()
        session = self._session_key(session)
        pending = _Pending()
        try:
            return self._sync(
                self._run(message, session, media, sampling, deadline_s, tags, pending)
            )
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            if pending.result is not None:
                return pending.result
            raise

    def stream(
        self,
        message: str,
        *,
        session: Session | str | None = None,
        media: Sequence[str] = (),
        sampling: Sampling | None = None,
        deadline_s: float | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> AgentStream:
        """:meth:`run` as an :class:`AgentStream` of typed ``StreamEvent`` events.

        Same arguments, span, hooks, session lock, deadline, budget and cancellation
        as :meth:`run`; ``await stream.result()`` is the :class:`RunResult`. The final
        event is ``run.failed`` for the stop reasons error, tool_error, deadline,
        budget and cancelled (``event.result`` is still the RunResult), else
        ``run.completed``. Closing the stream early cancels the run.
        """
        self._check_open()
        session = self._session_key(session)
        _check_run_args(sampling, deadline_s)
        out = _StreamOut()
        gen = self._stream_events(message, session, media, sampling, deadline_s, tags, out)
        return AgentStream(self, gen, out)

    def stream_sync(
        self,
        message: str,
        *,
        session: Session | str | None = None,
        media: Sequence[str] = (),
        sampling: Sampling | None = None,
        deadline_s: float | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> SyncAgentStream:
        """Sync :meth:`stream` (safe from any thread): iterate, then ``result()``."""
        return SyncAgentStream(self.stream(
            message, session=session, media=media, sampling=sampling,
            deadline_s=deadline_s, tags=tags,
        ))

    async def _stream_events(
        self,
        message: str,
        session: str | None,
        media: Sequence[str],
        sampling: Sampling | None,
        deadline_s: float | None,
        tags: Mapping[str, str] | None,
        out: _StreamOut,
    ) -> AsyncIterator[StreamEvent]:
        """Runs on the kernel loop: yields the run's events, then stores its result.

        *out* is a plain holder, not the ``AgentStream``: the generator must not
        reference its owner, so a dropped stream is finalised (and its run cancelled)
        as soon as it is unreachable, not at some later cyclic GC.
        """
        from nanobot.sdk.streaming import start_streamed_run

        async def body(wiring: StreamWiring) -> RunResult:
            pending = _Pending()
            try:
                return await self._run(
                    message, session, media, sampling, deadline_s, tags, pending,
                    stream=wiring,
                )
            except asyncio.CancelledError:
                if pending.result is not None and self._closed:
                    return pending.result  # the agent (or kernel) closed mid-run
                raise

        run = start_streamed_run(
            body,
            final_event=_final_event,
            started_metadata={
                "session_key": session or f"agent:{self._spec.name}",
                "agent": self._spec.name,
            },
        )
        events = run.stream_events()
        try:
            async for event in events:
                yield event
            out.result = await run.wait()
        except Exception as exc:
            out.error = exc
            raise
        finally:
            await events.aclose()
            if not run.done:
                await run.aclose()

    def _session_key(self, session: Session | str | None) -> str | None:
        """The key for a run's ``session=`` (a handle must be on this agent's kernel)."""
        from nanobot.kernel.sessions import Session

        if session is None or isinstance(session, str):
            return session
        if isinstance(session, Session):
            if session._kernel is not self._kernel:
                raise ValueError(f"{session!r} belongs to another kernel")
            return session.key
        raise TypeError(f"session must be a Session, str or None, got {type(session).__name__}")

    def _request_extras(self, sampling: Sampling | None) -> Any:
        from nanobot.providers.base import RequestExtras

        route = self._route
        explicit = sampling if sampling is not None else self._spec.sampling
        defaults = self._kernel.llm._default_sampling(route)
        if (explicit is None or not explicit.set_fields()) and defaults is None:
            return None
        return RequestExtras(
            sampling=explicit if explicit is not None and explicit.set_fields() else None,
            default_sampling=defaults,
        )

    async def _run(
        self,
        message: str,
        session: str | None,
        media: Sequence[str],
        sampling: Sampling | None,
        deadline_s: float | None,
        tags: Mapping[str, str] | None,
        pending: _Pending,
        stream: StreamWiring | None = None,
    ) -> RunResult:
        from nanobot.agent.runner import turn_request_extras
        from nanobot.kernel.trace_hook import TraceHook

        _check_run_args(sampling, deadline_s)
        loop = self._ensure_loop()
        task = asyncio.current_task()
        if task is not None:
            self._runs.add(task)
        spec = self._spec
        session_key = session or f"agent:{spec.name}"
        deadline = deadline_s if deadline_s is not None else spec.limits.deadline_s
        route = self._route
        capture = _RunCapture(model=route.model, provider=route.provider.provider_name)
        cost = _RunCost()
        span_tags = {**dict(tags or {}), "agent": spec.name}
        run_span = self._kernel.trace.span("agent.run", **span_tags)
        unsubscribe = self._kernel.trace.subscribe("model.call", cost.on_event)

        def result(stop: str, *, content: str = "", error: LLMError | str | None = None,
                   question: AskUser | None = None) -> RunResult:
            if stop not in _STOP_REASONS:
                # A runner stop reason this API does not know: never report success.
                logger.warning("agent {}: unknown runner stop reason {!r}; reported as "
                               "'error'", spec.name, stop)
                if error is None:
                    error = f"unknown stop reason {stop!r}"
                stop = "error"
            return RunResult(
                content=content,
                stop_reason=stop,  # type: ignore[arg-type]
                iterations=capture.iterations,
                usage=Usage.from_llm_usage(capture.usage),
                cost_usd=cost.total,
                tools_used=tuple(capture.tools_used),
                error=error,
                question=question,
                session_key=session_key,
                trace_id=run_span.trace_id,
                messages=tuple(freeze(m) for m in capture.messages),
            )

        timeout = asyncio.timeout(deadline)

        def trace_stop(context: AgentRunHookContext) -> str | None:
            # run.completed must say what the RunResult says.
            if timeout.expired():
                return "deadline"
            if isinstance(context.exception, BudgetExceeded):
                return "budget"
            return None

        hooks = [capture, TraceHook(
            self._kernel.trace, session_key=session_key, model=route.model,
            stop_reason=trace_stop,
        )]
        stream_kwargs: dict[str, Any] = {}
        if stream is not None:
            hooks.append(stream.hook)
            stream_kwargs = stream.process_kwargs()
        try:
            with run_span, turn_request_extras(self._request_extras(sampling)):
                token = _ACTIVE_RUN.set(cost)
                try:
                    try:
                        async with timeout:
                            await self._ensure_mcp()
                            # Serialise same-key runs across the kernel's agents: they
                            # share one SessionManager (and its cached Session objects).
                            async with self._kernel._session_lock(session_key):
                                response = await loop.process_direct(
                                    message,
                                    session_key=session_key,
                                    media=list(media) or None,
                                    hooks=hooks,
                                    **stream_kwargs,
                                )
                    except BudgetExceeded as exc:
                        return result("budget", error=exc)
                    except LLMError as exc:  # before TimeoutError: LLMTimeoutError is one
                        return result("error", error=exc)
                    except TimeoutError:
                        if not timeout.expired():
                            raise
                        return result("deadline", error=f"run exceeded deadline_s={deadline}")
                    except asyncio.CancelledError:
                        pending.result = result("cancelled")
                        raise
                finally:
                    _ACTIVE_RUN.reset(token)
        finally:
            unsubscribe()
            self._drain_outbound(loop)
            if task is not None:
                self._runs.discard(task)

        content = (response.content if response is not None else None) or ""
        stop = capture.stop_reason or "completed"
        error: LLMError | str | None = capture.error
        question = None
        if stop == "error" and capture.llm_error is not None:
            error = capture.llm_error
        elif stop == "ask_user":
            question = AskUser(
                question=capture.final_content or content,
                options=tuple(capture.options or ()),
            )
        return result(stop, content=content, error=error, question=question)

    @staticmethod
    def _drain_outbound(loop: AgentLoop) -> None:
        """Empty the loop's outbound bus queue (nothing consumes it here)."""
        queue = getattr(getattr(loop, "bus", None), "outbound", None)
        if queue is None:
            return
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    # -- lifecycle -------------------------------------------------------------------

    async def _aclose(self) -> None:
        """Cancel in-flight runs, then close the loop (on the kernel loop thread)."""
        if self._closed:
            return
        self._closed = True
        runs = [t for t in self._runs if not t.done()]
        for task in runs:
            task.cancel()
        if runs:
            await asyncio.gather(*runs, return_exceptions=True)
        loop, self._loop = self._loop, None
        mcp, self._mcp = self._mcp, None
        try:
            if mcp is not None:
                try:
                    await mcp.aclose()
                except Exception as exc:  # noqa: BLE001 - still close the loop
                    logger.warning("agent {}: closing MCP servers failed: {!r}",
                                   self._spec.name, exc)
            if loop is not None:
                await loop.aclose()
        finally:
            # The store is the kernel's (it closes it); only the pin is the agent's.
            memory, self._memory = self._memory, None
            if memory is not None:
                memory._unpin()

    async def aclose(self) -> None:
        """Cancel in-flight runs (they return ``stop_reason="cancelled"``) and close
        the agent's loop. Idempotent. The kernel's session store stays open."""
        if self._closed or self._kernel.closed:
            self._closed = True
            return
        await self._on_loop(self._aclose())

    def __repr__(self) -> str:
        state = "closed" if self._closed else ("built" if self._loop is not None else "lazy")
        return f"Agent({self._spec.name!r}, {state})"


__all__ = [
    "Agent",
    "AgentSpec",
    "AgentStream",
    "AskUser",
    "RunLimits",
    "RunResult",
    "StopReason",
    "SyncAgentStream",
    "ToolInfo",
]
