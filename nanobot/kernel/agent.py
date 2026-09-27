"""Agents (Task 9A): ``kernel.agent(AgentSpec(...))`` runs an agent loop on the kernel.

An :class:`Agent` is an ``AgentLoop`` built from the kernel's environment with the
spec applied, living on the kernel's loop thread:

- **Config.** ``env.config`` scoped by the spec, through the same profile compiler
  ``MoekaCore.create(profile=...)`` uses (:mod:`nanobot.config.profile`): tool
  allow/deny, skills include/exclude/inline, persona (``system_prompt`` fills
  ``AGENTS.md`` unless ``bootstrap`` has one), memory.
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

Not built yet (Task 9B): ``actions``, ``mcp_servers``, ``offline`` and streaming.
Setting them raises ``NotImplementedError`` rather than being ignored.
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
from nanobot.kernel.frozen import FrozenMap, freeze
from nanobot.kernel.llm import Usage
from nanobot.kernel.llm_errors import BudgetExceeded, LLMError, classify
from nanobot.kernel.sampling import Sampling

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.schema import AgentProfileConfig
    from nanobot.kernel.kernel import Kernel
    from nanobot.kernel.policy import PermissionPolicy
    from nanobot.kernel.variants import Fingerprint


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

_PART_B = "is not implemented yet (kernel Task 9B)"


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


def _inline_skill(skill: Any) -> FrozenMap:
    from nanobot.config.schema import InlineSkillConfig

    if not isinstance(skill, InlineSkillConfig):
        skill = InlineSkillConfig.model_validate(dict(skill) if isinstance(skill, Mapping)
                                                 else skill)
    return FrozenMap(skill.model_dump())


@dataclass(frozen=True)
class AgentSpec:
    """What an agent is (hashable: ``kernel.agent(spec)`` caches one agent per spec).

    Mappings are stored frozen, sequences as tuples; ``inline_skills`` are validated
    ``InlineSkillConfig`` values stored as frozen dicts. ``doc_scopes`` is kept for
    the kernel's document memory (Task 11). ``actions``, ``mcp_servers`` and
    ``offline`` are accepted here but raise ``NotImplementedError`` when the agent
    is built (Task 9B).
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
        set_(self, "mcp_servers", FrozenMap(self.mcp_servers))
        set_(self, "tools_allow", _str_tuple(self.tools_allow, "tools_allow", optional=True))
        set_(self, "tools_deny", _str_tuple(self.tools_deny, "tools_deny"))
        set_(self, "skills_include",
             _str_tuple(self.skills_include, "skills_include", optional=True))
        set_(self, "skills_exclude", _str_tuple(self.skills_exclude, "skills_exclude"))
        set_(self, "doc_scopes", _str_tuple(self.doc_scopes, "doc_scopes"))
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


class Agent:
    """An agent on a kernel. Build with ``kernel.agent(spec)``; see the module docstring."""

    def __init__(self, kernel: Kernel, spec: AgentSpec) -> None:
        if spec.actions:
            raise NotImplementedError(f"AgentSpec.actions {_PART_B}")
        if spec.mcp_servers:
            raise NotImplementedError(f"AgentSpec.mcp_servers {_PART_B}")
        if spec.offline:
            raise NotImplementedError(f"AgentSpec.offline {_PART_B}")
        if spec.limits.max_tool_errors is not None:
            raise NotImplementedError(
                "RunLimits.max_tool_errors: the runner has no tool-error ceiling yet"
            )
        self._kernel = kernel
        self._spec = spec
        self._loop: AgentLoop | None = None
        self._route: Any = None
        self._vec_store: Any = None
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
        # No preset selection: the loop would build its own (unmetered) provider for
        # it. The kernel pool's provider and resolved model are passed explicitly.
        cfg.agents.defaults.model_preset = None
        if spec.limits.max_iterations is not None:
            cfg.agents.defaults.max_tool_iterations = spec.limits.max_iterations
        extra: dict[str, Any] = {
            "provider": route.provider,
            "model": route.model,
            "session_manager": kernel._session_manager(),
            "variant": kernel.variant,
            "max_policy_denials": spec.limits.max_policy_denials,
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
            from nanobot.core.vec import open_vec_store

            vec = cfg.agents.defaults.vec
            self._vec_store = open_vec_store(
                env.paths.work_dir / "memory" / "vec.db",
                model=vec.embedding_model,
                log_retrievals=vec.log_retrievals,
            )
            extra["vec_config"] = vec
            extra["vec_store"] = self._vec_store
        # Runtime preset switches (/model, the my tool, the Dream model override)
        # resolve through the kernel pool too, so they stay metered and ledgered.
        extra["preset_snapshot_loader"] = self._preset_loader(cfg)
        loop = AgentLoop.from_config(
            cfg,
            tool_registry=ToolRegistry(),
            env=kernel.core_env,
            policy=_combine_policies(kernel.policy, spec.policy),
            **extra,
        )
        self._route = route
        return loop

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
        session: str | None = None,
        media: Sequence[str] = (),
        sampling: Sampling | None = None,
        deadline_s: float | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Run one turn. ``sampling`` replaces the spec's sampling for this run only;
        ``deadline_s`` overrides ``spec.limits.deadline_s``; ``tags`` are added to the
        run's span (so to every event and to budget ``per_tag`` caps). ``session``
        defaults to ``"agent:<name>"``.

        Returns a :class:`RunResult` for every stop reason. Cancelling the task
        awaiting this raises ``CancelledError`` as usual; closing the agent or kernel
        mid-run instead returns ``stop_reason="cancelled"``.
        """
        self._check_open()
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
        session: str | None = None,
        media: Sequence[str] = (),
        sampling: Sampling | None = None,
        deadline_s: float | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Blocking :meth:`run` (safe from any thread, including inside ``asyncio.run``)."""
        import concurrent.futures

        self._check_open()
        pending = _Pending()
        try:
            return self._sync(
                self._run(message, session, media, sampling, deadline_s, tags, pending)
            )
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            if pending.result is not None:
                return pending.result
            raise

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
    ) -> RunResult:
        from nanobot.agent.runner import turn_request_extras
        from nanobot.kernel.trace_hook import TraceHook

        if sampling is not None and not isinstance(sampling, Sampling):
            raise TypeError(f"sampling must be a Sampling, got {type(sampling).__name__}")
        _check_deadline(deadline_s, "deadline_s")
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
            return RunResult(
                content=content,
                stop_reason=stop if stop in _STOP_REASONS else "completed",  # type: ignore[arg-type]
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
        try:
            with run_span, turn_request_extras(self._request_extras(sampling)):
                token = _ACTIVE_RUN.set(cost)
                try:
                    try:
                        async with timeout:
                            # Serialise same-key runs across the kernel's agents: they
                            # share one SessionManager (and its cached Session objects).
                            async with self._kernel._session_lock(session_key):
                                response = await loop.process_direct(
                                    message,
                                    session_key=session_key,
                                    media=list(media) or None,
                                    hooks=hooks,
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
        try:
            if loop is not None:
                await loop.aclose()
        finally:
            vec, self._vec_store = self._vec_store, None
            if vec is not None:
                try:
                    vec.close()
                except Exception:  # noqa: BLE001 - closing must not fail the agent close
                    logger.warning("agent {}: closing its vec store failed", self._spec.name)

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
    "AskUser",
    "RunLimits",
    "RunResult",
    "StopReason",
    "ToolInfo",
]
