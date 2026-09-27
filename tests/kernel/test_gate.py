"""The capability gate at the tool-execution choke points (Task 9).

- ``gate_call`` runs ``Tool.capabilities`` -> floors -> policy for every request.
- Both call paths (``AgentRunner._run_tool`` and ``ToolRegistry.execute``) gate.
- A denied call never reaches a hook and never executes the tool.
- Every request emits a ``policy.decision`` event; a raising sink changes nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.hook import AgentHook
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool, capability_request
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import AgentDefaults
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.gate import (
    AGENT_PRINCIPAL,
    GateResult,
    _normalize_fs_resource,
    emit_tool_invalid,
    gate_call,
)
from nanobot.kernel.policy import (
    POLICY_MARKER,
    Allow,
    CapabilityRequest,
    DefaultPolicy,
    Deny,
    Principal,
)
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.security.protected_paths import PROTECTED_MARKER
from tests._trace import unstamped
from tests.agent.runner_helpers import make_run_spec

_MAX_CHARS = AgentDefaults().max_tool_result_chars
_FORK_BOMB = ":(){ :|:& };:"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(unstamped(event))

    def decisions(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("event") == "policy.decision"]


class RaisingSink:
    def emit(self, event: dict[str, Any]) -> None:
        raise RuntimeError("sink is down")


class _EmptyConfig:
    def section(self, name: str) -> dict[str, Any]:
        return {}


def _env(tmp_path: Path, sink: Any = None) -> CoreEnvironment:
    return CoreEnvironment(
        config=_EmptyConfig(),
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state"),
        trace=sink if sink is not None else RecordingSink(),
        strict=True,
    )


class CapTool(Tool):
    """Declares one capability per call; records whether it ran."""

    def __init__(self, name: str = "fetch", capability: str = "net.fetch", key: str = "target"):
        self._name = name
        self._capability = capability
        self._key = key
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {self._key: {"type": "string"}}}

    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [capability_request(self._capability, params.get(self._key))]

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "ran"


class MultiCapTool(CapTool):
    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        return [capability_request("fs.read", p) for p in params.get("paths") or []]


class WorkspaceCapTool(CapTool):
    """Like the file tools: relative paths resolve against the tool's own workspace."""

    def __init__(self, workspace: Path, capability: str = "fs.read"):
        super().__init__(name="read_thing", capability=capability, key="path")
        self._workspace = workspace


class BrokenCapTool(CapTool):
    def capabilities(self, params: dict[str, Any]) -> list[CapabilityRequest]:
        raise RuntimeError("boom")


class SpyPolicy(DefaultPolicy):
    def __init__(self, deny: set[str] | None = None) -> None:
        super().__init__()
        self.deny = deny or set()
        self.seen: list[CapabilityRequest] = []

    def decide(self, principal: Principal, req: CapabilityRequest, ctx: Any) -> Allow | Deny:
        self.seen.append(req)
        if req.capability in self.deny:
            from nanobot.kernel.policy import policy_deny

            return policy_deny(principal, req, "test policy")
        return super().decide(principal, req, ctx)


class SpyHook(AgentHook):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def before_execute_tool(self, context, tool_call, tool, params) -> None:
        self.calls.append("before_execute_tool")

    async def after_execute_tool(self, context, tool_call, tool, params, result) -> None:
        self.calls.append("after_execute_tool")

    async def on_execute_tool_error(self, context, tool_call, tool, params, error) -> None:
        self.calls.append("on_execute_tool_error")


def _provider(*calls: ToolCallRequest) -> MagicMock:
    provider = MagicMock()
    responses = [LLMResponse(content="calling", tool_calls=[c]) for c in calls]
    responses.append(LLMResponse(content="done", tool_calls=[]))
    provider.chat_stream_with_retry = AsyncMock(side_effect=responses)
    return provider


async def _run(tools: ToolRegistry, *calls: ToolCallRequest, **spec: Any):
    return await AgentRunner().run(make_run_spec(
        _provider(*calls),
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=len(calls) + 2,
        max_tool_result_chars=_MAX_CHARS,
        **spec,
    ))


def _tool_messages(result) -> list[str]:
    return [m["content"] for m in result.messages if m.get("role") == "tool"]


# -- gate_call ----------------------------------------------------------------


def test_allowed_call_emits_policy_decision(tmp_path):
    sink = RecordingSink()
    tool = CapTool()
    result = gate_call(tool, {"target": "example.com"}, AGENT_PRINCIPAL, DefaultPolicy(),
                       _env(tmp_path, sink))
    assert isinstance(result, GateResult)
    assert result.allowed and isinstance(result.decision, Allow)
    assert sink.decisions() == [{
        "event": "policy.decision",
        "actor": "agent",
        "principal_kind": "agent",
        "tool": "fetch",
        "capability": "net.fetch",
        "resource": "example.com",
        "verdict": "allow",
        "layer": None,
        "marker": None,
    }]


def test_policy_deny_event_content(tmp_path):
    sink = RecordingSink()
    principal = Principal(name="worker", kind="subagent")
    result = gate_call(CapTool(), {"target": "example.com"}, principal,
                       SpyPolicy(deny={"net.fetch"}), _env(tmp_path, sink))
    assert not result.allowed
    assert result.layer == "policy"
    assert isinstance(result.decision, Deny)
    assert POLICY_MARKER in result.error_text()
    assert result.error_text().startswith("Error:")
    assert sink.decisions() == [{
        "event": "policy.decision",
        "actor": "worker",
        "principal_kind": "subagent",
        "tool": "fetch",
        "capability": "net.fetch",
        "resource": "example.com",
        "verdict": "deny",
        "layer": "policy",
        "marker": POLICY_MARKER,
    }]


def test_floor_deny_short_circuits_policy(tmp_path):
    sink = RecordingSink()
    policy = SpyPolicy()
    tool = CapTool(name="exec", capability="exec.run", key="command")
    result = gate_call(tool, {"command": _FORK_BOMB}, AGENT_PRINCIPAL, policy,
                       _env(tmp_path, sink))
    assert not result.allowed and result.layer == "floor"
    assert policy.seen == []
    assert "this guard is not configurable" in result.error_text().lower()
    (event,) = sink.decisions()
    assert event["verdict"] == "deny" and event["layer"] == "floor"
    assert event["marker"] == "blocked by safety guard (dangerous pattern detected)"


@pytest.mark.parametrize("verdict_policy", [DefaultPolicy(), SpyPolicy(deny={"net.fetch"})])
def test_raising_sink_does_not_change_verdict(tmp_path, verdict_policy):
    params = {"target": "example.com"}
    quiet = gate_call(CapTool(), params, AGENT_PRINCIPAL, verdict_policy,
                      _env(tmp_path, RecordingSink()))
    loud = gate_call(CapTool(), params, AGENT_PRINCIPAL, verdict_policy,
                     _env(tmp_path, RaisingSink()))
    assert loud.allowed == quiet.allowed
    assert loud.decision == quiet.decision


def test_first_deny_wins_and_every_evaluated_request_is_audited(tmp_path):
    sink = RecordingSink()
    env = _env(tmp_path, sink)
    tool = MultiCapTool(name="multi")
    secret = str(env.paths.state_dir / "x")
    result = gate_call(tool, {"paths": ["a.txt", secret, "b.txt"]}, AGENT_PRINCIPAL,
                       DefaultPolicy(), env)
    assert not result.allowed and result.layer == "floor"
    verdicts = [(e["resource"], e["verdict"]) for e in sink.decisions()]
    assert verdicts == [(str(env.paths.work_dir / "a.txt"), "allow"), (secret, "deny")]


def test_capabilities_that_raise_fail_closed(tmp_path):
    sink = RecordingSink()
    result = gate_call(BrokenCapTool(), {}, AGENT_PRINCIPAL, DefaultPolicy(),
                       _env(tmp_path, sink))
    assert not result.allowed
    assert POLICY_MARKER in result.error_text()
    assert sink.decisions()[0]["verdict"] == "deny"


def test_duck_typed_tool_without_capabilities_declares_nothing(tmp_path):
    class Duck:
        name = "duck"

    sink = RecordingSink()
    result = gate_call(Duck(), {}, AGENT_PRINCIPAL, DefaultPolicy(), _env(tmp_path, sink))
    assert result.allowed and sink.decisions() == []


def test_tool_invalid_event(tmp_path):
    sink = RecordingSink()
    emit_tool_invalid(
        _env(tmp_path, sink), "nope", AGENT_PRINCIPAL, "Error: Tool 'nope' not found",
        call_id="c9",
    )
    assert sink.events == [{
        "event": "tool.invalid",
        "actor": "agent",
        "principal_kind": "agent",
        "tool": "nope",
        "call_id": "c9",
        "error": "Error: Tool 'nope' not found",
    }]
    emit_tool_invalid(_env(tmp_path, RaisingSink()), "nope", AGENT_PRINCIPAL, "x")  # no raise


# -- Ruling H: fs resources are normalised against the workspace -------------


def test_normalize_fs_resource(tmp_path):
    ws = tmp_path / "ws"
    assert _normalize_fs_resource("a/b.txt", ws) == str(ws / "a" / "b.txt")
    assert _normalize_fs_resource("  a.txt \n", ws) == str(ws / "a.txt")
    assert _normalize_fs_resource("../x", ws) == str(tmp_path / "x")
    assert _normalize_fs_resource("/abs/p", ws) == "/abs/p"
    assert _normalize_fs_resource("", ws) == ""


def test_relative_fs_path_is_resolved_against_workspace_not_cwd(tmp_path, monkeypatch):
    """``../state/secret`` is the state dir only relative to the workspace.

    Relative to the process cwd (moved elsewhere here) it names an unprotected
    path, so an unnormalised resource would wrongly pass the floor.
    """
    elsewhere = tmp_path / "elsewhere" / "deep"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    sink = RecordingSink()
    env = _env(tmp_path, sink)
    env.paths.work_dir.mkdir()
    tool = WorkspaceCapTool(env.paths.work_dir)
    result = gate_call(tool, {"path": "../state/secret.txt"}, AGENT_PRINCIPAL,
                       DefaultPolicy(), env)
    assert not result.allowed and result.layer == "floor"
    assert PROTECTED_MARKER in result.error_text()
    (event,) = sink.decisions()
    assert event["resource"] == str(env.paths.state_dir / "secret.txt")


def test_relative_fs_path_reaches_policy_normalised(tmp_path):
    env = _env(tmp_path)
    policy = DefaultPolicy(deny_rules=(("fs.write", str(env.paths.work_dir / "locked*")),))
    tool = WorkspaceCapTool(env.paths.work_dir, capability="fs.write")
    denied = gate_call(tool, {"path": " locked.txt "}, AGENT_PRINCIPAL, policy, env)
    assert not denied.allowed and denied.layer == "policy"
    assert gate_call(tool, {"path": "open.txt"}, AGENT_PRINCIPAL, policy, env).allowed


# -- runner call path ------------------------------------------------------------


async def test_runner_denied_call_never_reaches_hooks_or_tool(tmp_path):
    sink = RecordingSink()
    tool = CapTool()
    tools = ToolRegistry()
    tools.register(tool)
    hook = SpyHook()
    result = await _run(
        tools,
        ToolCallRequest(id="c1", name="fetch", arguments={"target": "example.com"}),
        hook=hook,
        policy=SpyPolicy(deny={"net.fetch"}),
        env=_env(tmp_path, sink),
    )
    assert tool.calls == []
    assert hook.calls == []
    (content,) = _tool_messages(result)
    assert POLICY_MARKER in content
    assert result.tool_events[0]["status"] == "error"
    assert result.tool_events[0]["detail"].startswith("policy_denial: ")
    assert [e["verdict"] for e in sink.decisions()] == ["deny"]


async def test_runner_allowed_call_is_audited_and_runs_hooks(tmp_path):
    sink = RecordingSink()
    tool = CapTool()
    tools = ToolRegistry()
    tools.register(tool)
    hook = SpyHook()
    await _run(
        tools,
        ToolCallRequest(id="c1", name="fetch", arguments={"target": "example.com"}),
        hook=hook,
        env=_env(tmp_path, sink),
    )
    assert tool.calls == [{"target": "example.com"}]
    assert hook.calls == ["before_execute_tool", "after_execute_tool"]
    assert [e["verdict"] for e in sink.decisions()] == ["allow"]


async def test_runner_prep_error_emits_tool_invalid(tmp_path):
    sink = RecordingSink()
    tools = ToolRegistry()
    tools.register(CapTool())
    await _run(tools, ToolCallRequest(id="c1", name="missing", arguments={}),
               env=_env(tmp_path, sink))
    (event,) = [e for e in sink.events if e["event"] == "tool.invalid"]
    assert event["tool"] == "missing" and "not found" in event["error"]


async def test_runner_and_registry_gate_identically(tmp_path):
    policy = SpyPolicy(deny={"net.fetch"})
    params = {"target": "example.com"}

    runner_sink = RecordingSink()
    runner_tool = CapTool()
    runner_tools = ToolRegistry()
    runner_tools.register(runner_tool)
    result = await _run(runner_tools, ToolCallRequest(id="c1", name="fetch", arguments=params),
                        policy=policy, env=_env(tmp_path, runner_sink))

    registry_sink = RecordingSink()
    registry_tool = CapTool()
    registry = ToolRegistry()
    registry.register(registry_tool)
    registry.configure_gate(policy=policy, env=_env(tmp_path, registry_sink))
    direct = await registry.execute("fetch", params)

    assert runner_tool.calls == [] and registry_tool.calls == []
    assert getattr(direct, "is_error", False)
    deny = gate_call(CapTool(), params, AGENT_PRINCIPAL, policy, _env(tmp_path))
    assert deny.error_text() in _tool_messages(result)[0]
    assert deny.error_text() in direct
    assert runner_sink.decisions() == registry_sink.decisions()


async def test_registry_execute_floor_gates_by_default(tmp_path):
    """No configure_gate: DefaultPolicy + floors still apply on the direct path."""
    tool = CapTool(name="exec", capability="exec.run", key="command")
    registry = ToolRegistry()
    registry.register(tool)
    out = await registry.execute("exec", {"command": _FORK_BOMB})
    assert tool.calls == []
    assert "this guard is not configurable" in out.lower()


async def test_execution_module_path_also_gates(tmp_path):
    from nanobot.agent.hook import AgentHookContext
    from nanobot.agent.tools.execution import execute_tool_calls

    sink = RecordingSink()
    tool = CapTool()
    registry = ToolRegistry()
    registry.register(tool)
    registry.configure_gate(policy=SpyPolicy(deny={"net.fetch"}), env=_env(tmp_path, sink))
    hook = SpyHook()
    results, _events = await execute_tool_calls(
        registry,
        [ToolCallRequest(id="c1", name="fetch", arguments={"target": "x"})],
        concurrent=False,
        external_lookup_counts={},
        workspace_violation_counts={},
        hook=hook,
        context=AgentHookContext(iteration=0, messages=[]),
    )
    assert tool.calls == [] and hook.calls == []
    assert POLICY_MARKER in results[0]
    assert [e["verdict"] for e in sink.decisions()] == ["deny"]


# -- classification ------------------------------------------------------------


async def test_policy_denial_escalates_on_third_hit(tmp_path):
    tools = ToolRegistry()
    tools.register(CapTool())
    calls = [
        ToolCallRequest(id=f"c{i}", name="fetch", arguments={"target": f"h{i}.example"})
        for i in range(3)
    ]
    result = await _run(tools, *calls, policy=SpyPolicy(deny={"net.fetch"}),
                        env=_env(tmp_path))
    details = [e["detail"] for e in result.tool_events]
    assert details[0].startswith("policy_denial: ")
    assert details[1].startswith("policy_denial: ")
    assert details[2].startswith("policy_denial_escalated: ")
    last = _tool_messages(result)[2]
    assert "not configurable by the agent" in last and "stop retrying" in last.lower()


def test_classify_policy_signature_only_for_policy_denials():
    runner = AgentRunner()
    call = ToolCallRequest(id="c", name="fetch", arguments={})
    counts: dict[str, int] = {}
    text = "Error: net.fetch blocked by permission policy for agent 'agent' (x)."
    handled = runner._classify_violation(
        raw_text=text, soft_payload=text, event={}, tool_call=call,
        workspace_violation_counts=counts, policy_capability="net.fetch",
    )
    assert handled is not None
    assert counts == {"violation:policy:net.fetch": 1}
    # Without the gate's capability the text alone is not a policy denial.
    assert runner._classify_violation(
        raw_text=text, soft_payload=text, event={}, tool_call=call,
        workspace_violation_counts={},
    ) is None


async def test_floor_denials_keep_their_classification(tmp_path):
    """A floor deny from the gate is exec-floor / workspace, never ``violation:policy:*``."""
    env = _env(tmp_path)
    exec_tool = CapTool(name="exec", capability="exec.run", key="command")
    fs_tool = WorkspaceCapTool(env.paths.work_dir)
    tools = ToolRegistry()
    tools.register(exec_tool)
    tools.register(fs_tool)
    result = await _run(
        tools,
        ToolCallRequest(id="c1", name="exec", arguments={"command": _FORK_BOMB}),
        ToolCallRequest(id="c2", name="read_thing",
                        arguments={"path": str(env.paths.state_dir / "s")}),
        policy=SpyPolicy(deny={"net.fetch"}),
        env=env,
    )
    exec_detail, fs_detail = (e["detail"] for e in result.tool_events)
    assert exec_detail.startswith("exec_guard_denial: ")
    assert fs_detail.startswith("workspace_violation: ")
    assert not any("policy" in e["detail"] for e in result.tool_events)

    runner = AgentRunner()
    counts: dict[str, int] = {}
    floor = gate_call(exec_tool, {"command": _FORK_BOMB}, AGENT_PRINCIPAL, DefaultPolicy(), env)
    runner._classify_violation(
        raw_text=floor.error_text(), soft_payload="", event={},
        tool_call=ToolCallRequest(id="c", name="exec", arguments={}),
        workspace_violation_counts=counts,
    )
    assert counts == {"violation:exec-floor": 1}


# -- AgentLoop wiring --------------------------------------------------------------


def _loop(tmp_path, **extra):
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.schema import Config

    config = Config.model_validate({
        "providers": {"openai": {"apiKey": "sk-gate-test"}},
        "agents": {"defaults": {"workspace": str(tmp_path / "ws"), "model": "openai/gpt-4o"}},
    })
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop.from_config(config, tool_registry=ToolRegistry(), provider=provider, **extra)


def test_loop_default_policy_and_principal(tmp_path):
    loop = _loop(tmp_path)
    assert type(loop.policy) is DefaultPolicy
    assert loop.principal == Principal(name="agent", kind="agent")
    assert loop.tools.gate_policy is loop.policy


def test_loop_accepts_custom_policy(tmp_path):
    policy = SpyPolicy(deny={"net.fetch"})
    loop = _loop(tmp_path, policy=policy)
    assert loop.policy is policy
    assert loop.tools.gate_policy is policy


# -- final review I1: policy and grants see the resolved path and the subtree -----
#
# A deny rule or plugin grant matched only the path the call NAMED. A symlink to a
# denied file, or a directory-rooted tool (grep, find_files, list_dir) rooted at an
# ancestor of a denied tree, slipped past it. Rules now check the lexical AND the
# resolved path (a deny on either denies; a grant must cover both), and a request
# whose path is a directory is denied when a rule could match anything under it.


def _denied_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``secret/`` (denied by rule) with a file, and a workspace with its own file."""
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "hostname").write_text("TOP-SECRET-HOST\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.txt").write_text("plain workspace file\n")
    return secret, ws, secret / "hostname"


def _fs_registry(ws: Path, policy: DefaultPolicy) -> ToolRegistry:
    from nanobot.agent.tools.filesystem import ListDirTool, ReadFileTool
    from nanobot.agent.tools.search import FindFilesTool, GrepTool

    registry = ToolRegistry()
    for tool in (
        ReadFileTool(workspace=ws), ListDirTool(workspace=ws),
        GrepTool(workspace=ws), FindFilesTool(workspace=ws),
    ):
        registry.register(tool)
    registry.configure_gate(policy=policy, workspace=ws)
    return registry


async def test_symlink_to_denied_file_is_denied_by_policy(tmp_path):
    secret, ws, target = _denied_tree(tmp_path)
    (ws / "link").symlink_to(target)
    registry = _fs_registry(ws, DefaultPolicy(deny_rules=(("fs.read", f"{secret}/*"),)))

    direct = await registry.execute("read_file", {"path": str(target)})
    assert POLICY_MARKER in str(direct)
    via_link = await registry.execute("read_file", {"path": str(ws / "link")})
    assert POLICY_MARKER in str(via_link)
    assert "TOP-SECRET-HOST" not in str(via_link)
    relative = await registry.execute("read_file", {"path": "link"})
    assert POLICY_MARKER in str(relative)


@pytest.mark.parametrize("tool_name", ["grep", "find_files", "list_dir"])
async def test_directory_tool_rooted_at_ancestor_of_denied_tree_is_denied(tmp_path, tool_name):
    secret, ws, _ = _denied_tree(tmp_path)
    registry = _fs_registry(ws, DefaultPolicy(deny_rules=(("fs.read", f"{secret}/*"),)))
    params = {"pattern": "SECRET"} if tool_name == "grep" else {}
    for root in (secret, tmp_path, Path("/")):
        out = await registry.execute(tool_name, {**params, "path": str(root)})
        assert POLICY_MARKER in str(out), (tool_name, root, out)
        assert "TOP-SECRET-HOST" not in str(out)


async def test_symlinked_directory_into_denied_tree_is_denied(tmp_path):
    secret, ws, _ = _denied_tree(tmp_path)
    (ws / "dirlink").symlink_to(secret, target_is_directory=True)
    registry = _fs_registry(ws, DefaultPolicy(deny_rules=(("fs.read", f"{secret}/*"),)))
    out = await registry.execute("grep", {"pattern": "SECRET", "path": "dirlink"})
    assert POLICY_MARKER in str(out)
    assert "TOP-SECRET-HOST" not in str(out)


async def test_unrelated_paths_stay_allowed_under_a_deny_rule(tmp_path):
    """No over-broad denial: symlink-free, non-ancestor calls work as before."""
    secret, ws, _ = _denied_tree(tmp_path)
    (ws / "inner").symlink_to(ws / "ok.txt")  # a symlink that stays outside the rule
    registry = _fs_registry(ws, DefaultPolicy(deny_rules=(("fs.read", f"{secret}/*"),)))

    assert "plain workspace file" in str(await registry.execute("read_file", {"path": "ok.txt"}))
    assert "plain workspace file" in str(await registry.execute("read_file", {"path": "inner"}))
    grep = await registry.execute("grep", {"pattern": "plain", "path": str(ws)})
    assert POLICY_MARKER not in str(grep) and "ok.txt" in str(grep)
    listing = await registry.execute("list_dir", {"path": str(ws)})
    assert POLICY_MARKER not in str(listing) and "ok.txt" in str(listing)
    # A sibling directory whose name shares the rule's prefix is not an ancestor.
    sibling = tmp_path / "secretive"
    sibling.mkdir()
    (sibling / "a.txt").write_text("sibling\n")
    out = await registry.execute("read_file", {"path": str(sibling / "a.txt")})
    assert "sibling" in str(out) and POLICY_MARKER not in str(out)


def test_plugin_grant_does_not_follow_a_symlink_out(tmp_path):
    secret, ws, target = _denied_tree(tmp_path)
    (ws / "link").symlink_to(target)
    tool = WorkspaceCapTool(ws)
    tool.capability_grant = (f"fs.read:{ws}/*",)
    assert gate_call(tool, {"path": "ok.txt"}, AGENT_PRINCIPAL, DefaultPolicy(), None).allowed
    escaped = gate_call(tool, {"path": "link"}, AGENT_PRINCIPAL, DefaultPolicy(), None)
    assert not escaped.allowed and escaped.layer == "policy"
    assert "capability grant" in escaped.error_text()


def test_plugin_grant_on_a_directory_must_cover_its_whole_subtree(tmp_path):
    _secret, ws, _ = _denied_tree(tmp_path)
    (ws / "src").mkdir()
    (ws / "src.py").mkdir()  # a directory whose NAME matches a file-only grant
    tool = WorkspaceCapTool(ws)
    tool.capability_grant = (f"fs.read:{ws}/*",)
    assert gate_call(tool, {"path": "src"}, AGENT_PRINCIPAL, DefaultPolicy(), None).allowed
    tool.capability_grant = (f"fs.read:{ws}/*.py",)
    denied = gate_call(tool, {"path": "src.py"}, AGENT_PRINCIPAL, DefaultPolicy(), None)
    assert not denied.allowed and "capability grant" in denied.error_text()
