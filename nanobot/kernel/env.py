"""Host contract: the environment a host hands to the kernel (stdlib + loguru only)."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from nanobot.kernel.trace import TraceSink


class PathsOverlapError(ValueError):
    """work_dir and state_dir alias, or one is nested inside the other."""


PathOverride = Path | Callable[[], Path] | None


@dataclass(frozen=True)
class Paths:
    """Separated work (agent-visible) and state (kernel-private) directories.

    ``sessions_root``/``data_dir``/``logs_dir``/``media_dir`` derive from the two
    roots unless an explicit ``*_override`` is given. An override is a path or a
    zero-argument callable; a callable is evaluated on first access (never at
    construction) and cached, so an adapter can defer ambient lookups.
    """

    work_dir: Path
    state_dir: Path
    overlap_ok: bool = False
    sessions_root_override: PathOverride = None
    data_dir_override: PathOverride = None
    logs_dir_override: PathOverride = None
    media_dir_override: PathOverride = None
    _resolved: dict[str, Path] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        work = Path(self.work_dir).resolve()
        state = Path(self.state_dir).resolve()
        object.__setattr__(self, "work_dir", work)
        object.__setattr__(self, "state_dir", state)
        for name in ("sessions_root", "data_dir", "logs_dir", "media_dir"):
            value = getattr(self, f"{name}_override")
            if value is not None and not callable(value):
                object.__setattr__(self, f"{name}_override", Path(value).resolve())
        if not self.overlap_ok and self.overlaps:
            raise PathsOverlapError(
                f"work_dir {work} and state_dir {state} overlap; "
                "pass overlap_ok=True for the legacy flat layout"
            )

    def _dir(self, name: str, default: Path) -> Path:
        override = getattr(self, f"{name}_override")
        if override is None:
            return default
        if not callable(override):
            return override
        cached = self._resolved.get(name)
        if cached is None:
            cached = Path(override()).resolve()
            self._resolved[name] = cached
        return cached

    @property
    def overlaps(self) -> bool:
        return self.work_dir.is_relative_to(self.state_dir) or self.state_dir.is_relative_to(
            self.work_dir
        )

    @property
    def sessions_root(self) -> Path:
        return self._dir("sessions_root", self.state_dir / "sessions")

    @property
    def data_dir(self) -> Path:
        return self._dir("data_dir", self.state_dir / "data")

    @property
    def logs_dir(self) -> Path:
        return self._dir("logs_dir", self.state_dir / "logs")

    @property
    def media_dir(self) -> Path:
        return self._dir("media_dir", self.work_dir / "media")


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
    exec_base_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    strict: bool = False
    # The consumer a model call is attributed to when no layer bound one (usage-record.v1).
    consumer: str | None = None

    def __post_init__(self) -> None:
        # I2: ``overlap_ok=True`` is the legacy flat layout only; a strict env must
        # keep work_dir and state_dir separate whatever its Paths allow.
        if self.strict and self.paths.overlaps:
            raise PathsOverlapError(
                f"strict CoreEnvironment requires separate work_dir {self.paths.work_dir} "
                f"and state_dir {self.paths.state_dir} (overlap_ok=True is legacy-only)"
            )


def resolve_credential(
    env: CoreEnvironment | None,
    ref: str,
    scope: str,
    *,
    exclude_config: bool = False,
) -> str | None:
    """Resolve ``ref`` for ``scope`` through the host env (I1); empty values become ``None``.

    With an env, only ``env.credentials`` is consulted: a kernel-native host whose
    resolver lacks ``ref`` gets ``None`` even when a matching variable is set in the
    process. ``env=None`` is the legacy direct-construction path (a tool or provider
    built without a host env): it falls back to the pre-kernel environment variables
    via ``nanobot.kernel.legacy.LEGACY_ENV_REFS``, so behaviour is unchanged there.

    ``exclude_config=True``: the caller already consulted the current config for
    this value, so a resolver that mirrors config values (the legacy adapter's
    ``resolve_non_config``) must not hand back its startup copy. Resolvers without
    that method answer normally.
    """
    if env is not None:
        non_config = getattr(env.credentials, "resolve_non_config", None)
        if exclude_config and callable(non_config):
            value = non_config(ref, scope)
        else:
            value = env.credentials.resolve(ref, scope)
    else:
        from nanobot.kernel.legacy import ambient_credential

        value = ambient_credential(ref)
    return value or None


def missing_credential(ref: str) -> str:
    """Redaction-safe message for an absent credential: names the ref, never a value."""
    return f"missing credential {ref}"
