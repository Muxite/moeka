"""Runtime log visibility controls shared by CLI commands."""

from typing import Any

from loguru import logger

__all__ = ["_set_nanobot_logs", "add_console_sink"]

_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <5}</level> | "
    "<cyan>{extra[channel]}</cyan> | "
    "<level>{message}</level>"
)


def _set_nanobot_logs(enabled: bool) -> None:
    if enabled:
        logger.enable("nanobot")
    else:
        logger.disable("nanobot")


def add_console_sink(sink: Any, level: str) -> int:
    """Add the unified nanobot console sink and return its handler id.

    ``diagnose`` stays off: loguru's default renders every frame's local
    variables in tracebacks, which leaked channel tokens embedded in request
    URLs (``api.telegram.org/bot<TOKEN>/...``) into the service journal.
    """
    return logger.add(
        sink,
        format=_CONSOLE_FORMAT,
        level=level,
        colorize=None,
        diagnose=False,
        filter=lambda record: record["extra"].setdefault("channel", "-") or True,
    )
