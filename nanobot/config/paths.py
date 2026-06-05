"""Runtime path helpers derived from the active config context."""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

from nanobot.utils.helpers import ensure_dir

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
    """Get the configuration file path (lazy import to break circular dependency).

    Delegates to ``nanobot.config.loader.get_config_path`` at call time so
    that importing this module never triggers a circular import during startup.
    """
    from nanobot.config.loader import get_config_path as _loader_get_config_path
    return _loader_get_config_path()


def get_data_dir() -> Path:
    """Return the instance-level runtime data directory."""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """Return the media directory, optionally namespaced per channel."""
    base = get_runtime_subdir("media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir() -> Path:
    """Return the cron storage directory."""
    return get_runtime_subdir("cron")


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return get_runtime_subdir("logs")


def get_webui_dir() -> Path:
    """Return the directory for WebUI-only persisted display threads (JSON)."""
    return get_runtime_subdir("webui")


def _default_workspace() -> Path:
    """
    Resolve the default workspace path for this Moeka instance.

    State and workspace are unified into one directory — this just returns
    the state home. Kept as a function so call sites stay readable and so
    the ``is_default_workspace`` comparison has a single source of truth.

    :returns: ``get_state_home()`` — the unified Moeka instance directory.
    """
    return get_state_home()


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure the agent workspace path."""
    path = Path(workspace).expanduser() if workspace else _default_workspace()
    return ensure_dir(path)


def is_default_workspace(workspace: str | Path | None) -> bool:
    """Return whether a workspace resolves to nanobot's default workspace path."""
    current = Path(workspace).expanduser() if workspace is not None else _default_workspace()
    default = _default_workspace()
    return current.resolve(strict=False) == default.resolve(strict=False)


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return get_state_home() / "history" / "cli_history"


def get_bridge_install_dir() -> Path:
    """Return the shared WhatsApp bridge installation directory."""
    return get_state_home() / "bridge"


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return get_state_home() / "sessions"
