"""Strict mode (P2, design section 4): fail closed when there is no real boundary.

Both behaviours apply only when ``CoreEnvironment.strict`` is True:
- Exec needs a declared sandbox. A call that needs ``exec.run`` (``exec``, including the
  ``exec_session`` start path, ``exec`` with ``yield_time_ms``) is refused unless the
  tool reports ``sandbox_active is True``. The gate refuses it after the floors and
  before the policy (layer ``"gate"``); ``ExecTool.execute`` repeats the check first
  thing, before ``_prepare_command`` and its guard, for callers that bypass the gate.
  A tool without the attribute is treated as unsandboxed (fail closed).
- Fully denied tools are dropped. With an explicit policy, ``ToolRegistry`` does not
  register (and ``configure_gate`` prunes) a tool whose every declared capability the
  policy denies for every resource (``policy_denies_everywhere``). An empty or unknown
  surface is kept: the gate still decides per call.

Module-level imports are stdlib + kernel only (Ruling C).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from nanobot.kernel.policy import CapabilityRequest, Deny, policy_denies_everywhere

if TYPE_CHECKING:
    from nanobot.kernel.env import CoreEnvironment

STRICT_SANDBOX_MARKER = "strict mode requires a declared sandbox"

# Capabilities that start a host process: refused in strict mode without a sandbox.
SANDBOX_REQUIRED_CAPABILITIES = frozenset({"exec.run"})


def is_strict(env: CoreEnvironment | None) -> bool:
    return env is not None and env.strict is True


def tool_sandboxed(tool: Any) -> bool:
    """True only when *tool* reports an active sandbox backend (``sandbox_active``)."""
    try:
        return getattr(tool, "sandbox_active", False) is True
    except Exception:  # noqa: BLE001 - a broken property means no declared sandbox
        return False


def strict_sandbox_message(tool_name: str) -> str:
    """The tool-facing refusal text; always contains ``STRICT_SANDBOX_MARKER``."""
    return (
        f"Error: {tool_name} refused: {STRICT_SANDBOX_MARKER} and none is configured "
        "(set tools.exec.sandbox to 'bwrap' or 'seatbelt' on a platform that supports it). "
        "The host runs the kernel in strict mode, which fails closed rather than run "
        "shell commands without an isolation boundary. This is host configuration, not "
        "a transient failure: do not retry with another command or tool. Tell the user "
        "a sandbox backend must be configured for shell execution."
    )


def strict_sandbox_deny(
    env: CoreEnvironment | None, tool: Any, tool_name: str, req: CapabilityRequest,
) -> Deny | None:
    """The strict-mode refusal for one gate request, or ``None`` when it does not apply."""
    if req.capability not in SANDBOX_REQUIRED_CAPABILITIES or not is_strict(env):
        return None
    if tool_sandboxed(tool):
        return None
    return Deny(
        reason=strict_sandbox_message(tool_name),
        marker=STRICT_SANDBOX_MARKER,
        capability=req.capability,
    )


def fully_denied(policy: Any, surface: Iterable[str] | None) -> bool:
    """True when *surface* is non-empty and *policy* denies each name everywhere."""
    if surface is None:
        return False
    names = frozenset(surface)
    return bool(names) and all(policy_denies_everywhere(policy, c) for c in names)


__all__ = [
    "SANDBOX_REQUIRED_CAPABILITIES",
    "STRICT_SANDBOX_MARKER",
    "fully_denied",
    "is_strict",
    "strict_sandbox_deny",
    "strict_sandbox_message",
    "tool_sandboxed",
]
