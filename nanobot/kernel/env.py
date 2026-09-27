"""Host contract: the environment a host hands to the kernel (stdlib + loguru only)."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from nanobot.kernel.trace import TraceSink


class PathsOverlapError(ValueError):
    """work_dir and state_dir alias, or one is nested inside the other."""


@dataclass(frozen=True)
class Paths:
    """Separated work (agent-visible) and state (kernel-private) directories."""

    work_dir: Path
    state_dir: Path
    overlap_ok: bool = False

    def __post_init__(self) -> None:
        work = Path(self.work_dir).resolve()
        state = Path(self.state_dir).resolve()
        object.__setattr__(self, "work_dir", work)
        object.__setattr__(self, "state_dir", state)
        if not self.overlap_ok and self.overlaps:
            raise PathsOverlapError(
                f"work_dir {work} and state_dir {state} overlap; "
                "pass overlap_ok=True for the legacy flat layout"
            )

    @property
    def overlaps(self) -> bool:
        return self.work_dir.is_relative_to(self.state_dir) or self.state_dir.is_relative_to(
            self.work_dir
        )

    @property
    def sessions_root(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def data_dir(self) -> Path:
        return self.state_dir / "data"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def media_dir(self) -> Path:
        return self.work_dir / "media"


class CredentialResolver(Protocol):
    def resolve(self, ref: str, scope: str) -> str | None: ...


class StaticCredentialResolver:
    """In-memory resolver with an optional per-ref scope allow-list."""

    def __init__(
        self,
        values: Mapping[str, str],
        scopes: Mapping[str, Collection[str]] | None = None,
    ) -> None:
        self._values = dict(values)
        self._scopes = {k: frozenset(v) for k, v in (scopes or {}).items()}

    def resolve(self, ref: str, scope: str) -> str | None:
        if ref not in self._values:
            return None
        allowed = self._scopes.get(ref)
        if allowed is not None and scope not in allowed:
            return None
        return self._values[ref]

    def __repr__(self) -> str:
        return f"StaticCredentialResolver(refs={sorted(self._values)})"


class ConfigSource(Protocol):
    def section(self, name: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CoreEnvironment:
    config: ConfigSource
    credentials: CredentialResolver
    paths: Paths
    trace: TraceSink
    exec_base_env: Mapping[str, str] = field(default_factory=dict)
    strict: bool = False
