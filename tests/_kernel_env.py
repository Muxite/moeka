"""Test helper: a kernel-native CoreEnvironment carrying only the given credentials."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.trace import NullTraceSink


class _EmptyConfig:
    def section(self, name: str) -> dict[str, Any]:
        return {}


def credential_env(
    values: Mapping[str, str] | None = None,
    scopes: Mapping[str, Collection[str]] | None = None,
    *,
    exec_base_env: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> CoreEnvironment:
    """Build an env whose resolver holds exactly ``values`` (nothing ambient).

    ``Paths`` only resolves its roots (no filesystem access), so the default
    root never needs to exist.
    """
    base = Path(root) if root is not None else Path("/nonexistent-kernel-test")
    return CoreEnvironment(
        config=_EmptyConfig(),
        credentials=StaticCredentialResolver(values or {}, scopes),
        paths=Paths(work_dir=base / "work", state_dir=base / "state"),
        trace=NullTraceSink(),
        exec_base_env=dict(exec_base_env or {}),
    )
