"""Explicit, certified capability transfer between Moeka workspaces.

Transfers are deliberately manual: ``stage`` only prepares a verified bundle and
``activate`` performs the replacement when an operator chooses a between-turn point.
Each activated version has a destination-local backup record for rollback.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import cast

from filelock import FileLock

from nanobot.capabilities.provenance import TrustStore, bundle_io, verify_bundle
from nanobot.config.schema import Config


class TransferError(ValueError):
    """A capability could not safely be staged, activated, or restored."""


def _json_object(value: object, *, label: str) -> dict[str, object]:
    """Narrow untrusted JSON to an object with string keys and object values."""
    if not isinstance(value, dict):
        raise TransferError(f"{label} must be a JSON object")
    result: dict[str, object] = {}
    # Runtime dict check above justifies treating this dynamic boundary as object pairs.
    raw_object = cast(dict[object, object], value)
    for key, item in raw_object.items():
        if not isinstance(key, str):
            raise TransferError(f"{label} contains a non-string key")
        result[key] = item
    return result


def _trust(config: Config) -> TrustStore:
    return TrustStore.load(config.capabilities.trust_dirs)


def _kind_dir(workspace: Path, kind: str) -> Path:
    if kind == "skill":
        return workspace / "skills"
    if kind == "script-tool":
        return workspace / "tools-script"
    raise TransferError(f"unsupported transferable kind: {kind}")


def _safe_bundle(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise TransferError(f"bundle is not a regular directory: {path}")


def _contained(workspace: Path, path: Path) -> Path:
    """Reject symlinked components and paths outside the workspace."""
    root = Path(os.path.abspath(workspace.expanduser()))
    resolved_root = root.resolve(strict=False)
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise TransferError("transfer path is outside the workspace") from exc
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise TransferError(f"transfer path contains a symlink: {cursor}")
    if not candidate.resolve(strict=False).is_relative_to(resolved_root):
        raise TransferError("transfer path resolves outside the workspace")
    return candidate


def preview(
    source: Config, destination: Config, kind: str, name: str,
) -> dict[str, object]:
    """Check certification, current revocation, destination trust and effect fit."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise TransferError("invalid capability name")
    if kind == "script-tool" and not destination.tools.script.enabled:
        raise TransferError("destination script tools are disabled")
    src = _contained(source.workspace_path, _kind_dir(source.workspace_path, kind) / name)
    destination_bundle = _contained(
        destination.workspace_path, _kind_dir(destination.workspace_path, kind) / name,
    )
    _safe_bundle(src)
    source_verdict = verify_bundle(src, _trust(source))
    if not source_verdict.ok:
        raise TransferError(f"source certificate refused: {source_verdict.reason}")
    # Reverify at the destination so its trust roots and revocation list remain the
    # authority for what can execute there.
    destination_verdict = verify_bundle(src, _trust(destination))
    if not destination_verdict.ok:
        raise TransferError(f"destination certificate refused: {destination_verdict.reason}")
    io = bundle_io(src, "skill" if kind == "skill" else "script-tool")
    if io is None:
        raise TransferError("capability has no valid I/O declaration")
    if "sudo" in io.effects:
        raise TransferError("capabilities declaring sudo effects cannot be transferred")
    denied = sorted(set(io.effects) - set(destination.capabilities.allowed_effects))
    if denied:
        raise TransferError("destination disallows effects: " + ", ".join(denied))
    if "exec" in io.effects and not destination.tools.exec.enable:
        raise TransferError("destination execution tools are disabled")
    return {
        "kind": kind,
        "name": name,
        "source": str(src),
        "destination": str(destination_bundle),
        "cert_id": source_verdict.cert_id,
        "effects": list(io.effects),
        "destination_compatible": True,
    }


def stage(source: Config, destination: Config, kind: str, name: str) -> str:
    """Copy one compatible certified bundle into destination-local staging."""
    details = preview(source, destination, kind, name)
    root = _contained(
        destination.workspace_path, destination.workspace_path / ".moeka-transfer",
    )
    stage_id = uuid.uuid4().hex
    stage_dir = root / "staged" / stage_id
    stage_dir.parent.mkdir(parents=True, exist_ok=True)
    bundle_path = stage_dir / name
    shutil.copytree(Path(str(details["source"])), bundle_path, symlinks=True)
    # A final verification catches source changes during the copy.
    verdict = verify_bundle(bundle_path, _trust(destination))
    if not verdict.ok:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise TransferError(f"staged certificate refused: {verdict.reason}")
    (stage_dir / "transfer.json").write_text(
        json.dumps({**details, "stage_id": stage_id}, indent=2) + "\n", encoding="utf-8",
    )
    return stage_id


def _read_stage(workspace: Path, stage_id: str) -> tuple[Path, dict[str, object]]:
    if not stage_id.isalnum():
        raise TransferError("invalid stage id")
    base = _contained(workspace, workspace / ".moeka-transfer" / "staged" / stage_id)
    try:
        raw_record: object = json.loads(
            (base / "transfer.json").read_text(encoding="utf-8"),
        )
        record = _json_object(raw_record, label="staged transfer record")
    except (OSError, ValueError) as exc:
        raise TransferError(f"cannot read staged transfer: {exc}") from exc
    if record.get("stage_id") != stage_id:
        raise TransferError("invalid staged transfer record")
    kind, name = record.get("kind"), record.get("name")
    if not isinstance(kind, str) or not isinstance(name, str):
        raise TransferError("staged transfer is missing kind or name")
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise TransferError("invalid staged capability name")
    # Reconstruct the target locally; never trust paths from the staged JSON.
    target = _contained(workspace, _kind_dir(workspace, kind) / name)
    return base, {**record, "target": str(target)}


def _activate_one(destination: Config, stage_id: str) -> dict[str, object]:
    """Apply a previously approved bundle at a quiescent turn boundary."""
    base, record = _read_stage(destination.workspace_path, stage_id)
    bundle = base / str(record["name"])
    _safe_bundle(bundle)
    if record["kind"] == "script-tool" and not destination.tools.script.enabled:
        raise TransferError("destination script tools are disabled")
    verdict = verify_bundle(bundle, _trust(destination))
    if not verdict.ok:
        raise TransferError(f"activation certificate refused: {verdict.reason}")
    io = bundle_io(bundle, str(record["kind"]))
    if (
        io is None or "sudo" in io.effects
        or set(io.effects) - set(destination.capabilities.allowed_effects)
        or ("exec" in io.effects and not destination.tools.exec.enable)
    ):
        raise TransferError("destination permissions changed; stage a compatible bundle")
    target = Path(str(record["target"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    history_root = _contained(
        destination.workspace_path,
        destination.workspace_path / ".moeka-transfer" / "history",
    )
    history_root.mkdir(parents=True, exist_ok=True)
    activation_id = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    history_dir = history_root / activation_id
    history_dir.mkdir()
    existed = target.exists()
    if existed:
        _safe_bundle(target)
        backup = history_dir / "previous" / target.name
        backup.parent.mkdir()
        shutil.copytree(target, backup, symlinks=True)
    else:
        backup = None
    temporary_root = target.with_name(f".{target.name}.transfer-{uuid.uuid4().hex}")
    temporary_root.mkdir()
    temporary = temporary_root / target.name
    shutil.copytree(bundle, temporary, symlinks=True)
    copied_verdict = verify_bundle(temporary, _trust(destination))
    if not copied_verdict.ok:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise TransferError(f"activation copy refused: {copied_verdict.reason}")
    # The target swap is atomic on the same filesystem. Keep the old target until
    # the new certified bundle is in place, then persist a rollback record.
    displaced = target.with_name(f".{target.name}.old-{uuid.uuid4().hex}") if existed else None
    if displaced is not None:
        os.replace(target, displaced)
    try:
        os.replace(temporary, target)
    except BaseException:
        if displaced is not None and displaced.exists():
            os.replace(displaced, target)
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise
    shutil.rmtree(temporary_root, ignore_errors=True)
    if displaced is not None:
        shutil.rmtree(displaced)
    (history_dir / "activation.json").write_text(json.dumps({
        "activation_id": activation_id,
        "kind": record["kind"],
        "name": record["name"],
        "target": str(target),
        "had_previous": backup is not None,
        "cert_id": verdict.cert_id,
    }, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(base)
    return {"activation_id": activation_id, "target": str(target), "cert_id": verdict.cert_id}


def approve_activation(destination: Config, stage_id: str) -> Path:
    """Record owner approval; the live agent applies it before a later turn."""
    # Validate the exact staged artifact now, then validate it again at activation.
    base, record = _read_stage(destination.workspace_path, stage_id)
    bundle = base / str(record["name"])
    _safe_bundle(bundle)
    verdict = verify_bundle(bundle, _trust(destination))
    if not verdict.ok:
        raise TransferError(f"activation certificate refused: {verdict.reason}")
    io = bundle_io(bundle, str(record["kind"]))
    if (
        io is None or "sudo" in io.effects
        or set(io.effects) - set(destination.capabilities.allowed_effects)
        or ("exec" in io.effects and not destination.tools.exec.enable)
    ):
        raise TransferError("destination permissions changed; stage a compatible bundle")
    if record["kind"] == "script-tool" and not destination.tools.script.enabled:
        raise TransferError("destination script tools are disabled")
    root = _contained(
        destination.workspace_path,
        destination.workspace_path / ".moeka-transfer" / "promotions",
    )
    pending = root / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    flag = pending / f"{stage_id}.json"
    with FileLock(str(root / "lock")):
        if flag.exists():
            raise TransferError("activation is already approved and pending")
        flag.write_text(json.dumps({"stage_id": stage_id}, indent=2) + "\n", encoding="utf-8")
    return flag


def activate_pending(
    destination: Config, *, allow_script_tools: bool = True,
) -> list[dict[str, object]]:
    """Apply owner-approved transfers when the caller has reached a turn boundary."""
    root = _contained(
        destination.workspace_path,
        destination.workspace_path / ".moeka-transfer" / "promotions",
    )
    pending_dir = root / "pending"
    if not pending_dir.is_dir() or pending_dir.is_symlink():
        return []
    activated: list[dict[str, object]] = []
    with FileLock(str(root / "lock")):
        for flag in sorted(pending_dir.glob("*.json")):
            if flag.is_symlink():
                continue
            try:
                raw_data: object = json.loads(flag.read_text(encoding="utf-8"))
                data = _json_object(raw_data, label="pending activation record")
                stage_id = data.get("stage_id")
                if not isinstance(stage_id, str) or flag.stem != stage_id:
                    raise TransferError("invalid pending activation record")
                if not allow_script_tools:
                    _base, staged = _read_stage(destination.workspace_path, stage_id)
                    if staged.get("kind") == "script-tool":
                        continue
                result = _activate_one(destination, stage_id)
            except (OSError, ValueError, TransferError) as exc:
                # Keep a rejected item for owner inspection and avoid retrying it on
                # every incoming turn. The reason is stored beside the pending flag.
                (flag.with_suffix(".refused.txt")).write_text(str(exc) + "\n", encoding="utf-8")
                flag.unlink(missing_ok=True)
                continue
            flag.unlink(missing_ok=True)
            activated.append(result)
    return activated


def activate_pending_from_env(
    workspace: Path, env: object | None, *, allow_script_tools: bool = True,
) -> list[dict[str, object]]:
    """Environment adapter for the agent loop; absent host config means no activation."""
    if env is None:
        return []
    config_source = getattr(env, "config", None)
    section = getattr(config_source, "section", None)
    if not callable(section):
        return []
    try:
        # Config subclasses BaseSettings; ``model_validate`` can rescan the
        # process environment even though this adapter already has an explicit
        # CoreEnvironment config source. Reuse the host's explicit-data validator
        # so a turn never consults ambient credentials while checking pending work.
        from nanobot.kernel.hostenv import validate_config_data

        destination = validate_config_data({
            "agents": {"defaults": {"workspace": str(workspace)}},
            "capabilities": section("capabilities"),
            "tools": section("tools"),
        })
    except Exception as exc:  # malformed host config must not break a user turn
        from loguru import logger

        logger.warning("capability transfer activation skipped: invalid host config ({})", exc)
        return []
    try:
        return activate_pending(destination, allow_script_tools=allow_script_tools)
    except (OSError, TransferError) as exc:
        from loguru import logger

        logger.warning("capability transfer activation skipped: {}", exc)
        return []


def history(workspace: Path) -> list[dict[str, object]]:
    """Return destination-local activation records, newest first."""
    root = _contained(workspace, workspace / ".moeka-transfer" / "history")
    rows: list[dict[str, object]] = []
    for file in sorted(root.glob("*/activation.json"), reverse=True) if root.exists() else ():
        try:
            raw_row: object = json.loads(file.read_text(encoding="utf-8"))
            row = _json_object(raw_row, label="activation history record")
        except (OSError, ValueError):
            continue
        if row.get("activation_id") == file.parent.name and not (
            file.parent / "rolled-back"
        ).exists():
            rows.append(row)
    return rows


def rollback(destination: Config, activation_id: str | None = None) -> str:
    """Undo the named (or latest) activation from destination-local history."""
    workspace = destination.workspace_path
    rows = history(workspace)
    row = next((item for item in rows if activation_id is None or
                item.get("activation_id") == activation_id), None)
    if row is None:
        raise TransferError("no matching activation history")
    target = _contained(workspace, Path(str(row.get("target", ""))))
    kind, name = row.get("kind"), row.get("name")
    if not isinstance(kind, str) or not isinstance(name, str):
        raise TransferError("invalid activation history")
    expected = _kind_dir(workspace, kind) / name
    if target != expected:
        raise TransferError("activation history target does not match workspace")
    record_dir = workspace / ".moeka-transfer" / "history" / str(row["activation_id"])
    if row.get("had_previous"):
        backup = record_dir / "previous" / name
        _safe_bundle(backup)
        verdict = verify_bundle(backup, _trust(destination))
        if not verdict.ok:
            raise TransferError(f"rollback certificate refused: {verdict.reason}")
        io = bundle_io(backup, str(kind))
        if io is None or "sudo" in io.effects or set(io.effects) - set(destination.capabilities.allowed_effects):
            raise TransferError("rollback bundle exceeds destination permissions")
        current = target.with_name(f".{target.name}.rollback-{uuid.uuid4().hex}")
        if target.exists():
            os.replace(target, current)
        os.replace(backup, target)
        shutil.rmtree(current, ignore_errors=True)
    else:
        if target.exists():
            _safe_bundle(target)
            shutil.rmtree(target)
    rolled = record_dir / "rolled-back"
    rolled.write_text("\n", encoding="utf-8")
    return str(target)


__all__ = [
    "TransferError", "activate_pending", "activate_pending_from_env", "approve_activation",
    "history", "preview", "rollback", "stage",
]
