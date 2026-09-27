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
    """``ConfigSource`` over a pydantic ``Config``; sections are top-level fields.

    - ``source_path`` set (a config loaded from ``config.json``): sections reflect
      the file on disk, re-read when its bytes change. This is the pre-kernel
      ``load_config()`` re-read that hot reload (image generation, MCP, web search
      config, model presets, provider snapshots) relied on, now owned here (R2).
    - ``source_path`` unset: sections come from the held (in-memory) ``Config``.
    - ``runtime``: the synthetic ``runtime`` section (``LEGACY_ENV_SETTINGS``).
    - ``config=None`` with a path: a pure file source (``None`` path = the
      current ``get_config_path()``, resolved at each read).
    """

    def __init__(
        self,
        config: Any,
        *,
        source_path: Path | None = None,
        runtime: dict[str, str] | None = None,
        file_backed: bool = False,
    ) -> None:
        self._config = config
        self._source_path = source_path
        self._file_backed = file_backed or source_path is not None
        self._runtime = dict(runtime or {})
        self._cache: tuple[bytes | None, Any] | None = None

    def _read_file(self) -> Any:
        from nanobot.config import loader

        path = self._source_path or loader.get_config_path()
        try:
            raw: bytes | None = path.read_bytes()
        except FileNotFoundError:
            raw = None
        cached = self._cache
        if cached is not None and cached[0] == raw:
            return cached[1]
        # Errors propagate (callers report "could not reload"); nothing is cached.
        config = loader.resolve_config_env_vars(loader.load_config(path), config_path=path)
        self._cache = (raw, config)
        return config

    def snapshot(self) -> Any:
        """The current ``Config`` (fresh from disk for a file-backed source)."""
        if self._file_backed:
            return self._read_file()
        return self._config

    def section(self, name: str) -> dict[str, Any]:
        if name == "runtime":
            return dict(self._runtime)
        config = self.snapshot()
        if name not in type(config).model_fields:
            return {}
        value = config.model_dump(by_alias=False, include={name}).get(name)
        return value if isinstance(value, dict) else {}

    def __repr__(self) -> str:
        return "ConfigSectionSource()"


def file_config_source(path: Path | None = None) -> _ConfigSectionSource:
    """A ``ConfigSource`` over ``config.json`` at *path* (``None`` = current config path).

    Host-side helpers that are handed a config file path (and env-less legacy
    callers) read through this instead of calling ``load_config()`` themselves.
    """
    resolved = Path(path).expanduser().resolve() if path is not None else None
    return _ConfigSectionSource(
        None, source_path=resolved, runtime=legacy_runtime_settings(), file_backed=True
    )


# Non-credential tunables the pre-kernel code read from ``os.environ``
# (setting -> env var names, first set name wins). LegacyEnvironment exposes them
# as ``env.config.section("runtime")[<key>]``; kernel-native hosts get defaults
# (or whatever their own ConfigSource returns), never the process environment.
LEGACY_ENV_SETTINGS: dict[str, tuple[str, ...]] = {
    # providers/base.py streaming idle timeout (seconds)
    "runtime/stream_idle_timeout_s": ("NANOBOT_STREAM_IDLE_TIMEOUT_S",),
    # providers/openai_compat_provider.py request timeout (seconds)
    "runtime/openai_compat_timeout_s": ("NANOBOT_OPENAI_COMPAT_TIMEOUT_S",),
    # agent/loop.py concurrent request cap (<=0 or unset = unlimited)
    "runtime/max_concurrent_requests": ("NANOBOT_MAX_CONCURRENT_REQUESTS",),
    # security/workspace_access.py host sandbox markers
    "runtime/workspace_sandbox_provider": ("NANOBOT_WORKSPACE_SANDBOX_PROVIDER",),
    "runtime/workspace_sandbox_enforced": ("NANOBOT_WORKSPACE_SANDBOX_ENFORCED",),
    "runtime/sandbox_enforced": ("NANOBOT_SANDBOX_ENFORCED",),
}


def legacy_runtime_settings() -> dict[str, str]:
    """The ``runtime`` section from the process env (legacy hosts / env=None paths)."""
    values: dict[str, str] = {}
    for setting, names in LEGACY_ENV_SETTINGS.items():
        for name in names:
            value = os.environ.get(name)
            if value is not None:
                values[setting.partition("/")[2]] = value
                break
    return values


def ambient_env_var(name: str) -> str | None:
    """One process env var (legacy env=None fallback, e.g. skill ``requires.env``)."""
    return os.environ.get(name)


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


def _collect_credentials(config: Any) -> tuple[dict[str, str], dict[str, str]]:
    """``(env_values, config_values)``: refs from the process env and from ``Config``."""
    env_values: dict[str, str] = {}
    for ref in LEGACY_ENV_REFS:
        value = ambient_credential(ref)
        if value:
            env_values[ref] = value
    config_values: dict[str, str] = {}
    providers = config.providers
    slots: dict[str, Any] = {n: getattr(providers, n) for n in type(providers).model_fields}
    slots.update(providers.model_extra or {})
    for slot, prov in slots.items():
        key = getattr(prov, "api_key", None)
        if key:
            config_values[f"providers/{slot}/api_key"] = key
    search = config.tools.web.search
    if search.api_key and search.provider:
        config_values[f"web/{search.provider}"] = search.api_key
    return env_values, config_values


class _LegacyCredentialResolver(StaticCredentialResolver):
    """Named refs, each scoped to its owner, plus the ``exec`` scope over a env snapshot.

    ``exec`` lookups (``allowed_env_keys`` for the exec child env, R3) and ``skills``
    presence checks (``requires.env``) read only the process-env snapshot taken at
    construction and never a named ref; no other scope can read that snapshot.

    Refs provided by ``Config`` win over the process environment, but they are a
    startup mirror of the config: callers that hold the current config (hot
    reload) use its value and ask :meth:`resolve_non_config`, which answers from
    the process-env refs only, so a key cleared in config stops being used.
    """

    _SNAPSHOT_SCOPES = frozenset({"exec", "skills"})

    def __init__(
        self,
        env_values: dict[str, str],
        config_values: dict[str, str],
        exec_env: dict[str, str],
    ) -> None:
        values = {**env_values, **config_values}
        super().__init__(values, scopes={ref: (legacy_scope_for_ref(ref),) for ref in values})
        self._env_only = StaticCredentialResolver(
            env_values, scopes={ref: (legacy_scope_for_ref(ref),) for ref in env_values}
        )
        self._exec_env = exec_env

    def resolve(self, ref: str, scope: str) -> str | None:
        if scope in self._SNAPSHOT_SCOPES:
            return self._exec_env.get(ref)
        return super().resolve(ref, scope)

    def resolve_non_config(self, ref: str, scope: str) -> str | None:
        """Like :meth:`resolve`, ignoring values that came from ``Config``."""
        if scope in self._SNAPSHOT_SCOPES:
            return self._exec_env.get(ref)
        return self._env_only.resolve(ref, scope)


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
        env_values, config_values = _collect_credentials(config)
        env = CoreEnvironment(
            config=_ConfigSectionSource(
                config,
                source_path=getattr(config, "source_path", None),
                runtime=legacy_runtime_settings(),
            ),
            credentials=_LegacyCredentialResolver(
                env_values, config_values, dict(exec_base_env)
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
    "LEGACY_ENV_SETTINGS",
    "LegacyEnvironment",
    "ambient_credential",
    "ambient_env_var",
    "file_config_source",
    "legacy_runtime_settings",
    "legacy_scope_for_ref",
    "process_env_snapshot",
]
