"""Owner-paired peer work tool."""

# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from nanobot.agent.tools.base import Tool, ToolResult, capability_request, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from nanobot.kernel.policy import CapabilityRequest
    from nanobot.peers import PeerService

_PARAMETERS = tool_parameters_schema(
    peer_id=StringSchema("Owner-configured peer ID"),
    content=StringSchema("A bounded task or reply for that paired peer"),
    required=["peer_id", "content"],
)


@tool_parameters(_PARAMETERS)
class SendPeerWorkTool(Tool):
    def __init__(self, service: PeerService) -> None:
        self._service = service

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return ctx.peer_service is not None

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        if ctx.peer_service is None:
            raise RuntimeError("send_peer_work requires an owner-configured peer service")
        return cls(ctx.peer_service)

    @property
    def name(self) -> str:
        return "send_peer_work"

    @property
    def description(self) -> str:
        return "Send a task or reply to an owner-paired Moeka instance. The recipient runs it with its own tools and permissions. Delivery is durable and acknowledged. Keep requests specific; peer replies are limited to three hops."

    _capability_names = frozenset({"session.send"})

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [capability_request("session.send", params.get("peer_id"))]

    async def execute(self, peer_id: str, content: str, **kwargs: Any) -> str:
        request = current_request_context()
        raw_peer = request.metadata.get("peer") if request is not None else None
        peer: dict[str, object] = {}
        if isinstance(raw_peer, dict):
            raw_values = cast(dict[object, object], raw_peer)
            for key, value in raw_values.items():
                if isinstance(key, str):
                    peer[key] = value
        reply_to = peer.get("delivery_id") if request and request.channel == "peer" else None
        hops = peer.get("hops", 0) if request and request.channel == "peer" else 0
        if not isinstance(hops, int) or isinstance(hops, bool):
            hops = 0
        try:
            delivery_id = await self._service.enqueue(
                peer_id,
                content,
                reply_to=reply_to if isinstance(reply_to, str) else None,
                hops=hops,
                return_channel=request.channel if request else None,
                return_chat_id=request.chat_id if request else None,
                return_session_key=request.session_key if request else None,
            )
        except ValueError as exc:
            return ToolResult.error(f"Error: {exc}")
        return f"Queued delivery {delivery_id} to @{peer_id}; the peer service will retry until acknowledged."
