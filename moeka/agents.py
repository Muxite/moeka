"""Agents: ``kernel.agent(AgentSpec(...))`` and ``await agent.run(message)``.

An agent is a tool-using loop on the kernel: its model calls go through the
kernel's budget and ledger, its events through ``kernel.trace`` (one span per run),
and its sessions through the kernel's shared store (see ``nanobot.kernel.agent``).
"""

from nanobot.kernel.agent import (
    Agent,
    AgentSpec,
    AskUser,
    RunLimits,
    RunResult,
    StopReason,
    ToolInfo,
)

__all__ = ["Agent", "AgentSpec", "AskUser", "RunLimits", "RunResult", "StopReason", "ToolInfo"]
