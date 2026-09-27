"""Sub-agent attenuation (I4): a child never gets more than its parent grants.

- The child policy is ``parent.attenuate(requested, narrower=child_policy)`` where
  ``requested`` is the static capability surface of the child's own tool registry,
  minus ``session.send`` (denied for sub-agents by default).
- Both spawn entry points (``spawn`` and ``run_inline``) go through
  ``_run_admitted_subagent``, so both are checked here.
- The property test drives the real ``SubagentManager`` construction path
  (``_build_tools`` + ``_child_policy``) with random parents, tool selections and
  narrowers, then attenuates once more as a grandchild manager would.
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.runner import AgentRunResult
from nanobot.agent.subagent import SUBAGENT_DENIED_BY_DEFAULT, SubagentManager, SubagentStatus
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.exec_session import ExecSessionManager
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.kernel.gate import gate_call
from nanobot.kernel.policy import (
    Allow,
    CapabilityRequest,
    DefaultPolicy,
    Deny,
    IntersectionPolicy,
    PermissionPolicy,
    Principal,
)
from nanobot.providers.base import GenerationSettings, LLMProvider
from nanobot.utils.llm_runtime import LLMRuntime
from tests.kernel.test_capability_surface import EXPECTED_SURFACE
from tests.kernel.test_policy import UNIVERSE, _random_policy
from tests.kernel.test_tool_capabilities import EXPECTED

SUB = Principal("subagent:t1", "subagent")
RESOURCES = ("r", "/etc/passwd", "/tmp/x", "https://example.com", "", "@ops")

# The subagent-scope tools (``_scopes`` contains "subagent") with default config.
SUBAGENT_TOOLS = sorted([
    "apply_patch", "edit_file", "exec", "exec_session", "find_files", "grep", "list_dir",
    "list_exec_sessions", "read_file", "web_fetch", "web_search", "write_file",
])


def _allowed_pairs(policy: PermissionPolicy, principal: Principal = SUB) -> frozenset:
    return frozenset(
        (c, r) for c in UNIVERSE for r in RESOURCES
        if isinstance(policy.decide(principal, CapabilityRequest(c, r), None), Allow)
    )


def _allowed_caps(policy: PermissionPolicy) -> frozenset[str]:
    return frozenset(c for c, _ in _allowed_pairs(policy))


def _manager(tmp_path: Path, **kw: Any) -> SubagentManager:
    return SubagentManager(
        workspace=tmp_path, bus=MessageBus(), max_tool_result_chars=16_000, **kw,
    )


def _runtime() -> LLMRuntime:
    provider = MagicMock(spec=LLMProvider)
    provider.generation = GenerationSettings(temperature=0.1, max_tokens=4096)
    return LLMRuntime.capture(provider, "test-model", context_window_tokens=128_000)


def _ok_runner(sm: SubagentManager) -> None:
    sm.runner.run = AsyncMock(return_value=AgentRunResult(
        final_content="ok", messages=[], stop_reason="completed",
    ))


async def _spawned_spec(sm: SubagentManager) -> Any:
    _ok_runner(sm)
    with patch.object(sm, "_announce_result", new_callable=AsyncMock):
        await sm._run_subagent(
            "t1", "do task", "label", {"channel": "cli", "chat_id": "direct",
                                       "session_key": None},
            SubagentStatus(task_id="t1", label="label", task_description="do task",
                           started_at=time.monotonic()),
            _runtime(),
        )
    return sm.runner.run.await_args.args[0]


class _Plain(Tool):
    name = "plain"  # pyright: ignore[reportAssignmentType]
    description = "plain"  # pyright: ignore[reportAssignmentType]
    parameters = {"type": "object", "properties": {}}  # pyright: ignore[reportAssignmentType]

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class _Undeclared(_Plain):
    name = "undeclared"  # pyright: ignore[reportAssignmentType]

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [CapabilityRequest("mcp.call", "x")]


def test_undeclared_tool_is_probed_with_empty_params(tmp_path):
    sm = _manager(tmp_path)
    registry = ToolRegistry()
    registry.register(_Undeclared())
    registry.register(_Plain())
    assert sm._requested_capabilities(registry) == frozenset({"mcp.call"})


# --- the child policy ---------------------------------------------------------------


async def test_spawn_spec_policy_is_attenuated_to_the_tool_surface(tmp_path):
    spec = await _spawned_spec(_manager(tmp_path))
    surface = frozenset().union(*(EXPECTED_SURFACE[n] for n in SUBAGENT_TOOLS))
    assert sorted(spec.tools.tool_names) == SUBAGENT_TOOLS
    assert _allowed_caps(spec.policy) == surface
    for denied in ("session.read", "session.send", "mcp.call", "secret.read", "plugin.load"):
        assert denied not in _allowed_caps(spec.policy)


@pytest.mark.parametrize("allow, expected", [
    (["read_file"], {"fs.read"}),
    (["read_file", "exec"], {"fs.read", "exec.run"}),
    (["web_fetch", "list_exec_sessions"], {"net.fetch"}),
    ([], set()),
])
async def test_tools_allow_narrows_the_capability_ceiling(tmp_path, allow, expected):
    spec = await _spawned_spec(_manager(tmp_path, tools_allow=allow))
    assert _allowed_caps(spec.policy) == frozenset(expected)
    assert _allowed_pairs(spec.policy) <= _allowed_pairs(DefaultPolicy())


async def test_tools_deny_narrows_the_capability_ceiling(tmp_path):
    spec = await _spawned_spec(_manager(tmp_path, tools_deny=["exec", "exec_session"]))
    assert "exec.run" not in _allowed_caps(spec.policy)
    assert "exec.session_input" not in _allowed_caps(spec.policy)


async def test_child_is_strict_subset_of_restrictive_parent(tmp_path):
    parent = DefaultPolicy(
        deny_capabilities=frozenset({"net.fetch"}),
        deny_rules=(("fs.read", "/etc/*"),),
    )
    spec = await _spawned_spec(_manager(tmp_path, policy=parent))
    child = _allowed_pairs(spec.policy)
    assert child < _allowed_pairs(parent)
    assert ("fs.read", "/etc/passwd") not in child
    assert "net.fetch" not in _allowed_caps(spec.policy)


async def test_parent_subclass_rules_survive(tmp_path):
    class _NoExec(DefaultPolicy):
        def decide(self, principal, req, ctx):
            if req.capability == "exec.run":
                return Deny(reason="no exec", marker="no exec", capability="exec.run")
            return super().decide(principal, req, ctx)

    spec = await _spawned_spec(_manager(tmp_path, policy=_NoExec()))
    assert "exec.run" not in _allowed_caps(spec.policy)


async def test_narrower_child_policy_is_intersected(tmp_path):
    narrower = DefaultPolicy(deny_capabilities=frozenset({"fs.write", "exec.run"}))
    spec = await _spawned_spec(_manager(tmp_path, child_policy=narrower))
    caps = _allowed_caps(spec.policy)
    assert "fs.write" not in caps and "exec.run" not in caps
    assert "fs.read" in caps


async def test_session_send_denied_by_default(tmp_path):
    assert "session.send" in SUBAGENT_DENIED_BY_DEFAULT
    sm = _manager(tmp_path)
    spec = await _spawned_spec(sm)
    decision = spec.policy.decide(SUB, CapabilityRequest("session.send", "@ops"), None)
    assert isinstance(decision, Deny)
    # Even a child registry that contains a session.send tool does not get it.
    from nanobot.agent.tools.session_messages import SendSessionMessageTool

    registry = ToolRegistry()
    registry.register(SendSessionMessageTool.__new__(SendSessionMessageTool))
    child = sm._child_policy(registry)
    assert "session.send" not in _allowed_caps(child)


async def test_run_inline_also_attenuates(tmp_path):
    parent = DefaultPolicy(deny_capabilities=frozenset({"exec.run"}))
    sm = _manager(tmp_path, policy=parent, tools_allow=["read_file", "exec", "web_fetch"])
    _ok_runner(sm)
    result = await sm.run_inline("do task", runtime=_runtime())
    assert result == "ok"
    spec = sm.runner.run.await_args.args[0]
    assert _allowed_caps(spec.policy) == frozenset({"fs.read", "net.fetch"})
    assert spec.principal.kind == "subagent"


async def test_child_registry_gate_uses_the_child_policy(tmp_path):
    spec = await _spawned_spec(_manager(tmp_path))
    assert spec.tools.gate_policy is spec.policy
    assert spec.tools.gate_principal is spec.principal


async def test_permissive_parent_regression(tmp_path):
    """Under today's DefaultPolicy parent, every Task 8 call a sub-agent tool can make
    gets the same gate decision as before (the parent's policy, unattenuated)."""
    spec = await _spawned_spec(_manager(tmp_path))
    for name in spec.tools.tool_names:
        tool = spec.tools.get(name)
        for params, _ in EXPECTED[name]:
            before = gate_call(tool, params, spec.principal, DefaultPolicy(), None,
                               workspace=tmp_path)
            after = gate_call(tool, params, spec.principal, spec.policy, None,
                              workspace=tmp_path)
            assert after.allowed == before.allowed, f"{name} {params!r}"
            assert after.allowed, f"{name} {params!r}"


# --- per-child exec-session quota ---------------------------------------------------


async def test_each_child_gets_its_own_exec_session_manager(tmp_path):
    sm = _manager(tmp_path)
    first = (await _spawned_spec(sm)).tools.get("exec")._session_manager
    second = (await _spawned_spec(sm)).tools.get("exec")._session_manager
    assert first is not second
    assert first is not sm._exec_session_manager
    # Empty per-child managers are released when the run ends.
    assert sm._child_exec_managers == {}


async def test_child_exec_manager_with_live_sessions_is_kept_and_closed(tmp_path):
    sm = _manager(tmp_path)
    kept = MagicMock(spec=ExecSessionManager)
    kept.list = AsyncMock(return_value=[object()])
    kept.close_all = AsyncMock(return_value=1)
    kept.terminate_by_owner = AsyncMock(return_value=1)
    with patch("nanobot.agent.subagent.ExecSessionManager", return_value=kept):
        await _spawned_spec(sm)
    assert list(sm._child_exec_managers.values()) == [kept]
    await sm.cancel_by_session("cli:direct")
    kept.terminate_by_owner.assert_awaited_once_with("cli:direct")
    await sm.close()
    kept.close_all.assert_awaited_once()
    assert sm._child_exec_managers == {}


# --- property test: through the SubagentManager construction path ------------------


def _root(rng: random.Random) -> PermissionPolicy:
    parent = _random_policy(rng)
    if not isinstance(parent, (DefaultPolicy, IntersectionPolicy)):
        # The kernel only ever holds a foreign root as an IntersectionPolicy member.
        parent = IntersectionPolicy(parent)
    return parent


def test_child_and_grandchild_never_broaden_property(tmp_path):
    """For random parents, tool selections and narrowers, the child's effective
    ``decide`` never allows a (capability, resource) pair the parent denies, and a
    grandchild manager built on the child's policy never exceeds the child."""
    rng = random.Random(11)
    for i in range(60):
        parent = _root(rng)
        allow = (
            None if rng.random() < 0.3
            else rng.sample(SUBAGENT_TOOLS, rng.randint(0, len(SUBAGENT_TOOLS)))
        )
        deny = rng.sample(SUBAGENT_TOOLS, rng.randint(0, 3))
        narrower = _random_policy(rng) if rng.random() < 0.5 else None
        sm = _manager(tmp_path, policy=parent, tools_allow=allow, tools_deny=deny,
                      child_policy=narrower)
        tools = sm._build_tools()
        child = sm._child_policy(tools)
        surface = sm._requested_capabilities(tools)

        grand_sm = _manager(tmp_path, policy=child,
                            tools_allow=rng.sample(SUBAGENT_TOOLS, rng.randint(0, 12)))
        grand = grand_sm._child_policy(grand_sm._build_tools())

        for principal in (SUB, Principal("host", "host")):
            got = _allowed_pairs(child, principal)
            assert got <= _allowed_pairs(parent, principal), i
            assert {c for c, _ in got} <= surface - SUBAGENT_DENIED_BY_DEFAULT, i
            if narrower is not None:
                assert got <= _allowed_pairs(narrower, principal), i
            assert _allowed_pairs(grand, principal) <= got, i
