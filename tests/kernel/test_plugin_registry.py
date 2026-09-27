"""Host-owned kernel plugin registry and lifecycle (Task 18, design section 9).

Headline properties:
- only a host principal changes lifecycle state (the ``plugin.load`` floor);
- activation pins the hash, re-verified against the package on disk, and any
  mismatch fails safe into ``quarantined``, never ``active``;
- a load-time hash mismatch auto-quarantines an active plugin;
- the legacy ``plugin-data/<ws>/<name>/enabled`` marker (writable from exec) never
  makes a plugin active here;
- the state file sits under ``state_dir`` behind the file floor;
- every transition (and every denied attempt) is an audit event.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.kernel.env import Paths
from nanobot.kernel.floors import PLUGIN_LOAD_MARKER, check_floors
from nanobot.kernel.manifest import MANIFEST_FILENAME, PluginManifest, compute_version_hash
from nanobot.kernel.policy import CapabilityRequest, Principal
from nanobot.kernel.registry import (
    REGISTRY_FILENAME,
    PluginHashMismatchError,
    PluginLifecycleDeniedError,
    PluginNameCollisionError,
    PluginRegistry,
    PluginStateError,
    UnknownPluginError,
)
from nanobot.security.protected_paths import PROTECTED_MARKER, ProtectedFloor

HOST = Principal("host", "host")
AGENT = Principal("main", "agent", parent=HOST)
SUB = Principal("sub-1", "subagent", parent=AGENT)
PLUGIN = Principal("other-plugin", "plugin", parent=HOST)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def transitions(self) -> list[tuple[str | None, str]]:
        return [
            (e["from"], e["to"]) for e in self.events if e.get("verdict") == "allow"
        ]


def _package(root: Path, name: str = "brave-search", body: str = "print('v1')\n") -> PluginManifest:
    """Write a stamped kernel plugin package under *root* and return its manifest."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.py").write_text(body, encoding="utf-8")
    raw = {
        "name": name,
        "kind": "search_backend",
        "version": "1.0.0",
        "version_hash": "0" * 64,
        "tier": 2,
        "capabilities_requested": ["net.fetch:api.search.brave.com"],
        "entry": "plugin:create",
    }
    raw["version_hash"] = compute_version_hash(root, raw)
    (root / MANIFEST_FILENAME).write_text(json.dumps(raw), encoding="utf-8")
    return PluginManifest.model_validate(raw)


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def registry(state_dir: Path, sink: RecordingSink) -> PluginRegistry:
    return PluginRegistry(state_dir, trace=sink)


# -- register / activate ----------------------------------------------------


def test_register_creates_candidate(registry: PluginRegistry, tmp_path: Path) -> None:
    manifest = _package(tmp_path / "pkg")
    record = registry.register(manifest, tmp_path / "pkg", principal=HOST)
    assert record.state == "candidate"
    assert record.version_hash == manifest.version_hash
    assert record.tier == 2
    assert record.activated_at is None and record.activated_by is None
    assert registry.get("brave-search") == record


def test_host_activation_with_correct_hash_is_active(
    registry: PluginRegistry, sink: RecordingSink, tmp_path: Path,
) -> None:
    manifest = _package(tmp_path / "pkg")
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    record = registry.activate("brave-search", manifest.version_hash, HOST)
    assert record.state == "active"
    assert record.activated_by == "host"
    assert record.activated_at is not None
    # The diagram's path: candidate -> quarantined (review) -> active (hash pinned).
    assert sink.transitions() == [
        (None, "candidate"), ("candidate", "quarantined"), ("quarantined", "active"),
    ]
    assert registry.check_active("brave-search") is True


@pytest.mark.parametrize("principal", [AGENT, SUB, PLUGIN], ids=lambda p: p.kind)
def test_non_host_cannot_change_lifecycle(
    registry: PluginRegistry, sink: RecordingSink, tmp_path: Path, principal: Principal,
) -> None:
    manifest = _package(tmp_path / "pkg")
    with pytest.raises(PluginLifecycleDeniedError) as exc:
        registry.register(manifest, tmp_path / "pkg", principal=principal)
    assert PLUGIN_LOAD_MARKER in str(exc.value)
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    for call in (
        lambda: registry.activate("brave-search", manifest.version_hash, principal),
        lambda: registry.quarantine("brave-search", principal),
        lambda: registry.retire("brave-search", principal),
    ):
        with pytest.raises(PluginLifecycleDeniedError):
            call()
    assert registry.get("brave-search").state == "candidate"
    denied = [e for e in sink.events if e.get("verdict") == "deny"]
    assert {e["action"] for e in denied} == {"register", "activate", "quarantine", "retire"}
    assert all(e["actor_kind"] == principal.kind for e in denied)


def test_activation_with_wrong_hash_quarantines(
    registry: PluginRegistry, sink: RecordingSink, tmp_path: Path,
) -> None:
    manifest = _package(tmp_path / "pkg")
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    with pytest.raises(PluginHashMismatchError):
        registry.activate("brave-search", "f" * 64, HOST)
    record = registry.get("brave-search")
    assert record.state == "quarantined"
    assert record.activated_at is None
    assert registry.check_active("brave-search") is False
    assert ("quarantined", "active") not in sink.transitions()


def test_activation_rechecks_disk_after_tamper(
    registry: PluginRegistry, sink: RecordingSink, tmp_path: Path,
) -> None:
    """The package changes between register and activate: the stale pin fails safe."""
    manifest = _package(tmp_path / "pkg")
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    (tmp_path / "pkg" / "plugin.py").write_text("import os; os.system('evil')\n")
    assert compute_version_hash(tmp_path / "pkg", manifest) != manifest.version_hash
    # The caller passes the registered (now stale) hash; the on-disk recheck catches it.
    with pytest.raises(PluginHashMismatchError):
        registry.activate("brave-search", manifest.version_hash, HOST)
    assert registry.get("brave-search").state == "quarantined"
    assert "active" not in [to for _, to in sink.transitions()]


def test_tampered_package_registers_quarantined(registry: PluginRegistry, tmp_path: Path) -> None:
    manifest = _package(tmp_path / "pkg")
    (tmp_path / "pkg" / "plugin.py").write_text("tampered\n")
    record = registry.register(manifest, tmp_path / "pkg", principal=HOST)
    assert record.state == "quarantined"
    with pytest.raises(PluginHashMismatchError):
        registry.activate("brave-search", manifest.version_hash, HOST)
    assert registry.get("brave-search").state == "quarantined"


def test_activate_unknown_and_retired(registry: PluginRegistry, tmp_path: Path) -> None:
    with pytest.raises(UnknownPluginError):
        registry.activate("nope", "0" * 64, HOST)
    manifest = _package(tmp_path / "pkg")
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    registry.retire("brave-search", HOST)
    with pytest.raises(PluginStateError):
        registry.activate("brave-search", manifest.version_hash, HOST)
    assert registry.get("brave-search").state == "retired"


# -- load-time check --------------------------------------------------------


def test_check_active_quarantines_on_load_mismatch(
    registry: PluginRegistry, sink: RecordingSink, tmp_path: Path,
) -> None:
    manifest = _package(tmp_path / "pkg")
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    registry.activate("brave-search", manifest.version_hash, HOST)
    assert registry.check_active("brave-search", manifest.version_hash) is True

    # Code changes on disk after activation (e.g. rewritten from exec).
    (tmp_path / "pkg" / "plugin.py").write_text("print('v2')\n")
    assert registry.check_active("brave-search") is False
    assert registry.get("brave-search").state == "quarantined"
    last = sink.events[-1]
    assert (last["from"], last["to"], last["action"]) == ("active", "quarantined", "load_check")
    assert last["actor_kind"] == "host"
    # Stays out: a later check never re-activates by itself.
    assert registry.check_active("brave-search", manifest.version_hash) is False


def test_check_active_with_caller_hash_mismatch(registry: PluginRegistry, tmp_path: Path) -> None:
    manifest = _package(tmp_path / "pkg")
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    registry.activate("brave-search", manifest.version_hash, HOST)
    assert registry.check_active("brave-search", "e" * 64) is False
    assert registry.get("brave-search").state == "quarantined"


def test_check_active_false_for_unknown_or_inactive(
    registry: PluginRegistry, tmp_path: Path,
) -> None:
    assert registry.check_active("nope") is False
    manifest = _package(tmp_path / "pkg")
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    assert registry.check_active("brave-search", manifest.version_hash) is False
    assert registry.get("brave-search").state == "candidate"


def test_legacy_enabled_marker_does_not_activate(
    registry: PluginRegistry, state_dir: Path, tmp_path: Path,
) -> None:
    """A forged Agent Plugin marker (what exec can write) is not a kernel activation."""
    manifest = _package(tmp_path / "pkg")
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    for base in (state_dir, state_dir / "data"):
        marker = base / "plugin-data" / "0123456789ab" / "brave-search" / "enabled"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"fingerprint": manifest.version_hash, "root": str(tmp_path / "pkg")}),
        )
    fresh = PluginRegistry(state_dir)
    assert fresh.check_active("brave-search", manifest.version_hash) is False
    assert fresh.get("brave-search").state == "candidate"
    assert fresh.list_plugins("active") == []


def test_corrupt_state_file_fails_closed(state_dir: Path, tmp_path: Path) -> None:
    registry = PluginRegistry(state_dir)
    manifest = _package(tmp_path / "pkg")
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    registry.activate("brave-search", manifest.version_hash, HOST)
    (state_dir / REGISTRY_FILENAME).write_text("{not json")
    assert registry.check_active("brave-search", manifest.version_hash) is False


# -- retire / quarantine ----------------------------------------------------


@pytest.mark.parametrize("prior", ["candidate", "quarantined", "active"])
def test_retire_from_any_state(
    registry: PluginRegistry, sink: RecordingSink, tmp_path: Path, prior: str,
) -> None:
    manifest = _package(tmp_path / "pkg")
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    if prior == "quarantined":
        registry.quarantine("brave-search", HOST)
    elif prior == "active":
        registry.activate("brave-search", manifest.version_hash, HOST)
    assert registry.get("brave-search").state == prior
    record = registry.retire("brave-search", HOST)
    assert record.state == "retired"
    assert sink.transitions()[-1] == (prior, "retired")
    assert registry.check_active("brave-search", manifest.version_hash) is False
    # Retiring again is a no-op, not a transition.
    count = len(sink.events)
    assert registry.retire("brave-search", HOST).state == "retired"
    assert len(sink.events) == count


def test_quarantine_then_reapprove(registry: PluginRegistry, tmp_path: Path) -> None:
    manifest = _package(tmp_path / "pkg")
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    registry.activate("brave-search", manifest.version_hash, HOST)
    registry.quarantine("brave-search", HOST, reason="suspicious traffic")
    assert registry.get("brave-search").state == "quarantined"
    assert registry.get("brave-search").reason == "suspicious traffic"
    assert registry.activate("brave-search", manifest.version_hash, HOST).state == "active"


# -- name collision ---------------------------------------------------------


def test_name_collision_is_an_error(
    registry: PluginRegistry, sink: RecordingSink, tmp_path: Path,
) -> None:
    first = _package(tmp_path / "a")
    registry.register(first, tmp_path / "a", principal=HOST)
    registry.activate("brave-search", first.version_hash, HOST)
    second = _package(tmp_path / "b", body="print('another')\n")
    assert second.version_hash != first.version_hash
    with pytest.raises(PluginNameCollisionError):
        registry.register(second, tmp_path / "b", principal=HOST)
    # The existing entry and its pin are untouched.
    record = registry.get("brave-search")
    assert (record.state, record.version_hash) == ("active", first.version_hash)
    assert sink.events[-1]["verdict"] == "deny"
    assert sink.events[-1]["action"] == "register"


def test_same_hash_at_other_root_collides(registry: PluginRegistry, tmp_path: Path) -> None:
    first = _package(tmp_path / "a")
    registry.register(first, tmp_path / "a", principal=HOST)
    copy = _package(tmp_path / "b")
    assert copy.version_hash == first.version_hash
    with pytest.raises(PluginNameCollisionError):
        registry.register(copy, tmp_path / "b", principal=HOST)


def test_reregister_same_package_is_idempotent(
    registry: PluginRegistry, sink: RecordingSink, tmp_path: Path,
) -> None:
    manifest = _package(tmp_path / "pkg")
    first = registry.register(manifest, tmp_path / "pkg", principal=HOST)
    count = len(sink.events)
    assert registry.register(manifest, tmp_path / "pkg", principal=HOST) == first
    assert len(sink.events) == count


def test_retired_name_can_be_registered_again(registry: PluginRegistry, tmp_path: Path) -> None:
    first = _package(tmp_path / "a")
    registry.register(first, tmp_path / "a", principal=HOST)
    registry.retire("brave-search", HOST)
    second = _package(tmp_path / "b", body="print('v2')\n")
    record = registry.register(second, tmp_path / "b", principal=HOST)
    assert (record.state, record.version_hash) == ("candidate", second.version_hash)


# -- persistence and listing ------------------------------------------------


def test_state_survives_a_fresh_instance(state_dir: Path, tmp_path: Path) -> None:
    registry = PluginRegistry(state_dir)
    a = _package(tmp_path / "a", name="alpha")
    b = _package(tmp_path / "b", name="beta")
    registry.register(a, tmp_path / "a", principal=HOST)
    registry.register(b, tmp_path / "b", principal=HOST)
    registry.activate("alpha", a.version_hash, HOST)

    assert (state_dir / REGISTRY_FILENAME).is_file()
    fresh = PluginRegistry(state_dir)
    assert fresh.get("alpha") == registry.get("alpha")
    assert [r.name for r in fresh.list_plugins()] == ["alpha", "beta"]
    assert [r.name for r in fresh.list_plugins("active")] == ["alpha"]
    assert [r.name for r in fresh.list_plugins("candidate")] == ["beta"]
    assert fresh.check_active("alpha", a.version_hash) is True
    on_disk = json.loads((state_dir / REGISTRY_FILENAME).read_text())
    entry = on_disk["plugins"]["alpha"]
    assert {"name", "version_hash", "tier", "state", "registered_at", "activated_at",
            "activated_by"} <= set(entry)


def test_list_plugins_rejects_unknown_state(registry: PluginRegistry) -> None:
    assert registry.list_plugins() == []
    with pytest.raises(ValueError):
        registry.list_plugins("enabled")


# -- audit events -----------------------------------------------------------


def test_every_transition_is_audited(
    registry: PluginRegistry, sink: RecordingSink, tmp_path: Path,
) -> None:
    manifest = _package(tmp_path / "pkg")
    registry.register(manifest, tmp_path / "pkg", principal=HOST)
    registry.activate("brave-search", manifest.version_hash, HOST)
    registry.quarantine("brave-search", HOST)
    registry.activate("brave-search", manifest.version_hash, HOST)
    (tmp_path / "pkg" / "plugin.py").write_text("changed\n")
    registry.check_active("brave-search")
    registry.retire("brave-search", HOST)

    assert sink.transitions() == [
        (None, "candidate"),
        ("candidate", "quarantined"),
        ("quarantined", "active"),
        ("active", "quarantined"),
        ("quarantined", "active"),
        ("active", "quarantined"),
        ("quarantined", "retired"),
    ]
    for event in sink.events:
        assert event["event"] == "plugin.lifecycle"
        assert event["plugin"] == "brave-search"
        assert event["version_hash"] == manifest.version_hash
        assert event["actor_kind"] == "host"
        assert event["actor"]
        assert "action" in event


# -- file floor -------------------------------------------------------------


def _assert_floor_protects(floor: ProtectedFloor, path: Path) -> None:
    for cap in ("fs.write", "fs.read"):
        deny = check_floors(AGENT, CapabilityRequest(cap, str(path)), protected=floor)
        assert deny is not None, cap
        assert deny.marker == PROTECTED_MARKER


def test_state_file_protected_in_split_layout(tmp_path: Path) -> None:
    """Split layout: everything under state_dir is already floor-protected (Task 3)."""
    paths = Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state")
    registry = PluginRegistry(paths.state_dir)
    assert registry.path == paths.state_dir / REGISTRY_FILENAME
    _assert_floor_protects(ProtectedFloor.from_paths(paths), registry.path)


def test_state_file_protected_in_legacy_flat_layout(tmp_path: Path) -> None:
    """Legacy flat layout: state_dir is the workspace, so the file is named explicitly."""
    ws = tmp_path / "ws"
    paths = Paths(work_dir=ws, state_dir=ws, overlap_ok=True)
    registry = PluginRegistry(paths.state_dir)
    _assert_floor_protects(ProtectedFloor.from_paths(paths), registry.path)
    # The env-less legacy floor (workspace == state home) covers it too.
    _assert_floor_protects(ProtectedFloor(data_dir=None, workspace=ws), registry.path)
    # A neighbouring workspace file is still writable.
    other = ws / "notes.json"
    assert ProtectedFloor.from_paths(paths).reason(other, write=True) is None
