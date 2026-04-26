"""Tests for subagent exec tool configuration."""

from pathlib import Path

import pytest

from nanobot.agent.runner import AgentRunResult
from nanobot.agent.subagent import SubagentManager, SubagentStatus
from nanobot.agent.tools.shell import ExecTool
from nanobot.config.schema import ExecToolConfig, WebToolsConfig


class _Bus:
    """Capture subagent announcements without running the full message bus."""

    def __init__(self):
        self.inbound = []

    async def publish_inbound(self, msg):
        self.inbound.append(msg)


class _Provider:
    """Minimal provider stub for SubagentManager construction."""

    def get_default_model(self):
        return "test-model"


@pytest.mark.asyncio
async def test_subagent_exec_tool_uses_exec_config(tmp_path, monkeypatch):
    """Subagents should receive the same exec config fields as the main agent."""
    manager = SubagentManager(
        provider=_Provider(),
        workspace=Path(tmp_path),
        bus=_Bus(),
        max_tool_result_chars=1000,
        web_config=WebToolsConfig(enable=False),
        exec_config=ExecToolConfig(
            timeout=123,
            path_append="/opt/custom/bin",
            sandbox="",
            allowed_env_keys=["MY_CUSTOM_VAR"],
            allow_sudo=True,
        ),
        restrict_to_workspace=True,
    )
    monkeypatch.setattr(manager, "_build_subagent_prompt", lambda: "subagent prompt")

    captured = {}

    async def fake_run(spec):
        captured["exec_tool"] = spec.tools.get("exec")
        return AgentRunResult(final_content="done", messages=[])

    monkeypatch.setattr(manager.runner, "run", fake_run)

    status = SubagentStatus(
        task_id="task-1",
        label="label",
        task_description="task",
        started_at=0.0,
    )
    await manager._run_subagent(
        "task-1",
        "task",
        "label",
        {"channel": "cli", "chat_id": "direct"},
        status,
    )

    exec_tool = captured["exec_tool"]
    assert isinstance(exec_tool, ExecTool)
    assert exec_tool.timeout == 123
    assert exec_tool.path_append == "/opt/custom/bin"
    assert exec_tool.allowed_env_keys == ["MY_CUSTOM_VAR"]
    assert exec_tool.allow_sudo is True
    assert exec_tool.restrict_to_workspace is True
