"""Configuration loading utilities."""

import contextvars
import json
import os
import re
from pathlib import Path
from typing import Any

import pydantic
from loguru import logger
from pydantic import BaseModel

from nanobot.config.paths import get_state_home  # noqa: F401 — re-export for back-compat
from nanobot.config.schema import Config
from nanobot.utils.helpers import _write_text_atomic

# Tracks the dotted field path during `_resolve_in_place` recursion so the
# env-var warning can tell the user *where* in config.json the missing
# `${VAR}` reference lives (e.g. `providers.openrouter.apiKey`).
_RESOLVE_PATH: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "_RESOLVE_PATH", default=(),
)

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None
_schema_refs_ready = False


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return get_state_home() / "config.json"


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    # moeka: lazy, circular-import-resilient forward-ref rebuild. The eager
    # rebuild at schema import time may have failed (circular import order);
    # this catches the lazy case. The import is function-local and wrapped in
    # try/except so a transient import-order failure never blocks config load.
    # The _schema_refs_ready flag keeps it to a single successful rebuild.
    global _schema_refs_ready
    if not _schema_refs_ready:
        try:
            from nanobot.config.schema import _resolve_tool_config_refs

            _resolve_tool_config_refs()
            _schema_refs_ready = True
        except Exception:
            pass

    path = config_path or get_config_path()

    config = Config()
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            config = Config.model_validate(data)
        except (json.JSONDecodeError, ValueError, pydantic.ValidationError) as e:
            raise ValueError(f"Failed to load config from {path}: {e}") from e

    _apply_ssrf_whitelist(config)
    return config


def config_from_sources(
    *,
    config: Config | None = None,
    config_dict: dict | None = None,
    config_path: Path | str | None = None,
) -> tuple[Config, bool]:
    """Resolve a :class:`Config` from at most one source — files optional.

    This is the shared file→data adapter used by the embedding entry points
    (``MoekaCore``, ``acomplete``). Whatever the host has — a pydantic object, a
    plain dict, a file path, or nothing — becomes a single resolved ``Config``.
    ``${VAR}`` placeholders are resolved from the environment in every case.

    Args:
        config: A pre-built :class:`Config` (pure data).
        config_dict: A plain dict (e.g. parsed JSON) validated into a ``Config``.
        config_path: Path to a ``config.json`` to read.
        (none): discover ``~/.nanobot/config.json`` as the gateway does.

    Returns:
        ``(config, from_file)`` — ``from_file`` is True when the config came from
        a file or default discovery (so the caller may trust
        ``config.workspace_path``), False for purely in-memory inputs.

    Raises:
        ValueError: more than one source supplied.
        FileNotFoundError: ``config_path`` given but missing.
    """
    sources = [s for s in (config, config_dict, config_path) if s is not None]
    if len(sources) > 1:
        raise ValueError("Pass at most one of config=, config_dict=, config_path=.")

    # Resolve forward refs before any in-memory model_validate (load_config does
    # this itself; the data routes need it too).
    from nanobot.config.schema import _resolve_tool_config_refs
    try:
        _resolve_tool_config_refs()
    except Exception:
        pass

    if config is not None:
        return resolve_config_env_vars(config), False
    if config_dict is not None:
        cfg = Config.model_validate(_migrate_config(dict(config_dict)))
        return resolve_config_env_vars(cfg), False

    resolved = None
    if config_path is not None:
        resolved = Path(config_path).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Config not found: {resolved}")
    return resolve_config_env_vars(load_config(resolved)), True


def _apply_ssrf_whitelist(config: Config) -> None:
    """Apply SSRF whitelist from config to the network security module."""
    from nanobot.security.network import configure_ssrf_whitelist

    configure_ssrf_whitelist(config.tools.ssrf_whitelist)



def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file using an atomic write so a mid-write crash
    never leaves a corrupt config.json.

    :param config: Configuration to save.
    :param config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json", by_alias=True)
    # OAuth credentials live in dedicated token stores. Persist only the
    # non-credential request settings consumed by these provider backends.
    for alias, provider in (
        ("openaiCodex", config.providers.openai_codex),
        ("xaiGrok", config.providers.xai_grok),
    ):
        settings = provider.model_dump(
            mode="json",
            by_alias=True,
            include={"proxy", "extra_body"},
            exclude_none=True,
        )
        if settings:
            data.setdefault("providers", {})[alias] = settings

    # Temp + replace so a crash mid-write cannot leave a truncated config.json.
    _write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False))


def merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively add missing defaults without replacing configured values."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = merge_missing_defaults(merged[key], value)
    return merged


_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_config_env_vars(config: Config) -> Config:
    """Return *config* with ``${VAR}`` env-var references resolved.

    Walks in place so fields declared with ``exclude=True`` (e.g.
    ``DreamConfig.cron``) survive; returns the same instance when no
    references are present. Missing variables are logged as warnings and
    their placeholders are left unreplaced so the rest of the system can
    still start (moeka deviation: non-fatal, since keys.env injects secrets
    at process start and a single missing var should not block boot).
    """
    return _resolve_in_place(config)


def resolve_env_refs(value: str) -> str:
    """Resolve ``${VAR}`` references in a single string, leniently.

    Unlike :func:`resolve_config_env_vars` (which walks a whole ``Config`` and
    raises on a missing variable), this resolves one value and returns an empty
    string if any reference is unset. It is meant for individual, lazily consumed
    fields — e.g. a transcription provider's ``api_key`` or ``api_base`` — so a
    missing variable degrades to "not configured" instead of producing a partial
    value. Non-string input is returned unchanged.
    """
    if not isinstance(value, str):
        return value
    names = _ENV_REF_PATTERN.findall(value)
    if any(name not in os.environ for name in names):
        return ""
    return _ENV_REF_PATTERN.sub(lambda m: os.environ[m.group(1)], value)


def _resolve_in_place(obj: Any) -> Any:
    if isinstance(obj, str):
        new = _ENV_REF_PATTERN.sub(_env_replace, obj)
        return new if new != obj else obj
    if isinstance(obj, BaseModel):
        updates: dict[str, Any] = {}
        base_path = _RESOLVE_PATH.get()
        for name in type(obj).model_fields:
            old = getattr(obj, name)
            token = _RESOLVE_PATH.set(base_path + (name,))
            try:
                new = _resolve_in_place(old)
            finally:
                _RESOLVE_PATH.reset(token)
            if new is not old:
                updates[name] = new
        extras = obj.__pydantic_extra__
        new_extras: dict[str, Any] | None = None
        if extras:
            resolved: dict[str, Any] = {}
            for k, v in extras.items():
                token = _RESOLVE_PATH.set(base_path + (k,))
                try:
                    resolved[k] = _resolve_in_place(v)
                finally:
                    _RESOLVE_PATH.reset(token)
            if any(resolved[k] is not extras[k] for k in extras):
                new_extras = resolved
        if not updates and new_extras is None:
            return obj
        copy = obj.model_copy(update=updates) if updates else obj.model_copy()
        if new_extras is not None:
            copy.__pydantic_extra__ = new_extras
        return copy
    if isinstance(obj, dict):
        base_path = _RESOLVE_PATH.get()
        resolved_dict: dict[Any, Any] = {}
        for k, v in obj.items():
            token = _RESOLVE_PATH.set(base_path + (str(k),))
            try:
                resolved_dict[k] = _resolve_in_place(v)
            finally:
                _RESOLVE_PATH.reset(token)
        return resolved_dict if any(resolved_dict[k] is not obj[k] for k in obj) else obj
    if isinstance(obj, list):
        base_path = _RESOLVE_PATH.get()
        resolved_list: list[Any] = []
        for i, v in enumerate(obj):
            token = _RESOLVE_PATH.set(base_path + (f"[{i}]",))
            try:
                resolved_list.append(_resolve_in_place(v))
            finally:
                _RESOLVE_PATH.reset(token)
        return resolved_list if any(nv is not ov for nv, ov in zip(resolved_list, obj)) else obj
    return obj


def _resolve_env_vars(obj: object) -> object:
    """Recursively resolve ``${VAR}`` patterns in plain strings/dicts/lists."""
    if isinstance(obj, str):
        return _ENV_REF_PATTERN.sub(_env_replace, obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def _env_replace(match: re.Match[str]) -> str:
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        path = _RESOLVE_PATH.get()
        location = ".".join(path) if path else "<unknown>"
        logger.warning(
            "Environment variable '{}' referenced in config at {} is not set; "
            "leaving placeholder unreplaced — dependent features will be unavailable",
            name, location,
        )
        return match.group(0)
    return value


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # Move tools.myEnabled / tools.mySet → tools.my.{enable, allowSet}.
    # The old flat keys shipped in the initial MyTool landing; wrapping them in a
    # sub-config keeps `web` / `exec` / `my` symmetric and gives room to grow.
    if "myEnabled" in tools or "mySet" in tools:
        my_cfg = tools.setdefault("my", {})
        if "myEnabled" in tools and "enable" not in my_cfg:
            my_cfg["enable"] = tools.pop("myEnabled")
        else:
            tools.pop("myEnabled", None)
        if "mySet" in tools and "allowSet" not in my_cfg:
            my_cfg["allowSet"] = tools.pop("mySet")
        else:
            tools.pop("mySet", None)

    return data
