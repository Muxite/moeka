"""Owner-configured handoff from general conversation to a creative project."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, ToolResult, capability_request, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema
from nanobot.bus.events import InboundMessage

if TYPE_CHECKING:
    from nanobot.creative_bridge import CreativeBridge


_PARAMETERS = tool_parameters_schema(
    project_name=StringSchema("Creative project name; omit to select the default wonhee project"),
    leave=BooleanSchema(description="Return this chat to general moeka conversation", default=False),
)


@tool_parameters(_PARAMETERS)
class WonheeHandoffTool(Tool):
    def __init__(self, bridge: CreativeBridge) -> None:
        self._bridge = bridge

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return False  # The owning gateway installs this tool with its trusted bridge.

    @property
    def name(self) -> str:
        return "wonhee_project" if self._bridge.config.mode=='direct' else "wonhee_handoff"

    @property
    def parameters(self) -> dict[str, Any]:
        return _PARAMETERS

    @property
    def description(self) -> str:
        return "Select or create a Wonhee project for the current conversation. In direct mode you remain the same assistant and can immediately use Wonhee tools. The creator's current request and subsequent files stay in this project. Use leave=true to clear selection."

    def capabilities(self, params: dict[str, Any]) -> list[Any]:
        target=self._bridge.config.peer_id if self._bridge.config.mode=='peer' else 'wonhee:project'
        return [capability_request("session.send", target)]

    async def execute(self, project_name: str | None = None, leave: bool = False, **kwargs: Any) -> str:
        request = current_request_context()
        if request is None or request.channel != "telegram" or not request.sender_id:
            return ToolResult.error("wonhee handoff requires the current Telegram creator conversation")
        from nanobot.creative_bridge import json_object_list

        descriptors = json_object_list(request.metadata.get("attachments", []))
        media: list[str] = [item["path"] for item in descriptors if isinstance(item.get("path"), str)]
        message = InboundMessage(
            channel=request.channel, chat_id=request.chat_id, sender_id=request.sender_id,
            content=request.metadata.get('creative_original_text',request.original_user_text) or "", metadata=dict(request.metadata), media=media,
        )
        try:
            result = await self._bridge.handoff(message, project_name=project_name, leave=leave)
            if not leave and request.original_user_text:
                await self._bridge.intercept(message, selected_project=True)
                if self._bridge.config.mode=='direct':
                    request.metadata.update(message.metadata)
                    context=message.metadata['creative']
                    await self._bridge.call('project_brief_save',project_id=context['project_id'],
                        content=message.metadata['creative_original_text'],
                        source={**context['route'],'request_key':context['request_id']})
            elif leave and self._bridge.config.mode=='direct':
                from nanobot.creative_context import clear
                clear(request.metadata)
            return result
        except (ValueError, RuntimeError) as exc:
            return ToolResult.error(str(exc))
