"""Ambient adapter: build a CoreEnvironment from a legacy ``Config`` + process state.

This module (with ``nanobot/config/*``) is where ambient reads are allowed (R2).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.kernel.env import ConfigSource, CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.trace import LoguruTraceSink, safe_emit

_OVERLAP_WARNED = False


class _ConfigSectionSource:
    """``ConfigSource`` over a pydantic ``Config``; sections are top-level fields."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def section(self, name: str) -> dict[str, Any]:
        if name not in type(self._config).model_fields:
            return {}
        value = getattr(self._config, name, None)
        if value is None or not hasattr(value, "model_dump"):
            return {}
        return value.model_dump(by_alias=False)

    def __repr__(self) -> str:
        return "ConfigSectionSource()"


def _collect_credentials(config: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    providers = config.providers
    slots: dict[str, Any] = {n: getattr(providers, n) for n in type(providers).model_fields}
    slots.update(providers.model_extra or {})
    for slot, prov in slots.items():
        key = getattr(prov, "api_key", None)
        if key:
            values[f"providers/{slot}/api_key"] = key
    search = config.tools.web.search
    if search.api_key and search.provider:
        values[f"web/{search.provider}"] = search.api_key
    return values


def _legacy_data_dir() -> Path:
    """Today's ``get_data_dir()`` value, without creating it (evaluated lazily)."""
    from nanobot.config import loader

    return loader.get_config_path().parent


def _legacy_paths(config: Any) -> Paths:
    """Paths that keep every legacy on-disk location where it is today.

    - ``sessions_root``: the ``<workspace>-sessions`` sibling (``default_sessions_root``).
    - A config loaded from a file (``load_config`` binds its source path): data,
      media and logs stay at ``get_data_dir()``/``get_media_dir()``/``get_logs_dir()``,
      resolved lazily so building the env performs no filesystem or ``~`` access.
    - A purely in-memory config: data/logs/media derive from its own state dir
      (the workspace), never from ``~/.nanobot``.
    """
    from nanobot.session.sqlite_store import default_sessions_root

    workspace = Path(config.workspace_path).resolve()
    overrides: dict[str, Any] = {"sessions_root_override": default_sessions_root(workspace)}
    if getattr(config, "runtime_data_dir", None) is not None:
        overrides.update(
            data_dir_override=_legacy_data_dir,
            media_dir_override=lambda: _legacy_data_dir() / "media",
            logs_dir_override=lambda: _legacy_data_dir() / "logs",
        )
    return Paths(work_dir=workspace, state_dir=workspace, overlap_ok=True, **overrides)


def legacy_floor_extras() -> tuple[list[Path], list[Path]]:
    """Ambient floor roots for legacy hosts: config-file data dirs and config files."""
    from nanobot.security.protected_paths import default_config_files, default_data_dirs

    return default_data_dirs(), default_config_files()


class LegacyEnvironment:
    @staticmethod
    def from_config(config: Any) -> CoreEnvironment:
        """Wrap a ``Config`` (keys already ``${VAR}``-expanded) as a CoreEnvironment."""
        global _OVERLAP_WARNED
        # Legacy flat layout: memory and vec.db live in the workspace today, so the
        # kernel-private state dir is the workspace itself (overlap allowed).
        paths = _legacy_paths(config)
        trace = LoguruTraceSink()
        env = CoreEnvironment(
            config=_ConfigSectionSource(config),
            credentials=StaticCredentialResolver(_collect_credentials(config)),
            paths=paths,
            trace=trace,
            exec_base_env=dict(os.environ),
            strict=False,
        )
        if paths.overlaps and not _OVERLAP_WARNED:
            _OVERLAP_WARNED = True
            logger.warning(
                "work_dir {} and state_dir {} overlap (legacy flat layout); allowed for now",
                paths.work_dir,
                paths.state_dir,
            )
            safe_emit(
                trace,
                {
                    "event": "paths.overlap_allowed",
                    "work_dir": str(paths.work_dir),
                    "state_dir": str(paths.state_dir),
                },
            )
        return env


__all__ = ["ConfigSource", "LegacyEnvironment"]
