"""Incident replay for invariant I5: the June-2026 whitelist-only runaway turn.

- June 2026: a non-empty ``tools.exec.allowPatterns`` turned exec into whitelist-only
  mode. The model kept trying one distinct command after another; nothing stopped the
  turn short of ``max_tool_iterations``, so each turn burned ~200 iterations.
- Replay: a whitelist-only ``PermissionPolicy`` denies every command the model tries,
  the fake model requests a new distinct command each iteration (50 queued), and
  ``max_iterations`` is 200, so the old runner would run until the iteration cap.
- Required: the turn stops at exactly 6 policy denials with ``stop_reason ==
  "policy_denials"``, well short of 200 iterations, and the model gets its one
  no-tools finalization call.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.config.schema import AgentDefaults
from nanobot.kernel.policy import (
    POLICY_MARKER,
    Allow,
    CapabilityRequest,
    DefaultPolicy,
    Deny,
    Principal,
    policy_deny,
)
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.runtime import BUDGET_EXHAUSTED_FINALIZATION_PROMPT
from tests.agent.runner_helpers import make_run_spec

_MAX_CHARS = AgentDefaults().max_tool_result_chars
_COMMANDS = [f"systemctl status unit-{i}.service" for i in range(50)]


class WhitelistOnlyPolicy(DefaultPolicy):
    """``exec.run`` only for an exact allowlist; the June incident's allowPatterns."""

    def __init__(self, allowed_commands: set[str]) -> None:
        super().__init__()
        self.allowed_commands = allowed_commands
        self.denied: list[str] = []

    def decide(self, principal: Principal, req: CapabilityRequest, ctx: Any) -> Allow | Deny:
        if req.capability == "exec.run" and req.resource not in self.allowed_commands:
            self.denied.append(req.resource)
            return policy_deny(principal, req, "command not on the allowlist")
        return super().decide(principal, req, ctx)


class RunawayModel:
    """Requests the next distinct command on every tool-enabled call."""

    def __init__(self) -> None:
        self.tool_calls = 0
        self.finalization_calls: list[list[dict[str, Any]]] = []

    async def __call__(self, **kwargs: Any) -> LLMResponse:
        if kwargs.get("tools") is None:
            self.finalization_calls.append(list(kwargs["messages"]))
            return LLMResponse(content="The exec allowlist blocks what I need.", tool_calls=[])
        command = _COMMANDS[self.tool_calls % len(_COMMANDS)]
        self.tool_calls += 1
        return LLMResponse(
            content="trying another command",
            tool_calls=[ToolCallRequest(
                id=f"call_{self.tool_calls}", name="exec", arguments={"command": command},
            )],
        )


async def test_whitelist_only_turn_stops_at_six_policy_denials(tmp_path):
    model = RunawayModel()
    provider = MagicMock()
    provider.chat_stream_with_retry = model
    exec_tool = ExecTool(working_dir=str(tmp_path))
    tools = ToolRegistry()
    tools.register(exec_tool)
    policy = WhitelistOnlyPolicy(allowed_commands={"uptime"})

    result = await AgentRunner().run(make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "check every service"}],
        tools=tools,
        model="test-model",
        max_iterations=200,
        max_tool_result_chars=_MAX_CHARS,
        policy=policy,
    ))

    assert result.stop_reason == "policy_denials"
    # Exactly six distinct commands were tried and denied; the old bug ran 200.
    assert policy.denied == _COMMANDS[:6]
    assert model.tool_calls == 6
    assert model.tool_calls < 10
    denials = [
        e for e in result.tool_events
        if e["detail"].startswith(("policy_denial: ", "policy_denial_escalated: "))
    ]
    assert len(denials) == 6
    tool_texts = [m["content"] for m in result.messages if m.get("role") == "tool"]
    assert len(tool_texts) == 6
    # Hits 1-2 are the verbatim deny; 3-6 the existing per-capability escalation,
    # which on its own never ended the turn.
    assert all(POLICY_MARKER in text for text in tool_texts[:2])
    assert all("not configurable by the agent" in text for text in tool_texts[2:])
    # Same finalization as max_iterations: one no-tools call with the budget prompt.
    assert len(model.finalization_calls) == 1
    assert model.finalization_calls[0][-1]["content"] == BUDGET_EXHAUSTED_FINALIZATION_PROMPT
    assert result.final_content == "The exec allowlist blocks what I need."
