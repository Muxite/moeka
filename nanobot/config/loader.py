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

from nanobot.config.schema import Config

# Tracks the dotted field path during `_resolve_in_place` recursion so the
# env-var warning can tell the user *where* in config.json the missing
# `${VAR}` reference lives (e.g. `providers.openrouter.apiKey`).
_RESOLVE_PATH: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "_RESOLVE_PATH", default=(),
)

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


_DEPRECATED_MOEKA_STATE_WARNED = False


def get_state_home() -> Path:
    """
    Return the unified Moeka instance directory.

    As of v0.1.5 the former split between a "state" dir and a separate
    "workspace" dir is gone — both now live at the same path. The default
    is ``~/.nanobot`` for upstream-nanobot compatibility; ``MOEKA_WORKSPACE``
    overrides it. Legacy names are accepted for back-compat.

    Resolution order:
      1. ``MOEKA_WORKSPACE`` env var (preferred).
      2. ``MOEKA_STATE`` env var (deprecated — warns once).
      3. ``NANOBOT_HOME`` env var (forward-compat).
      4. Default: ``~/.nanobot``.

    :returns: Expanded absolute path (directory not guaranteed to exist).
    """
    global _DEPRECATED_MOEKA_STATE_WARNED
    override = os.environ.get("MOEKA_WORKSPACE")
    if not override:
        legacy = os.environ.get("MOEKA_STATE")
        if legacy:
            if not _DEPRECATED_MOEKA_STATE_WARNED:
                logger.warning(
                    "MOEKA_STATE is deprecated; rename to MOEKA_WORKSPACE "
                    "(state and workspace are one directory now)."
                )
                _DEPRECATED_MOEKA_STATE_WARNED = True
            override = legacy
    if not override:
        override = os.environ.get("NANOBOT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".nanobot"


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
    path = config_path or get_config_path()

    # Ensure forward refs in Config are resolved before instantiation. The
    # eager rebuild at schema import time may have failed (circular import);
    # this catches the lazy case.
    from nanobot.config.schema import _resolve_tool_config_refs
    try:
        _resolve_tool_config_refs()
    except Exception:
        pass

    config = Config()
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            config = Config.model_validate(data)
        except (json.JSONDecodeError, ValueError, pydantic.ValidationError) as e:
            logger.warning("Failed to load config from {}: {}", path, e)
            logger.warning("Using default configuration.")

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
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = config.model_dump(mode="json", by_alias=True)
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.error("Failed to save config to {}: {}", path, exc)


_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_config_env_vars(config: Config) -> Config:
    """Return *config* with ``${VAR}`` env-var references resolved.

    Walks in place so fields declared with ``exclude=True`` (e.g.
    ``DreamConfig.cron``) survive; returns the same instance when no
    references are present. Missing variables are logged as warnings and
    their placeholders are left unreplaced so the rest of the system can
    still start.

    """
    return _resolve_in_place(config)


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
