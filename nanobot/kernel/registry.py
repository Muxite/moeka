"""Host-owned kernel plugin registry and lifecycle (design section 9, Task 18).

Three "registry" concepts now exist; this module is only the third:

- :class:`nanobot.agent.tools.registry.ToolRegistry`: the agent's live tool table.
- :class:`nanobot.kernel.solvers.SolverRegistry`: deterministic router solvers (Task 15).
- :class:`PluginRegistry` (here): which kernel plugins (:class:`PluginManifest`) the
  HOST has activated, at which pinned ``version_hash``. It loads nothing; the loader
  consults it (Task 19).

Lifecycle (design section 9 diagram, plus ``candidate -> retired``):

```mermaid
stateDiagram-v2
    [*] --> candidate: register
    [*] --> quarantined: register, package hash mismatch
    candidate --> quarantined: review starts / hash mismatch
    quarantined --> active: activate, hashes match
    active --> quarantined: hash mismatch at load / host quarantines
    candidate --> retired: host rejects
    quarantined --> retired: host rejects
    active --> retired: host retires
```

Rules:
- Every state change except the load-time auto-quarantine needs a host principal.
  The check reuses the ``plugin.load`` capability floor (:func:`check_floors`): a
  lifecycle change decides what may load, and that floor is already host-only and
  not configurable. No new capability is added. A denial raises
  :class:`PluginLifecycleDeniedError` and emits a ``verdict: deny`` audit event.
- ``activate`` pins a hash only when three agree: the registered ``version_hash``, the
  caller's ``expected_hash`` and the hash recomputed from the package on disk (the
  manifest file re-read, :func:`compute_version_hash`). Any mismatch moves the entry
  to ``quarantined`` (persisted) and raises :class:`PluginHashMismatchError`; it never
  becomes ``active``. Activating a ``candidate`` passes through ``quarantined``
  (the diagram's "review starts"), so the audit trail shows both edges.
- ``check_active`` is the load-time check: an active entry whose current hash differs
  from the pin moves to ``quarantined`` and returns ``False``. It only ever moves
  toward safety, so it needs no principal; its actor is :data:`REGISTRY_PRINCIPAL`.
  Leaving quarantine again needs a host ``activate``.
- ``register`` of a package whose on-disk hash does not match its stamped
  ``version_hash`` records it as ``quarantined`` (fail safe, still auditable).
- Name collision is an error (design: "a name collision between plugins is a load
  error"): registering a different package (other hash or other root) under a name
  held by a non-retired entry raises :class:`PluginNameCollisionError` and leaves the
  existing entry and its pin untouched. The same package again is a no-op. A retired
  name may be registered anew (retire first, then register the upgrade).
- Every committed transition emits ``{"event": "plugin.lifecycle", "verdict":
  "allow", "action", "plugin", "from", "to", "version_hash", "actor", "actor_kind",
  "reason"}`` through :func:`safe_emit`, after the state file is written. A refused
  change (non-host principal, name collision, activation hash mismatch) emits the
  same shape with ``verdict: "deny"`` and ``to: None``.

State file:
- ``<state_dir>/kernel-plugins.json`` (:data:`REGISTRY_FILENAME`), written atomically
  (temp file, fsync, replace; mode 0600). It is NOT the Agent Plugin marker
  ``plugin-data/<ws>/<name>/enabled`` (``nanobot.agent.plugins``), which an agent can
  forge from ``exec``; that marker is never read here.
- The file floor denies the file tools read and write: in the split layout all of
  ``state_dir`` is protected (``ProtectedFloor.from_paths``, Task 3); in the legacy
  flat layout (``state_dir`` is the workspace) the file is protected by name.
- ``exec`` is not stopped by the file floor (the documented exec caveat). A forged
  entry has to match the package bytes at load, but that is no protection against an
  exec-capable agent: it can write its own package AND a matching "active" entry,
  hashed with the public ``compute_version_hash``, and ``check_active`` then returns
  True. Not proof against an exec-capable agent; strict mode plus a sandbox that does
  not bind ``state_dir`` read-write closes this gap.
- Every call re-reads the file, so two instances on one ``state_dir`` agree. A corrupt
  file fails closed: ``check_active`` returns ``False``, other calls raise
  :class:`PluginRegistryError`. Writers are serialised per instance only; the host is
  the single writer.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, get_args

from loguru import logger

from nanobot.kernel.floors import check_floors
from nanobot.kernel.manifest import MANIFEST_FILENAME, PluginManifest, compute_version_hash
from nanobot.kernel.policy import CapabilityRequest, Deny, Principal
from nanobot.kernel.trace import NullTraceSink, TraceSink, safe_emit

REGISTRY_FILENAME = "kernel-plugins.json"
_SCHEMA_VERSION = 1

PluginState = Literal["candidate", "quarantined", "active", "retired"]
PLUGIN_STATES: tuple[str, ...] = get_args(PluginState)

#: Actor recorded for the load-time auto-quarantine (host-side code, no caller principal).
REGISTRY_PRINCIPAL = Principal("plugin-registry", "host")

_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("candidate", "quarantined"),
    ("quarantined", "active"),
    ("active", "quarantined"),
    ("candidate", "retired"),
    ("quarantined", "retired"),
    ("active", "retired"),
})


class PluginRegistryError(Exception):
    """Base error for the kernel plugin registry."""


class PluginLifecycleDeniedError(PluginRegistryError, PermissionError):
    """A non-host principal tried to change lifecycle state (``plugin.load`` floor)."""

    def __init__(self, deny: Deny) -> None:
        super().__init__(deny.reason)
        self.deny = deny


class PluginHashMismatchError(PluginRegistryError):
    """Activation hashes disagreed; the entry is now ``quarantined``."""


class PluginNameCollisionError(PluginRegistryError):
    """A different package is already registered under this name."""


class PluginStateError(PluginRegistryError):
    """The requested transition is not allowed from the entry's current state."""


class UnknownPluginError(PluginRegistryError, LookupError):
    """No entry with this name."""


@dataclass(frozen=True)
class PluginRecord:
    """One registry entry. ``version_hash`` is the pin once ``state == "active"``."""

    name: str
    kind: str
    version: str
    version_hash: str
    tier: int
    root: str
    state: PluginState
    registered_at: str
    activated_at: str | None = None
    activated_by: str | None = None
    reason: str = ""

    @classmethod
    def from_json(cls, data: Any) -> PluginRecord:
        if not isinstance(data, dict):
            raise ValueError("entry is not an object")
        record = cls(**data)
        if record.state not in PLUGIN_STATES:
            raise ValueError(f"unknown state {record.state!r}")
        return record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_version_hash(root: Path, name: str | None = None) -> str | None:
    """Hash of the package at *root* as it is on disk now, or ``None`` if unreadable.

    Re-reads ``moeka-plugin.json`` (so a widened manifest changes the result) and, when
    *name* is given, requires the manifest's name to match it.
    """
    try:
        manifest = PluginManifest.model_validate_json(
            (root / MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        if name is not None and manifest.name != name:
            return None
        return compute_version_hash(root, manifest)
    except Exception as exc:  # noqa: BLE001 - any failure is "does not match"
        logger.debug("plugin package {} unhashable: {!r}", root, exc)
        return None


class PluginRegistry:
    """Host-owned lifecycle state for kernel plugins, persisted under ``state_dir``."""

    def __init__(self, state_dir: Path, *, trace: TraceSink | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / REGISTRY_FILENAME
        self._trace: TraceSink = trace if trace is not None else NullTraceSink()
        self._lock = threading.RLock()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> dict[str, PluginRecord]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise PluginRegistryError(f"plugin registry {self.path} unreadable: {exc}") from exc
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("schema") != _SCHEMA_VERSION:
                raise ValueError("unknown schema")
            plugins = data["plugins"]
            if not isinstance(plugins, dict):
                raise ValueError("plugins is not an object")
            records = {name: PluginRecord.from_json(raw) for name, raw in plugins.items()}
            if any(name != record.name for name, record in records.items()):
                raise ValueError("entry key does not match its name")
        except (ValueError, KeyError, TypeError) as exc:
            raise PluginRegistryError(f"plugin registry {self.path} is corrupt: {exc}") from exc
        return records

    def _save(self, records: dict[str, PluginRecord]) -> None:
        from nanobot.utils.helpers import _write_text_atomic  # lazy: Ruling C

        payload = {
            "schema": _SCHEMA_VERSION,
            "plugins": {name: asdict(records[name]) for name in sorted(records)},
        }
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(self.path, json.dumps(payload, indent=2, sort_keys=True))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- audit ---------------------------------------------------------------

    def _event(
        self, action: str, verdict: str, actor: Principal, name: str, *,
        old: str | None, new: str | None, version_hash: str | None, reason: str = "",
    ) -> dict[str, Any]:
        return {
            "event": "plugin.lifecycle",
            "verdict": verdict,
            "action": action,
            "plugin": name,
            "from": old,
            "to": new,
            "version_hash": version_hash,
            "actor": actor.name,
            "actor_kind": actor.kind,
            "reason": reason,
        }

    def _emit(self, events: Iterable[dict[str, Any]]) -> None:
        for event in events:
            safe_emit(self._trace, event)

    def _require_host(self, action: str, name: str, principal: Principal) -> None:
        deny = check_floors(principal, CapabilityRequest("plugin.load", f"{action}:{name}"))
        if deny is None:
            return
        current: PluginRecord | None = None
        try:
            current = self._load().get(name)
        except PluginRegistryError:
            pass
        self._emit([self._event(
            action, "deny", principal, name,
            old=current.state if current else None, new=None,
            version_hash=current.version_hash if current else None, reason=deny.reason,
        )])
        raise PluginLifecycleDeniedError(deny)

    def _get(self, records: dict[str, PluginRecord], name: str) -> PluginRecord:
        record = records.get(name)
        if record is None:
            raise UnknownPluginError(f"no kernel plugin registered as {name!r}")
        return record

    def _move(
        self, record: PluginRecord, new: PluginState, action: str, actor: Principal,
        reason: str, events: list[dict[str, Any]], **changes: Any,
    ) -> PluginRecord:
        if (record.state, new) not in _TRANSITIONS:
            raise PluginStateError(
                f"kernel plugin {record.name!r}: {record.state} -> {new} is not allowed"
            )
        events.append(self._event(
            action, "allow", actor, record.name, old=record.state, new=new,
            version_hash=record.version_hash, reason=reason,
        ))
        return replace(record, state=new, reason=reason, **changes)

    # -- lifecycle -----------------------------------------------------------

    def register(
        self, manifest: PluginManifest, root: Path, *, principal: Principal,
    ) -> PluginRecord:
        """Record *manifest* (package at *root*) as ``candidate``, or ``quarantined``
        when the package on disk does not hash to its stamped ``version_hash``."""
        name = manifest.name
        self._require_host("register", name, principal)
        root_str = str(Path(root).resolve())
        with self._lock:
            records = self._load()
            existing = records.get(name)
            if existing is not None and existing.state != "retired":
                if existing.version_hash == manifest.version_hash and existing.root == root_str:
                    return existing
                reason = (
                    f"name collision: {name!r} is already registered "
                    f"({existing.state}, {existing.version_hash[:12]} at {existing.root})"
                )
                self._emit([self._event(
                    "register", "deny", principal, name, old=existing.state, new=None,
                    version_hash=manifest.version_hash, reason=reason,
                )])
                raise PluginNameCollisionError(reason)
            disk = current_version_hash(Path(root), name)
            if disk == manifest.version_hash:
                state: PluginState = "candidate"
                reason = "registered"
            else:
                state = "quarantined"
                reason = "package hash does not match the manifest's version_hash"
            record = PluginRecord(
                name=name, kind=manifest.kind, version=manifest.version,
                version_hash=manifest.version_hash, tier=manifest.tier, root=root_str,
                state=state, registered_at=_now(), reason=reason,
            )
            records[name] = record
            self._save(records)
        self._emit([self._event(
            "register", "allow", principal, name, old=None, new=state,
            version_hash=record.version_hash, reason=reason,
        )])
        return record

    def activate(self, name: str, expected_hash: str, principal: Principal) -> PluginRecord:
        """Host approval: pin *expected_hash* and make the entry ``active``.

        Raises :class:`PluginHashMismatchError` (entry left ``quarantined``) unless the
        registered hash, *expected_hash* and the on-disk hash all agree.
        """
        self._require_host("activate", name, principal)
        events: list[dict[str, Any]] = []
        mismatch: str | None = None
        with self._lock:
            records = self._load()
            record = self._get(records, name)
            if record.state == "retired":
                raise PluginStateError(f"kernel plugin {name!r} is retired; register it again")
            disk = current_version_hash(Path(record.root), name)
            if expected_hash != record.version_hash:
                mismatch = "expected hash does not match the registered version_hash"
            elif disk != record.version_hash:
                mismatch = "package on disk does not match the registered version_hash"
            if mismatch is None and record.state == "active":
                return record
            if record.state != "quarantined":
                review = mismatch or "host review"
                record = self._move(record, "quarantined", "activate", principal, review, events)
            if mismatch is None:
                record = self._move(
                    record, "active", "activate", principal, "host approved (hash pinned)",
                    events, activated_at=_now(), activated_by=principal.name,
                )
            if events:
                records[name] = record
                self._save(records)
        if mismatch is not None:
            events.append(self._event(
                "activate", "deny", principal, name, old=record.state, new=None,
                version_hash=record.version_hash, reason=mismatch,
            ))
        self._emit(events)
        if mismatch is not None:
            raise PluginHashMismatchError(f"kernel plugin {name!r} quarantined: {mismatch}")
        return record

    def quarantine(
        self, name: str, principal: Principal, *, reason: str = "host review",
    ) -> PluginRecord:
        """Host moves a ``candidate`` or ``active`` entry to ``quarantined``."""
        self._require_host("quarantine", name, principal)
        events: list[dict[str, Any]] = []
        with self._lock:
            records = self._load()
            record = self._get(records, name)
            if record.state == "quarantined":
                return record
            record = self._move(record, "quarantined", "quarantine", principal, reason, events)
            records[name] = record
            self._save(records)
        self._emit(events)
        return record

    def retire(self, name: str, principal: Principal, *, reason: str = "host retired") -> PluginRecord:
        """Host retires the entry from any state (a retired entry is left as is)."""
        self._require_host("retire", name, principal)
        events: list[dict[str, Any]] = []
        with self._lock:
            records = self._load()
            record = self._get(records, name)
            if record.state == "retired":
                return record
            record = self._move(record, "retired", "retire", principal, reason, events)
            records[name] = record
            self._save(records)
        self._emit(events)
        return record

    def check_active(self, name: str, current_hash: str | None = None) -> bool:
        """Load-time check: ``True`` only for an active entry whose hash still matches.

        *current_hash* is the caller's hash of the package as it will be loaded; when
        ``None`` it is recomputed from the registered root. A mismatch moves the entry
        to ``quarantined`` (actor :data:`REGISTRY_PRINCIPAL`) and returns ``False``.
        Any registry error returns ``False`` (fail closed).
        """
        events: list[dict[str, Any]] = []
        try:
            with self._lock:
                records = self._load()
                record = records.get(name)
                if record is None or record.state != "active":
                    return False
                current = (
                    current_hash if current_hash is not None
                    else current_version_hash(Path(record.root), name)
                )
                if current == record.version_hash:
                    return True
                record = self._move(
                    record, "quarantined", "load_check", REGISTRY_PRINCIPAL,
                    "hash mismatch at load", events,
                )
                records[name] = record
                self._save(records)
        except Exception as exc:  # noqa: BLE001 - the load check must fail closed
            logger.warning("kernel plugin {!r} load check failed closed: {!r}", name, exc)
            return False
        self._emit(events)  # only once the quarantine is on disk
        return False

    # -- read access (any caller) --------------------------------------------

    def get(self, name: str) -> PluginRecord | None:
        return self._load().get(name)

    def list_plugins(self, state: str | None = None) -> list[PluginRecord]:
        if state is not None and state not in PLUGIN_STATES:
            raise ValueError(f"unknown plugin state {state!r}; expected one of {PLUGIN_STATES}")
        records = self._load()
        return [
            records[name] for name in sorted(records)
            if state is None or records[name].state == state
        ]


__all__ = [
    "PLUGIN_STATES",
    "REGISTRY_FILENAME",
    "REGISTRY_PRINCIPAL",
    "PluginHashMismatchError",
    "PluginLifecycleDeniedError",
    "PluginNameCollisionError",
    "PluginRecord",
    "PluginRegistry",
    "PluginRegistryError",
    "PluginState",
    "PluginStateError",
    "UnknownPluginError",
    "current_version_hash",
]
