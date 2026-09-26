"""Escalations for FIXED (non-configurable) guards must not advise config changes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from agent.runner_helpers import make_run_spec

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools import ToolResult
from nanobot.agent.tools.shell import ExecTool
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.runtime import (
    exec_guard_violation_signature,
    repeated_exec_guard_error,
    repeated_workspace_violation_error,
)

_FORK_BOMB = ":(){ :|:& };:"
_PROTECTED = (
    "Error: Path /root/.nanobot/auth/token.json is a protected internal path "
    "(not configurable); reading it through the file tools is not allowed."
)
_OUTSIDE = "Error: Path /etc/hosts is outside allowed directory /work"
_CONFIG_DENIAL = (
    "Error: Command blocked by safety guard (dangerous pattern detected). "
    "Matched '\\\\b(mkfs|diskpart)\\\\b'. "
    "Adjust tools.exec.deny_patterns in config to permit it."
)
_MAX_CHARS = AgentDefaults().max_tool_result_chars


def _floor_denial() -> str:
    denial = ExecTool()._guard_command(_FORK_BOMB, "/tmp")
    assert denial is not None and "This guard is not configurable." in str(denial)
    return str(denial)


def test_floor_denial_has_own_signature():
    assert exec_guard_violation_signature(_floor_denial()) == "violation:exec-floor"
    assert exec_guard_violation_signature(_CONFIG_DENIAL) == "violation:exec-denyguard"


def test_exec_floor_escalation_has_no_config_advice():
    counts: dict[str, int] = {}
    text = _floor_denial()
    assert repeated_exec_guard_error(text, counts) is None
    assert repeated_exec_guard_error(text, counts) is None
    third = repeated_exec_guard_error(text, counts)
    assert third is not None
    assert "must be updated" not in third
    assert "tools.exec" not in third
    assert "fixed safety guard" in third
    assert "Stop retrying" in third


def test_configurable_denials_keep_old_wording():
    counts: dict[str, int] = {}
    for _ in range(2):
        repeated_exec_guard_error(_CONFIG_DENIAL, counts)
    third = repeated_exec_guard_error(_CONFIG_DENIAL, counts)
    assert third == (
        "Error: refusing repeated exec attempts against the command guard.\n"
        "3 commands have been blocked by the configured "
        "tools.exec.deny_patterns this turn. This is a config-level block — "
        "trying different commands, tools, or quoting tricks will NOT change "
        "the answer. Stop retrying. Tell the user which commands you need and "
        "that tools.exec.allowPatterns / denyPatterns in the nanobot config "
        "must be updated before you can proceed."
    )


def test_floor_and_configurable_counters_are_separate():
    counts: dict[str, int] = {}
    floor = _floor_denial()
    repeated_exec_guard_error(floor, counts)
    repeated_exec_guard_error(floor, counts)
    # Two floor denials must not push the configurable class over its budget.
    assert repeated_exec_guard_error(_CONFIG_DENIAL, counts) is None
    assert repeated_exec_guard_error(_CONFIG_DENIAL, counts) is None
    assert repeated_exec_guard_error(floor, counts) is not None
    assert repeated_exec_guard_error(_CONFIG_DENIAL, counts) is not None


def test_exec_session_input_floor_denial_maps_to_floor_signature():
    denial = str(ExecTool().check_session_input(_FORK_BOMB))
    assert "input was not sent" in denial
    assert exec_guard_violation_signature(denial) == "violation:exec-floor"


def test_protected_path_escalation_has_no_workspace_advice():
    counts: dict[str, int] = {}
    args = {"path": "/root/.nanobot/auth/token.json"}
    for _ in range(2):
        assert repeated_workspace_violation_error(
            "read_file", args, counts, raw_text=_PROTECTED
        ) is None
    third = repeated_workspace_violation_error(
        "read_file", args, counts, raw_text=_PROTECTED
    )
    assert third is not None
    assert "protected internal path" in third
    assert "restrict_to_workspace" not in third
    assert "copy" not in third.lower()
    assert "hard policy boundary" not in third
    assert "Stop retrying" in third


def test_outside_directory_escalation_unchanged():
    args = {"path": "/etc/hosts"}
    with_raw: dict[str, int] = {}
    without_raw: dict[str, int] = {}
    for _ in range(2):
        repeated_workspace_violation_error("read_file", args, with_raw, raw_text=_OUTSIDE)
        repeated_workspace_violation_error("read_file", args, without_raw)
    a = repeated_workspace_violation_error("read_file", args, with_raw, raw_text=_OUTSIDE)
    b = repeated_workspace_violation_error("read_file", args, without_raw)
    assert a == b
    assert a is not None
    assert "restrict_to_workspace" in a and "hard policy boundary" in a


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args,denial,forbidden",
    [
        ("exec", None, None, ("must be updated", "tools.exec")),
        ("read_file", {"path": "/root/.nanobot/auth/token.json"}, _PROTECTED,
         ("restrict_to_workspace", "copy")),
    ],
)
async def test_runner_floor_escalation_is_non_retryable(name, args, denial, forbidden):
    if name == "exec":
        denial = _floor_denial()
    calls = [
        ToolCallRequest(
            id=f"c{i}", name=name,
            arguments=dict(args) if args else {"command": f"{_FORK_BOMB} # {i}"},
        )
        for i in range(4)
    ]
    responses = [LLMResponse(content=f"try {i}", tool_calls=[c]) for i, c in enumerate(calls)]
    responses.append(LLMResponse(content="telling the user", tool_calls=[]))
    provider = MagicMock()
    provider.chat_stream_with_retry = AsyncMock(side_effect=responses)
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value=ToolResult.error(denial))

    result = await AgentRunner().run(make_run_spec(
        provider, initial_messages=[], tools=tools, model="test-model",
        max_iterations=10, max_tool_result_chars=_MAX_CHARS,
    ))

    assert result.stop_reason != "tool_error"
    assert result.error is None
    assert result.final_content == "telling the user"
    escalated = [
        ev for ev in result.tool_events
        if ev["status"] == "error" and "escalated" in ev["detail"]
    ]
    assert escalated
    tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
    text = str(tool_msgs[-1]["content"])
    assert "Stop retrying" in text
    for word in forbidden:
        assert word not in text
