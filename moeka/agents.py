"""Agents: ``kernel.agent(AgentSpec(...))`` and ``await agent.run(message)``.

An agent is a tool-using loop on the kernel: its model calls go through the
kernel's budget and ledger, its events through ``kernel.trace`` (one span per run),
and its sessions through the kernel's shared store (see ``nanobot.kernel.agent``).
``agent.stream(message)`` is the same run as an :class:`AgentStream` of typed
:class:`StreamEvent` values (``StreamEventType`` names them).
"""

from nanobot.kernel.agent import (
    Agent,
    AgentSpec,
    AgentStream,
    AskUser,
    RunLimits,
    RunResult,
    StopReason,
    ToolInfo,
)
from nanobot.sdk.types import StreamEvent, StreamEventType

__all__ = [
    "Agent",
    "AgentSpec",
    "AgentStream",
    "AskUser",
    "RunLimits",
    "RunResult",
    "StopReason",
    "StreamEvent",
    "StreamEventType",
    "ToolInfo",
]
