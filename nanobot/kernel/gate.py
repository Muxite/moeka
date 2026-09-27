"""The capability gate (P2): every tool call passes here before it executes.

Order, per ``CapabilityRequest`` the tool declares (``Tool.capabilities``):
- fs resources are normalised first (``_normalize_fs_resource``, Ruling H): the raw
  param is stripped and resolved against the workspace the tool itself resolves
  against, so floors and policy see the path the tool will really touch.
- ``check_floors`` runs FIRST with the host's ``ProtectedFloor``; a floor ``Deny``
  short-circuits and the policy is never consulted.
- Strict mode (``nanobot.kernel.strict``): in a strict env an ``exec.run`` request from a
  tool without an active sandbox is denied next, at layer ``"gate"`` with
  ``STRICT_SANDBOX_MARKER``, before the policy and before the tool's own exec guard.
- Kernel plugin grant (Task 19): a tool carrying a ``capability_grant`` tuple (set by
  the kernel-mode loader on registry-active plugins: ``policy ∩ capabilities_requested``)
  has every declared request outside that grant denied next, at layer ``"policy"`` with
  ``POLICY_MARKER`` (it is the plugin's attenuated policy, so it counts toward I5). A
  grant rule ``cap`` covers every resource; ``cap:pattern`` covers resources matching
  ``pattern`` (``fnmatch``, after fs normalisation). Built-ins carry no grant. The gate
  is declaration-based: a plugin that under-declares in ``capabilities`` is not
  contained by this (in-process code needs the sandbox).
- ``PermissionPolicy.decide`` runs only when no floor (or strict refusal) fired.
- A ``policy.decision`` event goes to the trace sink for EVERY evaluated request
  (allow or deny), via ``safe_emit``: a failing sink never changes the verdict.
- The first ``Deny`` wins; later requests of the same call are not evaluated.
- Every ``Deny`` (any layer) appends one ``source="gate"`` entry to the deferred-action
  log (``nanobot.kernel.deferred``, design 5a) and ``error_text`` then ends with
  ``DEFERRED_NOTE``. The write is direct and never counts toward the I5 ceiling.

Call sites: ``AgentRunner._run_tool`` (between prepare_call and the first hook),
``ToolRegistry.execute`` and ``nanobot.agent.tools.execution``. A denied call never
reaches a hook and never executes.

Module-level imports are stdlib + kernel only; agent/security modules are imported
lazily (Ruling C).
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.kernel.deferred import deferred_work_dir, record_gate_denial, with_deferred_note
from nanobot.kernel.floors import FS_CAPABILITIES, check_floors
from nanobot.kernel.policy import (
    POLICY_MARKER,
    Allow,
    CapabilityRequest,
    DefaultPolicy,
    Deny,
    PermissionPolicy,
    Principal,
    policy_deny,
)
from nanobot.kernel.strict import strict_sandbox_deny
from nanobot.kernel.trace import LoguruTraceSink, TraceSink, safe_emit

if TYPE_CHECKING:
    from nanobot.kernel.env import CoreEnvironment
    from nanobot.security.protected_paths import ProtectedFloor

AGENT_PRINCIPAL = Principal(name="agent", kind="agent")

GateLayer = str  # "floor" | "policy" | "gate"


@dataclass(frozen=True)
class GateResult:
    """Outcome of ``gate_call``: ``Allow`` (audited) or the first ``Deny``.

    ``layer`` says who denied: ``"floor"`` (``check_floors``), ``"policy"``
    (``PermissionPolicy.decide``, or a kernel plugin's capability grant) or ``"gate"`` (the declaration itself failed, or strict
    mode refused ``exec.run`` without a sandbox).
    ``events`` are the ``policy.decision`` records emitted for this call.
    """

    decision: Allow | Deny
    layer: GateLayer | None = None
    events: tuple[dict[str, Any], ...] = field(default=(), repr=False)
    # True when this denial was appended to the deferred-action log (design 5a).
    deferred: bool = False

    @property
    def allowed(self) -> bool:
        return isinstance(self.decision, Allow)

    @property
    def deny(self) -> Deny | None:
        return self.decision if isinstance(self.decision, Deny) else None

    @property
    def policy_capability(self) -> str | None:
        """The denied capability, only when ``PermissionPolicy.decide`` denied.

        ``None`` for an allow, a floor deny and a ``"gate"`` deny (the tool's own
        declaration failed, or a strict-mode sandbox refusal: not the host's policy
        saying no). Only these
        policy denials are ``violation:policy:*`` and count toward the per-turn
        denial ceiling (I5).
        """
        deny = self.deny
        if deny is None or self.layer != "policy":
            return None
        return deny.capability

    def error_text(self) -> str:
        """Tool-facing error text (``""`` when allowed); always starts with ``Error``.

        When the denial was logged (``deferred``) the text ends with
        ``DEFERRED_NOTE``; the deny reason (and its pinned marker) comes first,
        unchanged.
        """
        deny = self.deny
        if deny is None:
            return ""
        reason = deny.reason.strip()
        text = reason if reason.startswith("Error") else f"Error: {reason}"
        return with_deferred_note(text) if self.deferred else text


def _normalize_fs_resource(resource: str, workspace: Path | None) -> str:
    """Resolve an ``fs.*`` resource the way the file tools do before touching it.

    - Surrounding whitespace is stripped (``apply_patch`` does this in execute; for
      other tools it only makes the gate stricter, never looser).
    - ``~`` is expanded; a relative path is joined to *workspace* (the tool's project
      path), never to the process cwd.
    - The result is absolute and lexically normalised (``os.path.abspath``); symlinks
      are NOT resolved here: ``ProtectedFloor.matches`` checks the path both as given
      and after ``resolve()``.
    """
    text = resource.strip()
    if not text:
        return ""
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute() and workspace is not None:
            candidate = Path(workspace).expanduser() / candidate
        return os.path.abspath(candidate)
    except (OSError, RuntimeError, ValueError):
        return text


def _tool_workspace(tool: Any) -> Path | None:
    """The workspace a tool resolves relative paths against, when it has one."""
    for obj in (tool, getattr(tool, "_wrapped", None)):
        if obj is None:
            continue
        for attr in ("_workspace", "workspace"):
            value = getattr(obj, attr, None)
            if isinstance(value, (str, Path)) and str(value):
                return Path(value)
    return None


def _call_workspace(
    tool: Any, env: CoreEnvironment | None, workspace: Path | None,
) -> Path | None:
    """Project path for this call: the bound scope, else the tool's own, else the host's."""
    from nanobot.security.workspace_access import current_tool_workspace

    default = _tool_workspace(tool) or workspace
    if default is None and env is not None:
        default = env.paths.work_dir
    return current_tool_workspace(default).project_path


def protected_floor(env: CoreEnvironment | None, workspace: Path | None) -> ProtectedFloor:
    """The same ``ProtectedFloor`` the file tools build for this host.

    With an env: ``ProtectedFloor.from_paths(env.paths, ...)`` plus, for a
    non-strict (legacy) env, the ambient data dirs and config files. Without one:
    the legacy default roots for *workspace*.
    """
    from nanobot.security.protected_paths import (
        ProtectedFloor,
        default_config_files,
        default_data_dirs,
    )

    if env is None:
        return ProtectedFloor(
            data_dir=default_data_dirs(), workspace=workspace, config_files=default_config_files(),
        )
    extra_dirs: list[Path] = []
    config_files: list[Path] = []
    if not env.strict:
        from nanobot.kernel.legacy import legacy_floor_extras

        extra_dirs, config_files = legacy_floor_extras()
    return ProtectedFloor.from_paths(
        env.paths, extra_data_dirs=extra_dirs, config_files=config_files,
    )


def _sink(env: CoreEnvironment | None) -> TraceSink:
    return env.trace if env is not None else LoguruTraceSink()


def _tool_name(tool: Any) -> str:
    try:
        return str(tool.name)
    except Exception:  # noqa: BLE001 - a broken name property must not break the gate
        return type(tool).__name__


def _decision_event(
    principal: Principal,
    tool_name: str,
    req: CapabilityRequest,
    decision: Allow | Deny,
    layer: GateLayer | None,
) -> dict[str, Any]:
    deny = decision if isinstance(decision, Deny) else None
    return {
        "event": "policy.decision",
        "actor": principal.name,
        "principal_kind": principal.kind,
        "tool": tool_name,
        "capability": req.capability,
        "resource": req.resource,
        "verdict": "deny" if deny is not None else "allow",
        "layer": layer if deny is not None else None,
        "marker": deny.marker if deny is not None else None,
    }


def plugin_grant_deny(tool: Any, principal: Principal, req: CapabilityRequest) -> Deny | None:
    """Deny *req* when *tool* carries a kernel plugin grant that does not cover it."""
    try:
        grant = getattr(tool, "capability_grant", None)
    except Exception:  # noqa: BLE001 - a broken grant property fails closed
        grant = ()
    if not isinstance(grant, tuple):
        return None  # built-ins and legacy tools carry no grant
    for rule in grant:
        cap, sep, pattern = str(rule).partition(":")
        if cap == req.capability and (not sep or fnmatch.fnmatchcase(req.resource, pattern)):
            return None
    return policy_deny(
        principal, req,
        "outside the plugin's capability grant, policy ∩ capabilities_requested",
    )


def gate_call(
    tool: Any,
    params: Any,
    principal: Principal | None,
    policy: PermissionPolicy | None,
    env: CoreEnvironment | None,
    ctx: Any = None,
    *,
    workspace: Path | None = None,
) -> GateResult:
    """Run floors then policy for every capability *tool* declares for *params*.

    ``principal``/``policy`` default to the top-level agent and ``DefaultPolicy()``.
    ``env`` supplies the trace sink and the protected floor; ``None`` means the legacy
    defaults (loguru sink, ambient floor roots). *workspace* is the fallback project
    path for relative fs resources when the tool has none of its own.
    """
    principal = principal or AGENT_PRINCIPAL
    policy = policy if policy is not None else DefaultPolicy()
    sink = _sink(env)
    name = _tool_name(tool)
    declare = getattr(tool, "capabilities", None)
    try:
        # A duck-typed tool without ``capabilities`` declares nothing (the ``Tool``
        # default); one whose declaration raises is denied (fail closed).
        requests = (
            list(declare(params if isinstance(params, dict) else {}))
            if declare is not None else []
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on a broken declaration
        logger.warning("tool {} capabilities() raised {!r}; denying the call", name, exc)
        req = CapabilityRequest("tool.capabilities", name)
        deny = Deny(
            reason=(
                f"Error: {name} call {POLICY_MARKER} for {principal.kind} "
                f"{principal.name!r} (its capability declaration failed)."
            ),
            marker=POLICY_MARKER,
            capability=req.capability,
        )
        event = _decision_event(principal, name, req, deny, "gate")
        safe_emit(sink, event)
        return _denied(name, params, deny, "gate", (event,), env, workspace)

    events: list[dict[str, Any]] = []
    floor: ProtectedFloor | None = None
    call_ws: Path | None = None
    ws_known = False
    for raw in requests:
        req = raw
        if req.capability in FS_CAPABILITIES:
            if not ws_known:
                call_ws = _call_workspace(tool, env, workspace)
                ws_known = True
            req = CapabilityRequest(req.capability, _normalize_fs_resource(req.resource, call_ws))
            if floor is None:
                floor = protected_floor(env, call_ws)
        decision: Allow | Deny
        layer: GateLayer | None
        floor_deny = check_floors(principal, req, protected=floor)
        strict_deny = strict_sandbox_deny(env, tool, name, req) if floor_deny is None else None
        grant_deny = (
            plugin_grant_deny(tool, principal, req)
            if floor_deny is None and strict_deny is None else None
        )
        if floor_deny is not None:
            decision, layer = floor_deny, "floor"
        elif strict_deny is not None:
            decision, layer = strict_deny, "gate"
        elif grant_deny is not None:
            decision, layer = grant_deny, "policy"
        else:
            layer = "policy"
            try:
                decision = policy.decide(principal, req, ctx)
            except Exception as exc:  # noqa: BLE001 - a broken policy fails closed
                logger.warning("policy {} raised {!r}; denying", type(policy).__name__, exc)
                decision = policy_deny(principal, req, "policy error")
        event = _decision_event(principal, name, req, decision, layer)
        events.append(event)
        safe_emit(sink, event)
        if isinstance(decision, Deny):
            return _denied(name, params, decision, layer, tuple(events), env, workspace)
    return GateResult(Allow(), None, tuple(events))


def _denied(
    name: str,
    params: Any,
    deny: Deny,
    layer: GateLayer | None,
    events: tuple[dict[str, Any], ...],
    env: CoreEnvironment | None,
    workspace: Path | None,
) -> GateResult:
    """Every denial (floor, policy, gate) appends one deferred entry (design 5a).

    A direct write, not a gated call: it is never classified as a denial and never
    counts toward the I5 ceiling. Without an env or explicit workspace there is
    nowhere to write, and ``deferred`` stays False so the text does not claim it.
    """
    logged = record_gate_denial(
        deferred_work_dir(env, workspace),
        tool=name,
        arguments=params if isinstance(params, dict) else {"_raw": params},
        reason=deny.reason,
        capability=deny.capability,
    )
    return GateResult(deny, layer, events, deferred=logged)


def emit_tool_invalid(
    env: CoreEnvironment | None,
    tool_name: str,
    principal: Principal | None,
    error: str,
) -> None:
    """Record a call that failed preparation (unknown tool, bad params)."""
    principal = principal or AGENT_PRINCIPAL
    safe_emit(_sink(env), {
        "event": "tool.invalid",
        "actor": principal.name,
        "principal_kind": principal.kind,
        "tool": tool_name,
        "error": str(error)[:500],
    })


__all__ = [
    "AGENT_PRINCIPAL",
    "GateResult",
    "emit_tool_invalid",
    "gate_call",
    "plugin_grant_deny",
    "protected_floor",
]
