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
OFFLINE_MARKER = "blocked in offline mode"

# Every capability the codebase uses for network access: ``net.fetch`` (web_search,
# web_fetch, image_generation, remote MCP tools) and ``mcp.call`` (any MCP server,
# local stdio ones included: offline connects none). Any other ``net.*`` name is
# treated as network too (``is_network_capability``).
NETWORK_CAPABILITIES: frozenset[str] = frozenset({"net.fetch", "mcp.call"})

PrincipalKind = Literal["agent", "subagent", "plugin", "host"]


@dataclass(frozen=True)
class CapabilityRequest:
    """One capability a tool call needs, e.g. ``("fs.write", "/srv/app/x.conf")``.

    ``subtree`` is set by the gate on an ``fs.*`` request whose path is an existing
    directory: a directory-rooted tool (grep, find_files, list_dir) may touch anything
    beneath it, so resource rules must consider the whole subtree
    (``glob_may_match_under`` / ``glob_covers_subtree``), not just the root's name.
    """

    capability: str
    resource: str = ""
    subtree: bool = False


_GLOB_CHARS = "*?["


def _glob_literal_prefix(glob: str) -> tuple[str, bool]:
    """The part of *glob* before its first wildcard, and whether it has one."""
    cut = min((i for i in (glob.find(c) for c in _GLOB_CHARS) if i >= 0), default=-1)
    return (glob, False) if cut < 0 else (glob[:cut], True)


def _dir_prefix(directory: str) -> str:
    return directory.rstrip("/") + "/"


def glob_may_match_under(directory: str, glob: str) -> bool:
    """True when some path strictly under *directory* could match *glob* (``fnmatch``).

    Conservative (fail closed): with a wildcard, ``fnmatch``'s ``*`` also crosses
    ``/``, so any path under *directory* may match once the glob's literal prefix and
    ``directory/`` agree (one is a prefix of the other). ``/`` and ``/etc`` are both
    "under" ``/etc/*`` in this sense; ``/home/ws`` is not.
    """
    prefix, wild = _glob_literal_prefix(glob)
    under = _dir_prefix(directory)
    if not wild:
        return prefix.startswith(under)
    return prefix.startswith(under) or under.startswith(prefix)


def glob_covers_subtree(directory: str, glob: str) -> bool:
    """True when EVERY path under *directory* (and *directory* itself) matches *glob*.

    ``directory/`` matching a glob that ends in ``*`` means that final ``*`` absorbs
    any further suffix, so every descendant matches too.
    """
    return (
        glob.endswith("*")
        and fnmatch.fnmatchcase(directory, glob)
        and fnmatch.fnmatchcase(_dir_prefix(directory), glob)
    )


def resource_matches(req: CapabilityRequest, glob: str) -> bool:
    """Deny-rule match: the resource, or (for a directory request) anything under it."""
    if fnmatch.fnmatchcase(req.resource, glob):
        return True
    return bool(req.subtree and req.resource) and glob_may_match_under(req.resource, glob)


def resource_covered(req: CapabilityRequest, glob: str) -> bool:
    """Allow/grant match: the resource, and (for a directory request) its whole subtree."""
    if req.subtree and req.resource:
        return glob_covers_subtree(req.resource, glob)
    return fnmatch.fnmatchcase(req.resource, glob)


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


def policy_denies_everywhere(policy: Any, capability: str) -> bool:
    """Ask *policy* whether it denies *capability* for every resource.

    - Uses the optional ``denies_everywhere`` method (``DefaultPolicy``,
      ``IntersectionPolicy``). It is not part of the ``PermissionPolicy`` protocol.
    - A policy without it, or whose answer raises or is not ``True``, is never assumed
      to deny everywhere: strict mode then keeps the tool and lets the gate decide per
      call (dropping a tool on a guess would hide a usable one).
    """
    ask = getattr(policy, "denies_everywhere", None)
    if not callable(ask):
        return False
    try:
        return ask(capability) is True
    except Exception:  # noqa: BLE001 - an unanswerable policy keeps the tool
        return False


class DefaultPolicy:
    """Permissive parity with today, plus optional deny rules and an allowed set.

    - ``deny_capabilities``: capabilities denied for every resource (the registry may
      drop such a tool entirely; see ``denies_everywhere``).
    - ``deny_rules``: ``(capability, resource_glob)`` pairs denied per resource
      (``fnmatch`` syntax). A directory request (``subtree``) is denied when the glob
      could match anything beneath it (``resource_matches``). The gate also calls
      ``decide`` with the symlink-resolved path, so a rule fires on either form.
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
        """True when *capability* is denied for every resource by this policy.

        - A ``deny_capabilities`` entry, or a capability outside ``allowed``.
        - A ``deny_rules`` entry whose glob is ``"*"`` (``fnmatch`` matches every
          resource, including the empty one). Narrower globs are per-resource only.
        """
        if capability in self.deny_capabilities:
            return True
        if any(cap == capability and glob == "*" for cap, glob in self.deny_rules):
            return True
        return self.allowed is not None and capability not in self.allowed

    def decide(self, principal: Principal, req: CapabilityRequest, ctx: Any) -> Allow | Deny:
        cap = req.capability
        # Defense in depth: check_floors already denies this (marker PLUGIN_LOAD_MARKER in
        # nanobot.kernel.floors) before decide runs; reaching here means the gate was
        # bypassed, so the generic POLICY_MARKER is used on purpose.
        if cap == "plugin.load" and principal.kind != "host":
            return policy_deny(principal, req, "plugin.load is host-only")
        if self.allowed is not None and cap not in self.allowed:
            return policy_deny(principal, req, "capability not granted")
        if cap in self.deny_capabilities:
            return policy_deny(principal, req, "capability denied for all resources")
        for rule_cap, glob in self.deny_rules:
            if rule_cap == cap and resource_matches(req, glob):
                return policy_deny(principal, req, f"resource matches deny rule {glob!r}")
        return Allow()

    def attenuate(
        self, requested: frozenset[str], narrower: PermissionPolicy | None = None,
    ) -> PermissionPolicy:
        """A child that allows only ``requested`` ∩ this policy (∩ ``narrower``).

        Fields are merged only for exact ``DefaultPolicy`` instances. A subclass (of
        ``self`` or ``narrower``) may add rules by overriding ``decide``, so it is kept
        as a member of an ``IntersectionPolicy`` and its own ``decide`` is still
        consulted: the child is never broader than either.
        """
        allowed = frozenset(requested)
        if self.allowed is not None:
            allowed &= self.allowed
        deny_caps = self.deny_capabilities
        rules = self.deny_rules
        if narrower is not None and type(narrower) is DefaultPolicy:
            if narrower.allowed is not None:
                allowed &= narrower.allowed
            deny_caps |= narrower.deny_capabilities
            rules = rules + narrower.deny_rules
            narrower = None
        child: PermissionPolicy = DefaultPolicy(
            deny_capabilities=deny_caps, deny_rules=rules, allowed=allowed,
        )
        extra: list[PermissionPolicy] = []
        if type(self) is not DefaultPolicy:
            extra.append(self)
        if narrower is not None:
            extra.append(narrower)
        return IntersectionPolicy(child, *extra) if extra else child


def is_network_capability(capability: str) -> bool:
    """True for a :data:`NETWORK_CAPABILITIES` name or any ``net.*`` capability."""
    return capability in NETWORK_CAPABILITIES or capability.startswith("net.")


class OfflinePolicy:
    """Denies every network capability (``is_network_capability``) for every resource.

    The deny carries :data:`OFFLINE_MARKER` (not ``POLICY_MARKER``). Intersect it with
    another policy (``IntersectionPolicy``); on its own it allows everything else.
    It covers what tools *declare*: ``exec`` is not network-sandboxed by it (a shell
    command can still open sockets); the container or sandbox is the real boundary.
    """

    def denies_everywhere(self, capability: str) -> bool:
        return is_network_capability(capability)

    def decide(self, principal: Principal, req: CapabilityRequest, ctx: Any) -> Allow | Deny:
        if not is_network_capability(req.capability):
            return Allow()
        target = f" on {req.resource!r}" if req.resource else ""
        return Deny(
            reason=(
                f"Error: {req.capability}{target} {OFFLINE_MARKER}: this agent has no "
                "network access. Do not retry with another network tool."
            ),
            marker=OFFLINE_MARKER,
            capability=req.capability,
        )

    def attenuate(
        self, requested: frozenset[str], narrower: PermissionPolicy | None = None,
    ) -> PermissionPolicy:
        """A child allowing only ``requested`` (∩ ``narrower``), still offline."""
        members: list[PermissionPolicy] = [self, DefaultPolicy(allowed=frozenset(requested))]
        if narrower is not None:
            members.append(narrower)
        return IntersectionPolicy(*members)

    def __eq__(self, other: object) -> bool:
        return type(other) is OfflinePolicy

    def __hash__(self) -> int:
        return hash(OfflinePolicy)

    def __repr__(self) -> str:
        return "OfflinePolicy()"


class IntersectionPolicy:
    """Allows a request only when every member policy allows it (first Deny wins)."""

    def __init__(self, *members: PermissionPolicy) -> None:
        flat: list[PermissionPolicy] = []
        for member in members:
            parts = member.members if type(member) is IntersectionPolicy else (member,)
            flat.extend(p for p in parts if not any(p is q for q in flat))
        self.members = tuple(flat)

    def denies_everywhere(self, capability: str) -> bool:
        """True when any member denies *capability* for every resource."""
        return any(policy_denies_everywhere(m, capability) for m in self.members)

    def decide(self, principal: Principal, req: CapabilityRequest, ctx: Any) -> Allow | Deny:
        for member in self.members:
            decision = member.decide(principal, req, ctx)
            if isinstance(decision, Deny):
                return decision
        return Allow()

    def attenuate(
        self, requested: frozenset[str], narrower: PermissionPolicy | None = None,
    ) -> PermissionPolicy:
        """Attenuate every member; members we do not control stay in as a floor.

        An exact ``DefaultPolicy`` member's ``attenuate`` is trusted. Any other member
        is kept alongside its attenuated version, and a ``DefaultPolicy(allowed=
        requested)`` bounds the result, so a misbehaving ``attenuate`` cannot let a
        grandchild escape its parent's rules or the requested set. A member with no
        ``attenuate`` (a decide-only policy) or whose ``attenuate`` raises is kept as
        that floor alone.
        """
        children: list[PermissionPolicy] = [DefaultPolicy(allowed=frozenset(requested))]
        for member in self.members:
            if type(member) is DefaultPolicy:
                children.append(member.attenuate(requested))
                continue
            attenuate = getattr(member, "attenuate", None)
            if callable(attenuate):
                try:
                    children.append(attenuate(requested))
                except Exception:  # noqa: BLE001 - the member itself stays as the floor
                    pass
            children.append(member)
        if narrower is not None:
            children.append(narrower)
        return IntersectionPolicy(*children)
