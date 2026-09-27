"""Subagent manager for background task execution."""

import asyncio
import json
import time
import uuid
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.runner import DEFAULT_MAX_POLICY_DENIALS, AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import (
    RequestContext,
    ToolContext,
    bind_request_context,
    reset_request_context,
)
from nanobot.agent.tools.exec_session import ExecSessionManager
from nanobot.agent.tools.file_state import FileStates
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults, ToolsConfig
from nanobot.llm_usage.context import LLMUsageSource, current_llm_usage_source
from nanobot.providers.base import LLMProvider, LLMUsage
from nanobot.security.redact import redact_value
from nanobot.security.workspace_access import (
    WorkspaceScope,
    bind_workspace_scope,
    reset_workspace_scope,
    sandbox_environ,
    workspace_sandbox_status,
)
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from nanobot.kernel.env import CoreEnvironment
    from nanobot.kernel.policy import PermissionPolicy
    from nanobot.kernel.variants import Variant


class _SubagentOrigin(TypedDict):
    channel: str
    chat_id: str
    session_key: str | None
    llm_usage_source: NotRequired[LLMUsageSource]


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent."""

    task_id: str
    label: str
    task_description: str
    started_at: float          # time.monotonic()
    # queued | initializing | awaiting_tools | tools_completed | final_response | done | error
    phase: str = "initializing"
    iteration: int = 0
    tool_events: list[dict[str, str]] = field(default_factory=list)
    usage: LLMUsage | None = None
    stop_reason: str | None = None
    error: str | None = None


class _SubagentHook(AgentHook):
    """Hook for subagent execution — logs tool calls and updates status."""

    def __init__(self, task_id: str, status: SubagentStatus | None = None) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tool_call in context.tool_calls:
            # Lazy: redaction only runs if a sink will actually emit the record.
            logger.opt(lazy=True).debug(
                "Subagent [{}] executing: {} with arguments: {}",
                lambda: self._task_id,
                lambda: tool_call.name,
                lambda: json.dumps(redact_value(tool_call.arguments), ensure_ascii=False),
            )

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_events)
        self._status.usage = context.usage
        if context.error:
            self._status.error = str(context.error)


# Capabilities a sub-agent never gets unless a future manifest grants them (I4).
SUBAGENT_DENIED_BY_DEFAULT: frozenset[str] = frozenset({"session.send"})


def _subagent_principal(task_id: str) -> Any:
    """The gate principal for one sub-agent run (kind ``subagent``)."""
    from nanobot.kernel.policy import Principal

    return Principal(name=f"subagent:{task_id}", kind="subagent")


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        workspace: Path | None = None,
        bus: MessageBus | None = None,
        max_tool_result_chars: int | None = None,
        model: str | None = None,
        tools_config: ToolsConfig | None = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        tools_allow: list[str] | None = None,
        tools_deny: list[str] | None = None,
        inline_skills: list | None = None,
        env: "CoreEnvironment | None" = None,
        policy: "PermissionPolicy | None" = None,
        max_policy_denials: int = DEFAULT_MAX_POLICY_DENIALS,
        child_policy: "PermissionPolicy | None" = None,
        variant: "Variant | None" = None,
    ):
        self.env = env
        # The parent loop's Variant (Task 8): children see the same overrides.
        self.variant = variant
        # The parent loop's gate policy; None means DefaultPolicy(). Each run gets
        # ``policy.attenuate(requested, narrower=child_policy)`` (I4, ``_child_policy``).
        self.policy = policy
        # Optional narrower set every child is intersected with (a future manifest).
        self.child_policy = child_policy
        # I5 denial ceiling for each sub-agent run; counted per run, never shared
        # with the parent turn's counter.
        self.max_policy_denials = max_policy_denials
        if workspace is None:
            raise TypeError("SubagentManager.__init__() missing required argument: 'workspace'")
        if bus is None:
            raise TypeError("SubagentManager.__init__() missing required argument: 'bus'")
        if max_tool_result_chars is None:
            raise TypeError(
                "SubagentManager.__init__() missing required argument: 'max_tool_result_chars'"
            )
        if model is not None and provider is None:
            raise TypeError("SubagentManager model compatibility argument requires provider")

        defaults = AgentDefaults()
        self._compat_runtime: LLMRuntime | None = None
        if provider is not None:
            warnings.warn(
                "SubagentManager provider/model constructor arguments are deprecated; "
                "pass runtime=... to spawn() instead",
                DeprecationWarning,
                stacklevel=2,
            )
            self._compat_runtime = LLMRuntime.capture(
                provider,
                model or provider.get_default_model(),
                context_window_tokens=defaults.context_window_tokens,
            )
        self.workspace = workspace
        self.bus = bus
        self.tools_config = tools_config or ToolsConfig()
        self.max_tool_result_chars = max_tool_result_chars
        self.restrict_to_workspace = restrict_to_workspace
        self.disabled_skills = set(disabled_skills or [])
        self.inline_skills = list(inline_skills) if inline_skills else None
        self.tools_allow = list(tools_allow) if tools_allow is not None else None
        self.tools_deny = list(tools_deny) if tools_deny else []
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else defaults.max_tool_iterations
        )
        self.max_concurrent_subagents = (
            max_concurrent_subagents
            if max_concurrent_subagents is not None
            else defaults.max_concurrent_subagents
        )
        self._run_slots = asyncio.Semaphore(self.max_concurrent_subagents)
        self.runner = AgentRunner()
        # Fallback for direct ``_build_tools`` callers; each run gets its own manager
        # (its own exec-session quota), kept here while it still has live sessions.
        self._exec_session_manager = ExecSessionManager()
        self._child_exec_managers: dict[str, ExecSessionManager] = {}
        self._running_tasks: dict[str, asyncio.Task[str]] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}

    def runtime_statuses(self) -> Mapping[str, SubagentStatus]:
        """Return the observable task statuses used by runtime-control snapshots."""
        return self._task_statuses

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        """Update the deprecated runtime source used by legacy ``spawn`` calls."""
        warnings.warn(
            "SubagentManager.set_provider() is deprecated; pass runtime=... to spawn() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        context_window_tokens = (
            self._compat_runtime.context_window_tokens
            if self._compat_runtime is not None
            else AgentDefaults().context_window_tokens
        )
        self._compat_runtime = LLMRuntime.capture(
            provider,
            model,
            context_window_tokens=context_window_tokens,
        )

    def _compat_spawn_runtime(self) -> LLMRuntime:
        runtime = self._compat_runtime
        if runtime is None:
            raise TypeError(
                "SubagentManager.spawn() missing required keyword-only argument: 'runtime'"
            )
        warnings.warn(
            "SubagentManager.spawn() without runtime is deprecated; pass runtime=... explicitly",
            DeprecationWarning,
            stacklevel=3,
        )
        return LLMRuntime.capture(
            runtime.provider,
            runtime.model,
            context_window_tokens=runtime.context_window_tokens,
        )

    def _subagent_tools_config(self) -> ToolsConfig:
        """Build a ToolsConfig scoped for subagent use."""
        return ToolsConfig(
            exec=self.tools_config.exec,
            web=self.tools_config.web,
            file=self.tools_config.file,
            restrict_to_workspace=self.restrict_to_workspace,
        )

    def _build_tools(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
        exec_session_manager: ExecSessionManager | None = None,
    ) -> ToolRegistry:
        """Build an isolated subagent tool registry via ToolLoader."""
        root = self.workspace if workspace is None else workspace
        registry = ToolRegistry()
        cfg = tools_config if tools_config is not None else self._subagent_tools_config()
        ctx = ToolContext(
            config=cfg,
            workspace=str(root.resolve()),
            exec_session_manager=(
                exec_session_manager
                if exec_session_manager is not None
                else self._exec_session_manager
            ),
            file_state_store=FileStates(),
            workspace_sandbox=workspace_sandbox_status(
                restrict_to_workspace=cfg.restrict_to_workspace,
                workspace=root,
                environ=sandbox_environ(
                    self.env.config.section("runtime") if self.env is not None else None
                ),
            ),
            env=self.env,
            variant=getattr(self, "variant", None),
        )
        ToolLoader().load(
            ctx, registry, scope="subagent",
            allow=self.tools_allow, deny=self.tools_deny,
        )
        return registry

    def _requested_capabilities(self, tools: ToolRegistry) -> frozenset[str]:
        """The static capability surface of every tool in *tools* (union of names).

        - Uses ``Tool.capability_surface`` (``_capability_names`` on each tool class).
        - An undeclared tool is probed with ``capabilities({})``; that may miss
          param-dependent names, which the child is then denied (fail closed).
        """
        names: set[str] = set()
        for name in tools.tool_names:
            tool = tools.get(name)
            surface_fn = getattr(tool, "capability_surface", None)
            try:
                surface = surface_fn() if callable(surface_fn) else None
            except Exception:  # noqa: BLE001 - treat a broken declaration as undeclared
                surface = None
            if surface is None:
                logger.warning(
                    "Tool {} declares no static capability surface; probing it for the "
                    "sub-agent policy", name,
                )
                try:
                    surface = frozenset(r.capability for r in tool.capabilities({}))
                except Exception:  # noqa: BLE001 - no probe result means no grant
                    surface = frozenset()
            names |= surface
        return frozenset(names)

    def _child_policy(self, tools: ToolRegistry) -> "PermissionPolicy":
        """The attenuated policy for one child run over *tools* (I4).

        Attenuates the parent to ``surface - SUBAGENT_DENIED_BY_DEFAULT`` intersected with
        ``child_policy``: never broader than the parent, the child's own tool surface or
        the narrower set.

        - Only an exact ``DefaultPolicy`` parent's own ``attenuate`` is trusted.
        - Any other parent (a subclass, a foreign policy whose ``attenuate`` may
          over-grant or raise, or a decide-only policy with no ``attenuate`` at all) is
          wrapped in ``IntersectionPolicy`` first. Its ``attenuate`` keeps the parent as
          a floor and bounds the result with ``DefaultPolicy(allowed=requested)``.
        """
        from nanobot.kernel.policy import DefaultPolicy, IntersectionPolicy

        parent = self.policy if self.policy is not None else DefaultPolicy()
        requested = self._requested_capabilities(tools) - SUBAGENT_DENIED_BY_DEFAULT
        if type(parent) is not DefaultPolicy:
            parent = IntersectionPolicy(parent)
        return parent.attenuate(requested, narrower=self.child_policy)

    async def _release_exec_manager(self, task_id: str) -> None:
        """Drop a finished run's exec-session manager once it has no live sessions."""
        manager = self._child_exec_managers.get(task_id)
        if manager is None:
            return
        try:
            live = await manager.list()
        except Exception:  # noqa: BLE001 - keep it so close() still reaches it
            logger.exception("Subagent [{}] exec sessions could not be listed", task_id)
            return
        if not live:
            self._child_exec_managers.pop(task_id, None)

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        *,
        runtime: LLMRuntime | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        if runtime is None:
            runtime = self._compat_spawn_runtime()
        if temperature is not None:
            runtime = runtime.with_generation_overrides(temperature=temperature)
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin: _SubagentOrigin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
            "session_key": session_key,
            "llm_usage_source": current_llm_usage_source(),
        }

        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
        )
        self._task_statuses[task_id] = status

        bg_task = asyncio.create_task(
            self._run_subagent(
                task_id,
                task,
                display_label,
                origin,
                status,
                runtime,
                origin_message_id,
                workspace_scope,
            )
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task[str]) -> None:
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def run_inline(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        *,
        runtime: LLMRuntime | None = None,
    ) -> str:
        """Run a subagent synchronously and return its result to the caller."""
        if runtime is None:
            runtime = self._compat_spawn_runtime()
        if temperature is not None:
            runtime = runtime.with_generation_overrides(temperature=temperature)
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin: _SubagentOrigin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
            "session_key": session_key,
            "llm_usage_source": current_llm_usage_source(),
        }
        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
        )
        self._task_statuses[task_id] = status
        logger.info("Running inline subagent [{}]: {}", task_id, display_label)
        inline_task = asyncio.create_task(
            self._run_subagent(
                task_id,
                task,
                display_label,
                origin,
                status,
                runtime,
                origin_message_id,
                workspace_scope,
                announce=False,
            )
        )
        self._running_tasks[task_id] = inline_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)
        try:
            result = await inline_task
            if status.phase == "error" or status.stop_reason == "error":
                return ToolResult.error(result)
            return result
        finally:
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: _SubagentOrigin,
        status: SubagentStatus,
        runtime: LLMRuntime,
        origin_message_id: str | None = None,
        workspace_scope: WorkspaceScope | None = None,
        *,
        announce: bool = True,
    ) -> str:
        """Wait for capacity, then execute one subagent task."""
        status.phase = "queued"
        async with self._run_slots:
            status.phase = "initializing"
            return await self._run_admitted_subagent(
                task_id,
                task,
                label,
                origin,
                status,
                runtime,
                origin_message_id,
                workspace_scope,
                announce=announce,
            )

    async def _run_admitted_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: _SubagentOrigin,
        status: SubagentStatus,
        runtime: LLMRuntime,
        origin_message_id: str | None = None,
        workspace_scope: WorkspaceScope | None = None,
        *,
        announce: bool = True,
    ) -> str:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        async def _on_checkpoint(payload: dict[str, Any]) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        try:
            root = workspace_scope.project_path if workspace_scope is not None else self.workspace
            cfg = None
            if workspace_scope is not None:
                cfg = self._subagent_tools_config()
                cfg.restrict_to_workspace = workspace_scope.restrict_to_workspace
            # Construct from the agent workspace; the bound scope below supplies the project cwd.
            exec_manager = ExecSessionManager()
            self._child_exec_managers[task_id] = exec_manager
            tools = self._build_tools(tools_config=cfg, exec_session_manager=exec_manager)
            principal = _subagent_principal(task_id)
            policy = self._child_policy(tools)
            tools.configure_gate(
                policy=policy, principal=principal, env=self.env, workspace=root,
            )
            system_prompt = self._build_subagent_prompt(workspace=root)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            sess_key = origin.get("session_key")
            request_token = bind_request_context(RequestContext(
                channel=origin["channel"],
                chat_id=origin["chat_id"],
                message_id=origin_message_id,
                session_key=sess_key,
                runtime=runtime,
            ))
            token = bind_workspace_scope(workspace_scope) if workspace_scope is not None else None
            try:
                result = await self.runner.run(AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    runtime=runtime,
                    max_iterations=self.max_iterations,
                    max_tool_result_chars=self.max_tool_result_chars,
                    hook=_SubagentHook(task_id, status),
                    max_iterations_message="Task completed but no final response was generated.",
                    finalize_on_max_iterations=False,
                    error_message=None,
                    checkpoint_callback=_on_checkpoint,
                    session_key=sess_key,
                    workspace=root,
                    llm_usage_source=origin.get(
                        "llm_usage_source",
                        current_llm_usage_source(),
                    ),
                    policy=policy,
                    principal=principal,
                    env=self.env,
                    max_policy_denials=self.max_policy_denials,
                ))
            finally:
                if token is not None:
                    reset_workspace_scope(token)
                reset_request_context(request_token)
            status.phase = "done"
            status.stop_reason = result.stop_reason

            if result.stop_reason == "error":
                final_result = result.error or "Error: subagent execution failed."
                final_status = "error"
            else:
                final_result = result.final_content or "Task completed but no final response was generated."
                final_status = "ok"
                logger.info("Subagent [{}] completed successfully", task_id)
            if announce:
                await self._announce_result(
                    task_id,
                    label,
                    task,
                    final_result,
                    origin,
                    final_status,
                    origin_message_id,
                )
            return final_result

        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            logger.exception("Subagent [{}] failed", task_id)
            final_result = f"Error: {e}"
            if announce:
                await self._announce_result(
                    task_id,
                    label,
                    task,
                    final_result,
                    origin,
                    "error",
                    origin_message_id,
                )
            return final_result
        finally:
            await self._release_exec_manager(task_id)

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: _SubagentOrigin,
        status: str,
        origin_message_id: str | None = None,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = render_template(
            "agent/subagent_announce.md",
            roots=self._template_roots(),
            label=label,
            status_text=status_text,
            task=task,
            result=result,
        )

        # Inject as system message to trigger main agent.
        # Use session_key_override to align with the main agent's effective
        # session key (which accounts for unified sessions) so the result is
        # routed to the correct pending queue (mid-turn injection) instead of
        # being dispatched as a competing independent task.
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        metadata: dict[str, Any] = {
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
        }
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
            metadata=metadata,
        )

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    def _template_roots(self) -> tuple[Path, ...]:
        variant = getattr(self, "variant", None)
        return variant.template_roots if variant is not None else ()

    def _build_subagent_prompt(self, workspace: Path | None = None) -> str:
        """Build a focused system prompt for the subagent."""
        from nanobot.agent.skills import SkillsLoader

        agent_workspace = self.workspace.expanduser().resolve()
        project_workspace = workspace.expanduser().resolve() if workspace else agent_workspace
        variant = getattr(self, "variant", None)
        skills_summary = SkillsLoader(
            self.workspace,
            builtin_skills_dir=variant.builtin_skills_dir if variant is not None else None,
            disabled_skills=self.disabled_skills,
            inline_skills=self.inline_skills,
            env=self.env,
        ).build_skills_summary(workspace=project_workspace)
        history_log = (
            str(agent_workspace / "memory" / "history.jsonl")
            if agent_workspace != project_workspace
            else "memory/history.jsonl"
        )
        return render_template(
            "agent/subagent_system.md",
            roots=self._template_roots(),
            workspace=str(project_workspace),
            agent_workspace=str(agent_workspace),
            history_log=history_log,
            skills_summary=skills_summary or "",
        )

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._exec_session_manager.terminate_by_owner(session_key)
        for task_id, manager in list(self._child_exec_managers.items()):
            await manager.terminate_by_owner(session_key)
            await self._release_exec_manager(task_id)
        return len(tasks)

    async def close(self) -> None:
        """Cancel running subagents and close every exec session they left open."""
        tasks = [task for task in self._running_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._exec_session_manager.close_all()
        managers = list(self._child_exec_managers.values())
        self._child_exec_managers.clear()
        for manager in managers:
            await manager.close_all()

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        tids = self._session_tasks.get(session_key, set())
        return sum(
            1 for tid in tids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        )
