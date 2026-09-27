"""Non-removable capability floors (P2).

Order (wired by the gate in Task 9):
- ``check_floors(principal, req)`` runs FIRST for every capability request.
- A floor ``Deny`` short-circuits: ``PermissionPolicy.decide`` is never consulted, so no
  policy, rule, flag or config can exempt a floor.
- Only a ``None`` result lets the request reach ``decide``.

Floors:
- ``exec.run`` / ``exec.session_input``: the fork bomb and internal-state writes, via
  ``ExecTool._FLOOR_DENY_PATTERNS``; the denial text is the tool's own, so the runner's
  ``_EXEC_GUARD_MARKERS`` / exec-floor classification keep recognising it.
- ``fs.read`` / ``fs.write``: the resolved-path floor (``ProtectedFloor``); the denial
  text is ``ProtectedFloor.reason`` and carries ``PROTECTED_MARKER``.
- ``plugin.load``: host-only; any other principal kind is denied.

``check_floors`` takes no policy and no config, and depends on no mode flag. Agent and
security modules are imported lazily inside functions (Ruling C).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from nanobot.kernel.policy import CapabilityRequest, Deny, Principal

if TYPE_CHECKING:
    from nanobot.security.protected_paths import ProtectedFloor

PLUGIN_LOAD_MARKER = "plugin.load is host-only"

EXEC_CAPABILITIES = frozenset({"exec.run", "exec.session_input"})
FS_CAPABILITIES = frozenset({"fs.read", "fs.write"})

# The substring ``_EXEC_GUARD_MARKERS`` keys on (nanobot/utils/runtime.py).
_EXEC_FLOOR_MARKER = "blocked by safety guard (dangerous pattern detected)"


def _exec_floor(req: CapabilityRequest) -> Deny | None:
    from nanobot.agent.tools.shell import ExecTool

    # Same normalisation as ExecTool._guard_command / session-input screening.
    cmd = req.resource.replace("\x00", "").strip()
    if not cmd:
        return None
    hit = ExecTool.floor_denial(cmd.lower())
    if hit is None:
        return None
    _pattern, message = hit
    return Deny(reason=message, marker=_EXEC_FLOOR_MARKER, capability=req.capability)


def _fs_floor(req: CapabilityRequest, protected: ProtectedFloor | None) -> Deny | None:
    from nanobot.security.protected_paths import (
        PROTECTED_MARKER,
        ProtectedFloor,
        default_config_files,
        default_data_dirs,
    )

    if not req.resource:
        return None
    floor = protected or ProtectedFloor(
        data_dir=default_data_dirs(), workspace=None, config_files=default_config_files(),
    )
    reason = floor.reason(Path(req.resource), write=req.capability == "fs.write")
    if reason is None:
        return None
    return Deny(reason=reason, marker=PROTECTED_MARKER, capability=req.capability)


def check_floors(
    principal: Principal,
    req: CapabilityRequest,
    *,
    protected: ProtectedFloor | None = None,
) -> Deny | None:
    """Return a floor ``Deny`` for *req*, or ``None`` when no floor applies.

    *protected* is the host's precomputed ``ProtectedFloor`` (``ProtectedFloor.from_paths``);
    without it the legacy default roots are used (same as ``check_protected``).
    """
    cap = req.capability
    if cap == "plugin.load" and principal.kind != "host":
        return Deny(
            reason=(
                f"Error: plugin.load blocked by capability floor — {PLUGIN_LOAD_MARKER} "
                f"({principal.kind} {principal.name!r} may not load plugins). "
                "This guard is not configurable."
            ),
            marker=PLUGIN_LOAD_MARKER,
            capability=cap,
        )
    if cap in EXEC_CAPABILITIES:
        return _exec_floor(req)
    if cap in FS_CAPABILITIES:
        return _fs_floor(req, protected)
    return None
