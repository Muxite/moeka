"""Worker tools: delegate technical work to Claude Code or Codex sessions (spec 015 phase 5).

Moeka briefs a stronger agent, follows up, and relays the result. Mechanical monitoring
(process, timeout, last output) lives in ``nanobot.workers.manager``; these tools only start,
resume, inspect and stop sessions by explicit job id. The two backends are separate accounts
and moeka never falls back from one to the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, ToolResult, capability_request, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext
    from nanobot.workers.manager import WorkerManager

_BACKENDS = ["claude", "codex"]


class _WorkerTool(Tool):
    def __init__(self, manager: WorkerManager) -> None:
        self._m = manager

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return (
            bool(ctx.host_tools)
            and ctx.worker_manager is not None
            and bool(ctx.worker_manager.enabled_backends())
        )

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(ctx.worker_manager)

    def _fmt(self, job_id: str, lead: str) -> str:
        return f"{lead} Job id: {job_id}. Use worker_inspect for status; the owner is told when it finishes."


@tool_parameters(
    tool_parameters_schema(
        required=["backend", "workspace", "request"],
        backend=StringSchema(
            "Which agent: 'claude' (Claude Code) or 'codex'. They are separate accounts with "
            "separate usage; choose deliberately, there is no automatic fallback.",
            enum=_BACKENDS,
        ),
        workspace=StringSchema("Absolute path of the project directory the worker will edit."),
        request=StringSchema(
            "The owner's request, forwarded faithfully with their constraints. Do not invent a "
            "plan or turn guesses into requirements; the worker investigates."
        ),
        context=StringSchema("Relevant history or constraints the owner gave earlier (optional)."),
    )
)
class WorkerStartTool(_WorkerTool):
    _capability_names = frozenset({"fs.write"})

    @property
    def name(self) -> str:
        return "worker_start"

    @property
    def description(self) -> str:
        return (
            "Delegate technical work to a Claude Code or Codex session running in a project "
            "directory. Returns immediately with a job id; the worker runs in the background and "
            "the owner is messaged when it finishes, asks a question or fails. Reports are the "
            "worker's own claims, not verified results. One writer per project at a time."
        )

    def capabilities(self, params: dict[str, Any]) -> list[Any]:
        return [capability_request("fs.write", params.get("workspace", ""))]

    async def execute(
        self, backend: str, workspace: str, request: str, context: str = "", **kwargs: Any,
    ) -> str:
        from nanobot.workers.manager import WorkerError

        rc = current_request_context()
        key = rc.session_key if rc and rc.session_key else (
            f"{rc.channel}:{rc.chat_id}" if rc else "")
        route = None
        if rc:
            parent = next((j for j in reversed(self._m.store.active(session_key=key))
                           if j.role == "" and j.route), None)
            route = parent.route if parent else (rc.channel, str(rc.chat_id), "")
        try:
            job = await self._m.start(
                backend=backend, workspace=workspace, request=request, context=context,
                session_key=key, route=route,
            )
        except WorkerError as exc:
            return ToolResult.error(f"Error: worker not started ({exc.cause}): {exc}")
        return self._fmt(job.id, f"Started a {backend} worker in {workspace}.")


@tool_parameters(
    tool_parameters_schema(
        required=["job_id", "message"],
        job_id=StringSchema("Worker job id from worker_start."),
        message=StringSchema(
            "The follow-up: the owner's answer to the worker's question, or a changed or added "
            "request. Resumes the same session, so earlier context is kept."
        ),
    )
)
class WorkerFollowUpTool(_WorkerTool):
    @property
    def name(self) -> str:
        return "worker_follow_up"

    @property
    def description(self) -> str:
        return (
            "Send a follow-up to an existing worker session by job id (resumes that exact session). "
            "Use it to pass the owner's answer to a worker's question or to extend the work. "
            "Fails if the worker is still running."
        )

    def capabilities(self, params: dict[str, Any]) -> list[Any]:
        return []

    async def execute(self, job_id: str, message: str, **kwargs: Any) -> str:
        from nanobot.workers.manager import WorkerError

        try:
            await self._m.follow_up(job_id, message)
        except WorkerError as exc:
            return ToolResult.error(f"Error: follow-up not sent ({exc.cause}): {exc}")
        return self._fmt(job_id, "Follow-up sent to the same session.")


@tool_parameters(
    tool_parameters_schema(
        job_id=StringSchema("Worker job id. Leave empty for recent workers and per-backend usage."),
    )
)
class WorkerInspectTool(_WorkerTool):
    @property
    def name(self) -> str:
        return "worker_inspect"

    @property
    def description(self) -> str:
        return (
            "Observed facts about a worker: process state, last output age, job state, latest "
            "record. With no job id, lists recent workers and usage per backend account. "
            "Reads code-observed state only; it never wakes the worker."
        )

    @property
    def read_only(self) -> bool:
        return True

    def capabilities(self, params: dict[str, Any]) -> list[Any]:
        return []

    async def execute(self, job_id: str = "", **kwargs: Any) -> str:
        return self._m.inspect(job_id.strip())


@tool_parameters(
    tool_parameters_schema(
        required=["job_id"],
        job_id=StringSchema("Worker job id to stop."),
    )
)
class WorkerStopTool(_WorkerTool):
    @property
    def name(self) -> str:
        return "worker_stop"

    @property
    def description(self) -> str:
        return "Stop a running worker (kills its process group). The job is recorded as stopped."

    def capabilities(self, params: dict[str, Any]) -> list[Any]:
        return []

    async def execute(self, job_id: str, **kwargs: Any) -> str:
        from nanobot.workers.manager import WorkerError

        try:
            return await self._m.stop(job_id)
        except WorkerError as exc:
            return ToolResult.error(f"Error: {exc}")
