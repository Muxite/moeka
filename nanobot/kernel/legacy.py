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


# The single table of credential refs the pre-kernel code read from ``os.environ``
# (ref -> env var names, first set name wins). LegacyEnvironment pre-populates its
# resolver from these, so ``BRAVE_API_KEY`` etc. keep working for legacy hosts;
# kernel-native hosts get nothing from the process environment (I1).
LEGACY_ENV_REFS: dict[str, tuple[str, ...]] = {
    # agent/tools/web.py (search backends; web/jina also serves the Jina fetch reader)
    "web/brave": ("BRAVE_API_KEY",),
    "web/tavily": ("TAVILY_API_KEY",),
    "web/searxng/base_url": ("SEARXNG_BASE_URL",),
    "web/jina": ("JINA_API_KEY",),
    "web/kagi": ("KAGI_API_KEY",),
    "web/exa": ("EXA_API_KEY",),
    "web/olostep": ("OLOSTEP_API_KEY",),
    "web/bocha": ("BOCHA_API_KEY",),
    "web/volcengine": ("VOLCENGINE_SEARCH_API_KEY", "WEB_SEARCH_API_KEY"),
    "web/serper": ("SERPER_API_KEY",),
    "web/keenable": ("KEENABLE_API_KEY",),
    "web/anysearch": ("ANYSEARCH_API_KEY",),
    # providers/transcription.py
    "transcription/assemblyai": ("ASSEMBLYAI_API_KEY",),
    "transcription/assemblyai/base_url": ("ASSEMBLYAI_BASE_URL",),
    "transcription/openai": ("OPENAI_API_KEY",),
    "transcription/openai/base_url": ("OPENAI_TRANSCRIPTION_BASE_URL",),
    "transcription/groq": ("GROQ_API_KEY",),
    "transcription/groq/base_url": ("GROQ_BASE_URL",),
    "transcription/openrouter": ("OPENROUTER_API_KEY",),
    "transcription/openrouter/base_url": ("OPENROUTER_BASE_URL",),
    "transcription/mimo": ("MIMO_API_KEY",),
    "transcription/mimo/base_url": ("MIMO_API_BASE",),
    "transcription/stepfun": ("STEPFUN_API_KEY",),
    # utils/searchusage.py
    "searchusage/tavily": ("TAVILY_API_KEY",),
    # providers/openai_compat_provider.py (enables the langfuse client wrapper)
    "observability/langfuse_secret": ("LANGFUSE_SECRET_KEY",),
    # providers/bedrock_provider.py
    "providers/bedrock/region": ("AWS_REGION", "AWS_DEFAULT_REGION"),
    # providers/github_copilot_provider.py (GitHub Enterprise endpoint overrides)
    "providers/github_copilot/api_base": ("NANOBOT_COPILOT_BASE_URL",),
    "providers/github_copilot/token_url": ("NANOBOT_COPILOT_TOKEN_URL",),
    "providers/github_copilot/client_id": ("NANOBOT_GITHUB_COPILOT_CLIENT_ID",),
    "providers/github_copilot/device_code_url": ("NANOBOT_GITHUB_DEVICE_CODE_URL",),
    "providers/github_copilot/access_token_url": ("NANOBOT_GITHUB_ACCESS_TOKEN_URL",),
    "providers/github_copilot/user_url": ("NANOBOT_GITHUB_USER_URL",),
}

# Scope that each ref family may be read from (StaticCredentialResolver allow-list).
_SCOPE_BY_FAMILY = {
    "web": "tool:web",
    "transcription": "transcription",
    "searchusage": "searchusage",
    "observability": "observability",
}


def legacy_scope_for_ref(ref: str) -> str:
    """The only scope allowed to read ``ref`` (``providers/<slot>/...`` -> ``provider:<slot>``)."""
    family, _, rest = ref.partition("/")
    if family == "providers":
        return f"provider:{rest.partition('/')[0]}"
    return _SCOPE_BY_FAMILY.get(family, family)


def ambient_credential(ref: str) -> str | None:
    """Read ``ref`` from the process env via LEGACY_ENV_REFS (legacy fallback only)."""
    for name in LEGACY_ENV_REFS.get(ref, ()):
        value = os.environ.get(name)
        if value:
            return value
    return None


def process_env_snapshot() -> dict[str, str]:
    """A copy of the process environment: the legacy exec base env (R3, env=None path)."""
    return dict(os.environ)


def _collect_credentials(config: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for ref in LEGACY_ENV_REFS:
        value = ambient_credential(ref)
        if value:
            values[ref] = value
    # Refs provided by Config win over the process environment.
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


class _LegacyCredentialResolver(StaticCredentialResolver):
    """Named refs, each scoped to its owner, plus the ``exec`` scope over a env snapshot.

    ``exec`` lookups (``allowed_env_keys`` for the exec child env, R3) read only the
    process-env snapshot taken at construction and never a named ref; no other scope
    can read that snapshot.
    """

    def __init__(self, values: dict[str, str], exec_env: dict[str, str]) -> None:
        super().__init__(values, scopes={ref: (legacy_scope_for_ref(ref),) for ref in values})
        self._exec_env = exec_env

    def resolve(self, ref: str, scope: str) -> str | None:
        if scope == "exec":
            return self._exec_env.get(ref)
        return super().resolve(ref, scope)


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

    # Sibling of the workspace path AS CONFIGURED (not resolved): for a symlinked
    # workspace that is ``<link>-sessions``, where the pre-kernel loop kept it.
    workspace = Path(config.workspace_path)
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
        exec_base_env = process_env_snapshot()
        env = CoreEnvironment(
            config=_ConfigSectionSource(config),
            credentials=_LegacyCredentialResolver(
                _collect_credentials(config), dict(exec_base_env)
            ),
            paths=paths,
            trace=trace,
            exec_base_env=exec_base_env,
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


__all__ = [
    "ConfigSource",
    "LEGACY_ENV_REFS",
    "LegacyEnvironment",
    "ambient_credential",
    "legacy_scope_for_ref",
    "process_env_snapshot",
]
