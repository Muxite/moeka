"""``job_status``: read the durable job record so status answers are never invented (spec 015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext
    from nanobot.kernel.jobs import JobStore


@tool_parameters(
    tool_parameters_schema(
        job_id=StringSchema(
            "Job id to inspect (shown in acknowledgements and /jobs). Leave empty to list "
            "active and recent jobs."
        ),
    )
)
class JobStatusTool(Tool):
    """Read-only view of the job record: state, failing side, cause and timings.

    Needs no capability: it reads only the instance's own job store. Use it whenever the
    owner asks what is going on, instead of answering from earlier messages.
    """

    def __init__(self, store: JobStore) -> None:
        self._store = store

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return bool(ctx.host_tools) and ctx.job_store is not None

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(ctx.job_store)

    @property
    def name(self) -> str:
        return "job_status"

    @property
    def description(self) -> str:
        return (
            "Report the real state of the owner's jobs from the durable job record: state, "
            "which side stopped a job and why, child worker or wonhee jobs, and timings. "
            "Call it when asked for status; never answer status from memory of earlier turns."
        )

    @property
    def read_only(self) -> bool:
        return True

    def capabilities(self, params: dict[str, Any]) -> list[Any]:
        return []

    async def execute(self, job_id: str = "", **kwargs: Any) -> str:
        from nanobot.kernel.job_report import format_job_notice

        store = self._store
        jid = (job_id or "").strip()
        if jid:
            job = store.get(jid)
            if job is None:
                return ToolResult.error(f"Error: no job {jid!r}")
            lines = [format_job_notice(job, store.history(jid))]
            for child in store.children(jid):
                lines.append(f"  child {child.role or 'job'} {child.id}: {child.state.value}")
            return "\n".join(lines)
        active = store.active()
        recent = [j for j in store.recent(8) if j.state not in {x.state for x in active}]
        if not active and not recent:
            return "No jobs recorded yet."
        out = []
        if active:
            out.append("Active:")
            out += [f"  {j.id} [{j.state.value}] {j.role or 'chat'}: {j.request_text[:80]}"
                    for j in active]
        if recent:
            out.append("Recent:")
            out += [f"  {j.id} [{j.state.value}] {j.role or 'chat'}: {j.request_text[:80]}"
                    for j in recent]
        return "\n".join(out)
