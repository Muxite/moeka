"""P2 policy vocabulary: PermissionPolicy, Principal, capability floors (Task 7)."""

from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest

from nanobot.kernel.floors import PLUGIN_LOAD_MARKER, check_floors
from nanobot.kernel.policy import (
    POLICY_MARKER,
    Allow,
    CapabilityRequest,
    DefaultPolicy,
    Deny,
    IntersectionPolicy,
    PermissionPolicy,
    Principal,
)

HOST = Principal("host", "host")
AGENT = Principal("main", "agent", parent=HOST)
SUB = Principal("sub-1", "subagent", parent=AGENT)
PLUGIN = Principal("some-plugin", "plugin", parent=HOST)

# The capability table in .agent/moeka-kernel-design.md section 4.
UNIVERSE = (
    "exec.run", "exec.session_input", "fs.read", "fs.write", "net.fetch", "secret.read",
    "mcp.call", "plugin.load", "session.read", "session.send", "model.dispatch",
    "budget.iterations", "budget.tokens",
)

FORK_BOMB = ":(){ :|:& };:"
HISTORY_WRITE = "echo x >> memory/history.jsonl"


def _allowed(policy: PermissionPolicy, principal: Principal = HOST) -> frozenset[str]:
    return frozenset(
        c for c in UNIVERSE
        if isinstance(policy.decide(principal, CapabilityRequest(c, "r"), None), Allow)
    )


# --- default policy: parity with today --------------------------------------------------


@pytest.mark.parametrize("cap", ["fs.read", "fs.write", "net.fetch", "exec.run", "mcp.call"])
def test_default_policy_allows_what_today_allows(cap: str) -> None:
    decision = DefaultPolicy().decide(AGENT, CapabilityRequest(cap, "anything"), None)
    assert isinstance(decision, Allow)


def test_default_policy_is_a_permission_policy() -> None:
    assert isinstance(DefaultPolicy(), PermissionPolicy)


def test_deny_capability_for_all_resources() -> None:
    policy = DefaultPolicy(deny_capabilities=frozenset({"net.fetch"}))
    decision = policy.decide(AGENT, CapabilityRequest("net.fetch", "https://x"), None)
    assert isinstance(decision, Deny)
    assert POLICY_MARKER in decision.reason
    assert decision.marker == POLICY_MARKER
    assert decision.capability == "net.fetch"
    assert policy.denies_everywhere("net.fetch")
    assert not policy.denies_everywhere("fs.read")
    assert isinstance(policy.decide(AGENT, CapabilityRequest("fs.read", "/x"), None), Allow)


def test_per_resource_deny_rule() -> None:
    policy = DefaultPolicy(deny_rules=(("fs.write", "/etc/*"),))
    assert isinstance(policy.decide(AGENT, CapabilityRequest("fs.write", "/etc/passwd"), None), Deny)
    assert isinstance(policy.decide(AGENT, CapabilityRequest("fs.write", "/tmp/x"), None), Allow)
    assert isinstance(policy.decide(AGENT, CapabilityRequest("fs.read", "/etc/passwd"), None), Allow)
    assert not policy.denies_everywhere("fs.write")


def test_default_policy_defends_plugin_load_in_depth() -> None:
    policy = DefaultPolicy()
    assert isinstance(policy.decide(HOST, CapabilityRequest("plugin.load", "p"), None), Allow)
    decision = policy.decide(PLUGIN, CapabilityRequest("plugin.load", "p"), None)
    assert isinstance(decision, Deny)
    assert POLICY_MARKER in decision.reason


# --- floors ------------------------------------------------------------------------------


def _exec_tool_guard(command: str) -> str | None:
    from nanobot.agent.tools.shell import ExecTool

    return ExecTool()._guard_command(command, str(Path.cwd()))


@pytest.mark.parametrize("command", [FORK_BOMB, HISTORY_WRITE, "cp a.txt memory/.dream_cursor"])
def test_exec_floor_parity_with_exec_tool(command: str) -> None:
    tool_error = _exec_tool_guard(command)
    assert tool_error is not None
    deny = check_floors(AGENT, CapabilityRequest("exec.run", command))
    assert isinstance(deny, Deny)
    assert deny.capability == "exec.run"
    # Same text the tool already emits, so the runner's classifiers keep working.
    assert deny.reason == str(tool_error)
    assert deny.marker in str(tool_error)
    from nanobot.utils.runtime import exec_guard_violation_signature

    assert exec_guard_violation_signature(deny.reason) == "violation:exec-floor"
    assert exec_guard_violation_signature(deny.reason) == exec_guard_violation_signature(
        str(tool_error)
    )


def test_exec_floor_applies_to_session_input() -> None:
    deny = check_floors(AGENT, CapabilityRequest("exec.session_input", FORK_BOMB))
    assert isinstance(deny, Deny)
    assert deny.capability == "exec.session_input"


@pytest.mark.parametrize("command", ["rm -rf /tmp/x", "ls -la", "dd if=/dev/zero of=/tmp/f"])
def test_exec_floor_leaves_moeka_permissive_commands_alone(command: str) -> None:
    assert check_floors(AGENT, CapabilityRequest("exec.run", command)) is None


def test_fs_floor_parity_with_protected_floor(tmp_path: Path) -> None:
    from nanobot.security.protected_paths import PROTECTED_MARKER, ProtectedFloor

    data = tmp_path / "data"
    ws = tmp_path / "ws"
    floor = ProtectedFloor(data_dir=data, workspace=ws)
    target = data / "auth" / "token.json"
    expected = floor.reason(target, write=False)
    assert expected is not None

    deny = check_floors(AGENT, CapabilityRequest("fs.read", str(target)), protected=floor)
    assert isinstance(deny, Deny)
    assert deny.reason == expected
    assert deny.marker == PROTECTED_MARKER
    assert deny.capability == "fs.read"

    hist = ws / "memory" / "history.jsonl"
    assert check_floors(AGENT, CapabilityRequest("fs.read", str(hist)), protected=floor) is None
    wdeny = check_floors(AGENT, CapabilityRequest("fs.write", str(hist)), protected=floor)
    assert isinstance(wdeny, Deny)
    assert wdeny.reason == floor.reason(hist, write=True)


def test_fs_floor_default_roots_cover_proc_secrets() -> None:
    deny = check_floors(AGENT, CapabilityRequest("fs.read", "/proc/self/environ"))
    assert isinstance(deny, Deny)
    from nanobot.security.protected_paths import PROTECTED_MARKER

    assert PROTECTED_MARKER in deny.reason


def test_fs_floor_allows_ordinary_paths(tmp_path: Path) -> None:
    assert check_floors(AGENT, CapabilityRequest("fs.read", str(tmp_path / "a.txt"))) is None


@pytest.mark.parametrize("principal", [SUB, PLUGIN, AGENT])
def test_plugin_load_is_host_only(principal: Principal) -> None:
    deny = check_floors(principal, CapabilityRequest("plugin.load", "x"))
    assert isinstance(deny, Deny)
    assert deny.marker == PLUGIN_LOAD_MARKER
    assert PLUGIN_LOAD_MARKER in deny.reason


def test_plugin_load_allowed_for_host() -> None:
    assert check_floors(HOST, CapabilityRequest("plugin.load", "x")) is None


@pytest.mark.parametrize("principal", [HOST, AGENT, SUB, PLUGIN])
def test_floors_cannot_be_exempted_by_policy(principal: Principal) -> None:
    """Even a host principal and an allow-everything policy cannot undo a floor Deny.

    Documented order: check_floors first, then decide; a floor Deny short-circuits.
    check_floors takes no policy or config, so nothing can switch it off.
    """
    permissive = DefaultPolicy()
    req = CapabilityRequest("exec.run", FORK_BOMB)
    assert isinstance(permissive.decide(principal, req, None), Allow)
    first = check_floors(principal, req)
    second = check_floors(principal, req)
    assert isinstance(first, Deny)
    assert first == second


def test_non_floor_capabilities_pass_floors() -> None:
    for cap in ("net.fetch", "mcp.call", "session.send", "secret.read"):
        assert check_floors(SUB, CapabilityRequest(cap, "whatever")) is None


# --- attenuation (I4) --------------------------------------------------------------------


def test_attenuate_empty_set_allows_nothing() -> None:
    child = DefaultPolicy().attenuate(frozenset())
    assert _allowed(child) == frozenset()


def test_attenuate_full_set_is_parent() -> None:
    parent = DefaultPolicy()
    child = parent.attenuate(frozenset(UNIVERSE))
    assert _allowed(child) == _allowed(parent)


def test_attenuate_keeps_parent_deny_rules() -> None:
    parent = DefaultPolicy(deny_capabilities=frozenset({"net.fetch"}))
    child = parent.attenuate(frozenset({"net.fetch", "fs.read"}))
    assert _allowed(child) == frozenset({"fs.read"})


def test_attenuate_with_narrower_intersects() -> None:
    parent = DefaultPolicy()
    narrower = DefaultPolicy().attenuate(frozenset({"fs.read", "exec.run"}))
    child = parent.attenuate(frozenset({"fs.read", "net.fetch"}), narrower)
    assert _allowed(child) == frozenset({"fs.read"})


class _DenyExec:
    """A foreign PermissionPolicy (not DefaultPolicy) used as the narrower."""

    def decide(self, principal, req, ctx):
        if req.capability == "exec.run":
            return Deny(reason=f"no exec {POLICY_MARKER}", marker=POLICY_MARKER,
                        capability=req.capability)
        return Allow()

    def attenuate(self, requested, narrower=None):
        return self


def test_attenuate_with_foreign_narrower() -> None:
    child = DefaultPolicy().attenuate(frozenset({"exec.run", "fs.read"}), _DenyExec())
    assert _allowed(child) == frozenset({"fs.read"})


RESOURCES = ("r", "/etc/passwd", "/tmp/x", "https://example.com", "")


def _allowed_pairs(policy: PermissionPolicy, principal: Principal) -> frozenset[tuple[str, str]]:
    return frozenset(
        (c, r) for c in UNIVERSE for r in RESOURCES
        if isinstance(policy.decide(principal, CapabilityRequest(c, r), None), Allow)
    )


class _NoCap(DefaultPolicy):
    """A DefaultPolicy subclass whose overridden decide denies one extra capability."""

    def __init__(self, banned: str, **kw) -> None:
        super().__init__(**kw)
        self.banned = banned

    def decide(self, principal, req, ctx):
        if req.capability == self.banned:
            return Deny(reason=f"banned {POLICY_MARKER}", marker=POLICY_MARKER,
                        capability=req.capability)
        return super().decide(principal, req, ctx)


class _Leaky:
    """A misbehaving foreign policy: denies one capability, attenuates to allow-all."""

    def __init__(self, banned: str) -> None:
        self.banned = banned

    def decide(self, principal, req, ctx):
        if req.capability == self.banned:
            return Deny(reason=f"leaky {POLICY_MARKER}", marker=POLICY_MARKER,
                        capability=req.capability)
        return Allow()

    def attenuate(self, requested, narrower=None):
        return DefaultPolicy()


def test_subclass_narrower_rules_survive_attenuate() -> None:
    child = DefaultPolicy().attenuate(frozenset({"exec.run", "fs.read"}), _NoCap("exec.run"))
    assert _allowed(child) == frozenset({"fs.read"})


def test_subclass_self_rules_survive_attenuate() -> None:
    parent = _NoCap("exec.run")
    child = parent.attenuate(frozenset({"exec.run", "fs.read"}))
    assert _allowed(child) == frozenset({"fs.read"})
    assert _allowed(child.attenuate(frozenset(UNIVERSE))) <= frozenset({"fs.read"})


def test_intersection_keeps_misbehaving_member_as_floor() -> None:
    parent = DefaultPolicy().attenuate(frozenset(UNIVERSE), _Leaky("net.fetch"))
    assert "net.fetch" not in _allowed(parent)
    grand = parent.attenuate(frozenset({"net.fetch", "fs.read"}))
    assert _allowed(grand) == frozenset({"fs.read"})
    great = grand.attenuate(frozenset(UNIVERSE))
    assert _allowed(great) <= frozenset({"fs.read"})


def _random_policy(rng: random.Random, depth: int = 0) -> PermissionPolicy:
    denied = frozenset(rng.sample(UNIVERSE, rng.randint(0, 3)))
    rules = tuple(
        (rng.choice(UNIVERSE), rng.choice(("/etc/*", "https://*", "*", "/tmp/?")))
        for _ in range(rng.randint(0, 3))
    )
    roll = rng.random()
    if roll < 0.4:
        policy: PermissionPolicy = DefaultPolicy(deny_capabilities=denied, deny_rules=rules)
    elif roll < 0.6:
        policy = _NoCap(rng.choice(UNIVERSE), deny_capabilities=denied, deny_rules=rules)
    elif roll < 0.75:
        policy = _Leaky(rng.choice(UNIVERSE))
    elif roll < 0.9 and depth < 2:
        policy = IntersectionPolicy(_random_policy(rng, depth + 1), _random_policy(rng, depth + 1))
    else:
        policy = _DenyExec()
    if rng.random() < 0.5:
        policy = policy.attenuate(frozenset(rng.sample(UNIVERSE, rng.randint(0, len(UNIVERSE)))))
    return policy


def test_attenuate_subset_property() -> None:
    """Every child is a subset of its parent, its request and its narrower (I4).

    Parents and narrowers are DefaultPolicy (with deny rules), DefaultPolicy subclasses
    overriding decide, IntersectionPolicy, and foreign policies (including one whose
    attenuate misbehaves); decisions are compared over several resources.
    """
    rng = random.Random(7)
    for _ in range(300):
        parent = _random_policy(rng)
        if not isinstance(parent, (DefaultPolicy, IntersectionPolicy)):
            # A bare foreign root's own attenuate is outside the kernel's control;
            # the kernel only ever holds one as an IntersectionPolicy member.
            parent = IntersectionPolicy(parent)
        requested = frozenset(rng.sample(UNIVERSE, rng.randint(0, len(UNIVERSE))))
        narrower = _random_policy(rng) if rng.random() < 0.5 else None
        child = parent.attenuate(requested, narrower)
        grand_req = frozenset(rng.sample(UNIVERSE, rng.randint(0, len(UNIVERSE))))
        grand = child.attenuate(grand_req)
        for principal in (HOST, AGENT, SUB):
            got = _allowed_pairs(child, principal)
            assert got <= _allowed_pairs(parent, principal)
            assert {c for c, _ in got} <= requested
            if narrower is not None:
                assert got <= _allowed_pairs(narrower, principal)
            g = _allowed_pairs(grand, principal)
            assert g <= got
            assert {c for c, _ in g} <= grand_req


def test_attenuated_deny_carries_policy_marker() -> None:
    child = DefaultPolicy().attenuate(frozenset({"fs.read"}))
    decision = child.decide(SUB, CapabilityRequest("net.fetch", "https://x"), None)
    assert isinstance(decision, Deny)
    assert decision.marker == POLICY_MARKER
    assert POLICY_MARKER in decision.reason


# --- Ruling C: lazy agent imports ------------------------------------------------------


@pytest.mark.parametrize("module", ["nanobot/kernel/policy.py", "nanobot/kernel/floors.py"])
def test_kernel_policy_modules_import_agent_lazily(module: str) -> None:
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / module).read_text())
    allowed = ("nanobot.config.paths", "nanobot.llm_usage", "nanobot.kernel.policy")
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            if name.startswith("nanobot"):
                assert name in allowed, f"{module}: module-level import of {name}"
