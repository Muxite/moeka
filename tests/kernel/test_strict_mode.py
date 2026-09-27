"""Strict mode (Task 13, closes P2): the kernel fails closed without a real boundary.

- Mechanism 1: a strict env refuses ``exec.run`` (``exec`` and the ``exec_session``
  start path, ``exec`` with ``yield_time_ms``) unless the tool declares an active
  sandbox backend. The refusal carries ``STRICT_SANDBOX_MARKER`` and happens before
  the command reaches the exec guard.
- Mechanism 2: a strict env with an explicit policy drops, at registration, every tool
  whose whole capability surface that policy denies for every resource, so the model
  never sees it.
- Neither applies to a non-strict (legacy) env; a permissive ``DefaultPolicy`` drops
  nothing in either mode.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool, capability_request
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.exec_session import ExecSessionManager
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.gate import AGENT_PRINCIPAL, gate_call
from nanobot.kernel.policy import (
    POLICY_MARKER,
    CapabilityRequest,
    DefaultPolicy,
    IntersectionPolicy,
    policy_denies_everywhere,
)
from nanobot.kernel.strict import STRICT_SANDBOX_MARKER
from nanobot.providers.base import ToolCallRequest
from nanobot.security.protected_paths import PROTECTED_MARKER
from nanobot.session.manager import SessionManager
from nanobot.utils.runtime import _EXEC_GUARD_MARKERS

_FORK_BOMB = ":(){ :|:& };:"


class _EmptyConfig:
    def section(self, name: str) -> dict[str, Any]:
        return {}


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))


def _env(tmp_path: Path, *, strict: bool = True, sink: Any = None) -> CoreEnvironment:
    return CoreEnvironment(
        config=_EmptyConfig(),
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state"),
        trace=sink if sink is not None else RecordingSink(),
        strict=strict,
    )


def _exec(tmp_path: Path, env: CoreEnvironment | None, sandbox: str = "") -> ExecTool:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return ExecTool(working_dir=str(work), sandbox=sandbox, env=env)


class CapTool(Tool):
    """Declares a fixed capability surface; records whether it ran."""

    _capability_names = frozenset({"net.fetch"})

    def __init__(self, name: str = "fetch", surface: frozenset[str] | None = None) -> None:
        self._name = name
        if surface is not None:
            self._capability_names = frozenset(surface)
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"target": {"type": "string"}}}

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [capability_request(c, params.get("target")) for c in sorted(self._capability_names)]

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "ran"


class PlainTool(Tool):
    """Declares no capability at all (empty surface)."""

    @property
    def name(self) -> str:
        return "plain"

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class UndeclaredTool(CapTool):
    """Overrides ``capabilities`` without redeclaring names: surface unknown (None)."""

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [capability_request("net.fetch", params.get("target"))]


_DENY_FETCH = DefaultPolicy(deny_capabilities=frozenset({"net.fetch"}))


# -- the marker ----------------------------------------------------------------


def test_marker_collides_with_no_other_denial_class():
    from nanobot.agent.tools.execution import _SSRF_MARKERS, _WORKSPACE_VIOLATION_MARKERS

    marker = STRICT_SANDBOX_MARKER.lower()
    others = [
        POLICY_MARKER, PROTECTED_MARKER,
        *(m for m, _ in _EXEC_GUARD_MARKERS),
        *_SSRF_MARKERS, *_WORKSPACE_VIOLATION_MARKERS,
        *AgentRunner._SSRF_MARKERS, *AgentRunner._WORKSPACE_VIOLATION_MARKERS,
    ]
    for other in others:
        assert other.lower() not in marker and marker not in other.lower()


# -- mechanism 1: exec refuses without a declared sandbox ----------------------


async def test_strict_exec_without_sandbox_refuses_with_marker(tmp_path):
    tool = _exec(tmp_path, _env(tmp_path))
    out = await tool.execute(command="echo hi")
    assert getattr(out, "is_error", False)
    assert STRICT_SANDBOX_MARKER in out
    assert "tools.exec.sandbox" in out and "do not retry" in out


async def test_strict_exec_session_start_refuses_with_marker(tmp_path):
    manager = ExecSessionManager()
    tool = _exec(tmp_path, _env(tmp_path))
    tool._session_manager = manager
    out = await tool.execute(command="sleep 5", yield_time_ms=10)
    assert STRICT_SANDBOX_MARKER in out
    assert "session_id" not in out.lower()


async def test_strict_refusal_happens_before_the_exec_guard(tmp_path):
    """The command never reaches ``_guard_command`` (a fork bomb gets the strict refusal)."""
    tool = _exec(tmp_path, _env(tmp_path))
    seen: list[str] = []
    original = tool._guard_command

    def spy(command, *args, **kwargs):
        seen.append(command)
        return original(command, *args, **kwargs)

    tool._guard_command = spy  # type: ignore[method-assign]
    out = await tool.execute(command=_FORK_BOMB)
    assert STRICT_SANDBOX_MARKER in out and seen == []


def test_gate_refuses_exec_run_without_sandbox_in_strict_env(tmp_path):
    sink = RecordingSink()
    env = _env(tmp_path, sink=sink)
    result = gate_call(_exec(tmp_path, env), {"command": "ls"}, AGENT_PRINCIPAL,
                       DefaultPolicy(), env)
    assert not result.allowed and result.layer == "gate"
    assert STRICT_SANDBOX_MARKER in result.error_text()
    (event,) = [e for e in sink.events if e.get("event") == "policy.decision"]
    assert event["verdict"] == "deny" and event["marker"] == STRICT_SANDBOX_MARKER


def test_gate_floor_still_runs_first_in_strict_env(tmp_path):
    env = _env(tmp_path)
    result = gate_call(_exec(tmp_path, env), {"command": _FORK_BOMB}, AGENT_PRINCIPAL,
                       DefaultPolicy(), env)
    assert not result.allowed and result.layer == "floor"


async def test_registry_execute_refuses_strict_exec_without_sandbox(tmp_path):
    env = _env(tmp_path)
    registry = ToolRegistry()
    registry.configure_gate(policy=DefaultPolicy(), env=env)
    registry.register(_exec(tmp_path, env))
    out = await registry.execute("exec", {"command": "echo hi"})
    assert STRICT_SANDBOX_MARKER in out


def test_duck_typed_exec_run_tool_without_sandbox_attr_is_refused(tmp_path):
    env = _env(tmp_path)
    result = gate_call(CapTool("bg", frozenset({"exec.run"})), {"target": "ls"},
                       AGENT_PRINCIPAL, DefaultPolicy(), env)
    assert not result.allowed and STRICT_SANDBOX_MARKER in result.error_text()


@pytest.mark.parametrize("backend", ["bwrap", "seatbelt"])
async def test_strict_exec_with_sandbox_passes_strict_check(tmp_path, backend, monkeypatch):
    import nanobot.agent.tools.shell as shell

    monkeypatch.setattr(shell, "_IS_WINDOWS", False)
    env = _env(tmp_path)
    tool = _exec(tmp_path, env, sandbox=backend)
    assert tool.sandbox_active
    gate = gate_call(tool, {"command": "ls"}, AGENT_PRINCIPAL, DefaultPolicy(), env)
    assert gate.allowed
    # Gated as before: the floor still denies, the policy still decides.
    assert gate_call(tool, {"command": _FORK_BOMB}, AGENT_PRINCIPAL, DefaultPolicy(),
                     env).layer == "floor"
    denied = gate_call(tool, {"command": "ls"}, AGENT_PRINCIPAL,
                       DefaultPolicy(deny_capabilities=frozenset({"exec.run"})), env)
    assert denied.layer == "policy"
    # execute() goes past the strict check into the usual prepare/guard path.
    prepared = tool._prepare_command("ls")
    assert not isinstance(prepared, str)
    wrapper = {"bwrap": "bwrap", "seatbelt": "/usr/bin/sandbox-exec"}[backend]
    assert prepared.command.split()[0] == wrapper
    tool._prepare_command = lambda *a, **k: "prepared-sentinel"  # type: ignore[method-assign]
    assert await tool.execute(command="ls") == "prepared-sentinel"


def test_unknown_or_windows_sandbox_is_not_active(tmp_path, monkeypatch):
    import nanobot.agent.tools.shell as shell

    env = _env(tmp_path)
    assert not _exec(tmp_path, env, sandbox="nosuch").sandbox_active
    monkeypatch.setattr(shell, "_IS_WINDOWS", True)
    assert not _exec(tmp_path, env, sandbox="bwrap").sandbox_active


@pytest.mark.parametrize("env_kind", ["legacy-none", "non-strict"])
async def test_non_strict_exec_runs_as_today(tmp_path, env_kind):
    env = None if env_kind == "legacy-none" else _env(tmp_path, strict=False)
    tool = _exec(tmp_path, env)
    out = await tool.execute(command="echo strict-off")
    assert "strict-off" in out and STRICT_SANDBOX_MARKER not in out
    gate = gate_call(tool, {"command": "echo x"}, AGENT_PRINCIPAL, DefaultPolicy(), env,
                     workspace=tmp_path)
    assert gate.allowed


def test_runner_returns_strict_refusal_verbatim_without_retry_hint():
    runner = AgentRunner()
    call = ToolCallRequest(id="c", name="exec", arguments={})
    text = f"Error: exec refused: {STRICT_SANDBOX_MARKER} (x)."
    event: dict[str, str] = {}
    handled = runner._classify_violation(
        raw_text=text, soft_payload=text + "\n\n[try a different approach]", event=event,
        tool_call=call, workspace_violation_counts={},
    )
    assert handled is not None
    assert handled[0] == text
    assert event["detail"].startswith("strict_sandbox: ")


# -- mechanism 2: fully-denied tools are dropped at registration ---------------


def test_denies_everywhere_helper():
    assert policy_denies_everywhere(_DENY_FETCH, "net.fetch")
    assert not policy_denies_everywhere(_DENY_FETCH, "fs.read")
    assert not policy_denies_everywhere(DefaultPolicy(), "net.fetch")
    assert policy_denies_everywhere(DefaultPolicy(allowed=frozenset({"fs.read"})), "net.fetch")
    # A per-resource rule is not "everywhere" unless its glob matches every resource.
    assert not policy_denies_everywhere(
        DefaultPolicy(deny_rules=(("net.fetch", "*.internal"),)), "net.fetch")
    assert policy_denies_everywhere(DefaultPolicy(deny_rules=(("net.fetch", "*"),)), "net.fetch")
    # Intersection: denied everywhere when any member denies it everywhere.
    both = IntersectionPolicy(DefaultPolicy(), _DENY_FETCH)
    assert policy_denies_everywhere(both, "net.fetch")
    assert not policy_denies_everywhere(both, "fs.read")

    class DecideOnly:
        def decide(self, principal, req, ctx):
            raise AssertionError("never consulted")

    # A policy that cannot answer is never assumed to deny everywhere.
    assert not policy_denies_everywhere(DecideOnly(), "net.fetch")


def test_strict_drops_tool_whose_only_capability_is_denied_everywhere(tmp_path):
    sink = RecordingSink()
    registry = ToolRegistry()
    registry.configure_gate(policy=_DENY_FETCH, env=_env(tmp_path, sink=sink))
    registry.register(CapTool("fetch"))
    assert not registry.has("fetch")
    assert "fetch" not in [ToolRegistry._schema_name(s) for s in registry.get_definitions()]
    dropped = [e for e in sink.events if e.get("event") == "tool.dropped"]
    assert [e["tool"] for e in dropped] == ["fetch"]


def test_strict_keeps_mixed_empty_and_undeclared_surfaces(tmp_path):
    registry = ToolRegistry()
    registry.configure_gate(policy=_DENY_FETCH, env=_env(tmp_path))
    registry.register(CapTool("mixed", frozenset({"net.fetch", "mcp.call"})))
    registry.register(PlainTool())
    registry.register(UndeclaredTool("undeclared"))
    assert sorted(registry.tool_names) == ["mixed", "plain", "undeclared"]


def test_configure_gate_after_register_prunes_fully_denied_tools(tmp_path):
    registry = ToolRegistry()
    registry.register(CapTool("fetch"))
    registry.register(CapTool("mixed", frozenset({"net.fetch", "fs.read"})))
    definitions_before = registry.get_definitions()
    assert len(definitions_before) == 2
    registry.configure_gate(policy=_DENY_FETCH, env=_env(tmp_path))
    assert registry.tool_names == ["mixed"]
    assert [ToolRegistry._schema_name(s) for s in registry.get_definitions()] == ["mixed"]


async def test_dropped_tool_is_not_callable(tmp_path):
    registry = ToolRegistry()
    registry.configure_gate(policy=_DENY_FETCH, env=_env(tmp_path))
    tool = CapTool("fetch")
    registry.register(tool)
    out = await registry.execute("fetch", {"target": "x"})
    assert "not found" in out and tool.calls == []


@pytest.mark.parametrize("strict,policy", [(False, _DENY_FETCH), (True, None)])
def test_no_drop_without_strict_or_without_explicit_policy(tmp_path, strict, policy):
    registry = ToolRegistry()
    registry.configure_gate(policy=policy, env=_env(tmp_path, strict=strict))
    registry.register(CapTool("fetch"))
    assert registry.has("fetch")


def test_loader_does_not_report_a_dropped_tool_as_registered(tmp_path):
    ctx = _tool_ctx(tmp_path, _env(tmp_path))
    registry = ToolRegistry()
    registry.configure_gate(
        policy=DefaultPolicy(deny_capabilities=frozenset({"net.fetch"})), env=ctx.env,
    )
    names = ToolLoader().load(ctx, registry)
    assert "web_fetch" not in names and "web_search" not in names
    assert sorted(names) == sorted(registry.tool_names)
    assert "exec" in names and "read_file" in names


def _tool_ctx(tmp_path: Path, env: CoreEnvironment | None) -> ToolContext:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        config=ToolsConfig(),
        workspace=str(workspace),
        bus=MessageBus(),
        subagent_manager=SimpleNamespace(
            get_running_count=lambda: 0, max_concurrent_subagents=4,
        ),
        exec_session_manager=ExecSessionManager(),
        sessions=SessionManager(workspace, sessions_root=tmp_path / "runtime"),
        timezone="UTC",
        env=env,
    )


def _load(tmp_path: Path, env: CoreEnvironment | None, policy: Any) -> list[str]:
    registry = ToolRegistry()
    registry.configure_gate(policy=policy, env=env)
    ToolLoader().load(_tool_ctx(tmp_path, env), registry)
    return sorted(registry.tool_names)


def test_permissive_default_policy_registers_every_tool_in_both_modes(tmp_path):
    """Regression: a permissive DefaultPolicy drops nothing, strict or not."""
    baseline = _load(tmp_path / "a", None, None)
    assert "exec" in baseline and len(baseline) > 10
    assert _load(tmp_path / "b", _env(tmp_path / "b", strict=False), DefaultPolicy()) == baseline
    assert _load(tmp_path / "c", _env(tmp_path / "c", strict=True), DefaultPolicy()) == baseline


# -- the two mechanisms are independent ----------------------------------------


async def test_strict_no_sandbox_refuses_exec_even_when_policy_allows_it(tmp_path):
    env = _env(tmp_path)
    registry = ToolRegistry()
    registry.configure_gate(policy=_DENY_FETCH, env=env)
    registry.register(_exec(tmp_path, env))
    registry.register(CapTool("fetch"))
    assert registry.has("exec") and not registry.has("fetch")
    out = await registry.execute("exec", {"command": "echo hi"})
    assert STRICT_SANDBOX_MARKER in out


def test_strict_drops_exec_when_exec_run_is_denied_everywhere(tmp_path):
    env = _env(tmp_path)
    registry = ToolRegistry()
    registry.configure_gate(
        policy=DefaultPolicy(deny_capabilities=frozenset({"exec.run"})), env=env,
    )
    registry.register(_exec(tmp_path, env))
    assert not registry.has("exec")
