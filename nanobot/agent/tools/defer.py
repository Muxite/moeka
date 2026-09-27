"""``defer_action``: record an action the agent wants to run but cannot (design 5a)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.schema import ObjectSchema, StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext
    from nanobot.agent.tools.registry import ToolRegistry


def _structured_error(error: str, tool: str, errors: list[str]) -> ToolResult:
    payload = {"error": error, "tool": tool, "errors": errors}
    return ToolResult.error("Error: " + json.dumps(payload, ensure_ascii=False))


@tool_parameters(
    tool_parameters_schema(
        tool=StringSchema("Name of the tool you would call"),
        arguments=ObjectSchema(
            description="The arguments you would pass to that tool",
            additional_properties=True,
        ),
        reason=StringSchema("Why it is needed and why you cannot run it now"),
        required=["tool", "arguments", "reason"],
    )
)
class DeferActionTool(Tool):
    """Append a ``source="agent"`` entry to ``scratchpad/deferred.jsonl``.

    - Needs no capability (``capabilities`` returns ``[]``), so the gate never denies it.
    - When *tool* is registered, *arguments* are cast and validated against that tool's
      own schema (``Tool.validate_params``, i.e. ``Schema.validate_json_schema_value``);
      invalid arguments are rejected with a structured error and nothing is logged.
    - Never executes the named tool. The log write is direct, not a gated tool call.
    """

    def __init__(self, work_dir: Path | str, registry: ToolRegistry | None = None) -> None:
        from nanobot.kernel.deferred import DeferredLog

        self._log = DeferredLog(work_dir)
        self._registry = registry

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        work_dir = ctx.env.paths.work_dir if ctx.env is not None else Path(ctx.workspace)
        return cls(work_dir=work_dir)

    def bind_registry(self, registry: ToolRegistry) -> None:
        """Called by ``ToolRegistry.register``: the registry whose schemas are checked."""
        self._registry = registry

    @property
    def name(self) -> str:
        return "defer_action"

    @property
    def description(self) -> str:
        return (
            "Record a tool call you need but cannot run (blocked, missing capability, "
            "sandbox or credential) in the deferred-action log for a human to review. "
            "It never runs the tool. Denied calls are logged automatically."
        )

    async def execute(
        self,
        tool: str | None = None,
        arguments: dict[str, Any] | None = None,
        reason: str | None = None,
        **_: Any,
    ) -> Any:
        name = str(tool or "").strip()
        if not name:
            return _structured_error("invalid_tool", name, ["tool must be a tool name"])
        if name == self.name:
            return _structured_error("invalid_tool", name, ["defer_action cannot defer itself"])
        if not isinstance(arguments, dict):
            return _structured_error("invalid_arguments", name, ["arguments must be an object"])

        capability: str | None = None
        target = self._registry.get(name) if self._registry is not None else None
        if target is not None:
            params = target.cast_params(dict(arguments))
            errors = target.validate_params(params)
            if errors:
                return _structured_error("invalid_arguments", name, errors)
            arguments = params
            try:
                names = sorted({r.capability for r in target.capabilities(params)})
            except Exception:  # noqa: BLE001 - a broken declaration only loses this field
                names = []
            capability = ",".join(names) or None

        try:
            self._log.append(
                tool=name, arguments=arguments, reason=str(reason or ""),
                capability=capability, source="agent",
            )
        except OSError as exc:
            return ToolResult.error(f"Error: could not write the deferred-action log: {exc}")
        return (
            f"Logged deferred action for {name}; it was not run. "
            "A host or human reviews the deferred log. Continue without it."
        )
