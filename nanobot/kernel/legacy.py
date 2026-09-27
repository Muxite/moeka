"""Ambient adapter: build a CoreEnvironment from a legacy ``Config`` + process state.

This module (with ``nanobot/config/*``) is where ambient reads are allowed (R2).
"""

from __future__ import annotations

import os
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
    if search.api_key:
        values[f"web/{search.provider}"] = search.api_key
    return values


class LegacyEnvironment:
    @staticmethod
    def from_config(config: Any) -> CoreEnvironment:
        """Wrap a ``Config`` (keys already ``${VAR}``-expanded) as a CoreEnvironment."""
        global _OVERLAP_WARNED
        # Legacy flat layout: sessions.db, memory and vec.db all live in the workspace
        # today, so the kernel-private state dir is the workspace itself. (Resolving
        # the config-file home here would touch ~/.nanobot for in-memory embeds.)
        paths = Paths(
            work_dir=config.workspace_path,
            state_dir=config.workspace_path,
            overlap_ok=True,
        )
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
