"""Capability policy vocabulary (P2): principals, requests, decisions, policies.

- A tool call becomes one or more ``CapabilityRequest``s (capability name plus resource).
- The gate (wired in Task 9) runs ``nanobot.kernel.floors.check_floors`` FIRST; a floor
  ``Deny`` short-circuits and ``PermissionPolicy.decide`` is never consulted.
- Only when no floor fires does ``decide`` run. No policy, rule or config can undo a floor.
- ``DefaultPolicy`` keeps today's permissive behaviour: it allows everything unless a
  deny rule was configured. ``plugin.load`` for a non-host principal is denied by the
  floor layer; ``DefaultPolicy`` also denies it as defense in depth.
- ``attenuate`` builds a child policy that allows only a subset of the parent (I4).

Module-level imports are stdlib only (Ruling C: agent modules are never imported here).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

POLICY_MARKER = "blocked by permission policy"

PrincipalKind = Literal["agent", "subagent", "plugin", "host"]


@dataclass(frozen=True)
class CapabilityRequest:
    """One capability a tool call needs, e.g. ``("fs.write", "/srv/app/x.conf")``."""

    capability: str
    resource: str = ""


@dataclass(frozen=True)
class Principal:
    """Who is asking: the host, the main agent, a sub-agent or a plugin."""

    name: str
    kind: PrincipalKind
    parent: Principal | None = None


@dataclass(frozen=True)
class Allow:
    """The request may proceed."""


@dataclass(frozen=True)
class Deny:
    """The request is refused.

    ``reason`` is the full tool-facing error text; ``marker`` is the substring the
    runner's classifiers key on (it always occurs in ``reason``).
    """

    reason: str
    marker: str
    capability: str


Decision = Allow | Deny


@runtime_checkable
class PermissionPolicy(Protocol):
    def decide(self, principal: Principal, req: CapabilityRequest, ctx: Any) -> Allow | Deny: ...

    def attenuate(
        self, requested: frozenset[str], narrower: PermissionPolicy | None = None,
    ) -> PermissionPolicy: ...


def policy_deny(principal: Principal, req: CapabilityRequest, why: str) -> Deny:
    """A ``Deny`` carrying ``POLICY_MARKER``."""
    target = f" on {req.resource!r}" if req.resource else ""
    return Deny(
        reason=(
            f"Error: {req.capability}{target} {POLICY_MARKER} for {principal.kind} "
            f"{principal.name!r} ({why})."
        ),
        marker=POLICY_MARKER,
        capability=req.capability,
    )


class DefaultPolicy:
    """Permissive parity with today, plus optional deny rules and an allowed set.

    - ``deny_capabilities``: capabilities denied for every resource (the registry may
      drop such a tool entirely; see ``denies_everywhere``).
    - ``deny_rules``: ``(capability, resource_glob)`` pairs denied per resource
      (``fnmatch`` syntax).
    - ``allowed``: ``None`` means every capability; otherwise only these (set by
      ``attenuate``).
    """

    def __init__(
        self,
        deny_capabilities: frozenset[str] = frozenset(),
        deny_rules: tuple[tuple[str, str], ...] = (),
        allowed: frozenset[str] | None = None,
    ) -> None:
        self.deny_capabilities = frozenset(deny_capabilities)
        self.deny_rules = tuple(deny_rules)
        self.allowed = None if allowed is None else frozenset(allowed)

    def denies_everywhere(self, capability: str) -> bool:
        """True when *capability* is denied for every resource by this policy."""
        if capability in self.deny_capabilities:
            return True
        return self.allowed is not None and capability not in self.allowed

    def decide(self, principal: Principal, req: CapabilityRequest, ctx: Any) -> Allow | Deny:
        cap = req.capability
        # Defense in depth: the floor already denies this before decide runs.
        if cap == "plugin.load" and principal.kind != "host":
            return policy_deny(principal, req, "plugin.load is host-only")
        if self.allowed is not None and cap not in self.allowed:
            return policy_deny(principal, req, "capability not granted")
        if cap in self.deny_capabilities:
            return policy_deny(principal, req, "capability denied for all resources")
        for rule_cap, glob in self.deny_rules:
            if rule_cap == cap and fnmatch.fnmatchcase(req.resource, glob):
                return policy_deny(principal, req, f"resource matches deny rule {glob!r}")
        return Allow()

    def attenuate(
        self, requested: frozenset[str], narrower: PermissionPolicy | None = None,
    ) -> PermissionPolicy:
        """A child that allows only ``requested`` ∩ this policy (∩ ``narrower``).

        Deny rules are inherited. With a foreign ``narrower`` the child also requires
        ``narrower.decide`` to allow, so it is never broader than either parent.
        """
        allowed = frozenset(requested)
        if self.allowed is not None:
            allowed &= self.allowed
        deny_caps = self.deny_capabilities
        rules = self.deny_rules
        if isinstance(narrower, DefaultPolicy):
            if narrower.allowed is not None:
                allowed &= narrower.allowed
            deny_caps |= narrower.deny_capabilities
            rules = rules + narrower.deny_rules
            narrower = None
        child = DefaultPolicy(deny_capabilities=deny_caps, deny_rules=rules, allowed=allowed)
        if narrower is None:
            return child
        return IntersectionPolicy(child, narrower)


class IntersectionPolicy:
    """Allows a request only when every member policy allows it (first Deny wins)."""

    def __init__(self, *members: PermissionPolicy) -> None:
        self.members = members

    def decide(self, principal: Principal, req: CapabilityRequest, ctx: Any) -> Allow | Deny:
        for member in self.members:
            decision = member.decide(principal, req, ctx)
            if isinstance(decision, Deny):
                return decision
        return Allow()

    def attenuate(
        self, requested: frozenset[str], narrower: PermissionPolicy | None = None,
    ) -> PermissionPolicy:
        children = [m.attenuate(requested) for m in self.members]
        if narrower is not None:
            children.append(narrower)
        return IntersectionPolicy(*children)
