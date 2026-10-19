"""Versioned, instance-specific personality edits without policy writes."""

from __future__ import annotations

# Tool calls carry operation-specific arguments.
# pyright: reportIncompatibleMethodOverride=false
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanobot.agent import personality
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext

if TYPE_CHECKING:
    from nanobot.kernel.env import CoreEnvironment


class PersonalityTool(Tool):
    def __init__(self, workspace: Path, env: CoreEnvironment | None) -> None:
        self.workspace = workspace
        self.env = env

    @property
    def name(self) -> str:
        return "personality"

    @property
    def description(self) -> str:
        return "Revise your personality (150 words), list versions or restore one. Applied next turn; fixed policy cannot change."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
            "operation": {"type": "string", "enum": ["read", "revise", "history", "restore"]},
            "text": {"type": "string"}, "version": {"type": "string"},
        }, "required": ["operation"], "additionalProperties": False}

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return bool(personality.settings(ctx.env).get("allow_self_edit", False))

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(Path(ctx.workspace), ctx.env)

    async def execute(self, operation: str, text: str = "", version: str = "", **kwargs: Any) -> ToolResult:
        try:
            if operation == "read":
                return ToolResult(content=(self.workspace / "SOUL.md").read_text())
            if operation == "history":
                return ToolResult(content="\n".join(personality.versions(self.workspace)))
            if operation == "revise":
                return ToolResult(content=personality.stage(self.workspace, text, self.env))
            if operation == "restore":
                return ToolResult(content=personality.restore(self.workspace, version, self.env))
            return ToolResult.error("Unknown personality operation")
        except (OSError, ValueError) as exc:
            return ToolResult.error(str(exc))
