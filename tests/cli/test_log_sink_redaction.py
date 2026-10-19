"""The console sink must not render traceback locals (token leak, 2026-10-16)."""

import io

from loguru import logger

from nanobot.cli.log_control import add_console_sink

_FAKE_TOKEN = "1234567890:AAFakeTokenValueForLeakRegressionTest"


def _poll() -> None:
    url = f"https://api.telegram.org/bot{_FAKE_TOKEN}/getUpdates"
    # The raising line must reference ``url``: diagnose annotates only the
    # variables shown on each traceback line, as httpx's request frame did.
    raise ConnectionError("Temporary failure in name resolution", url[:0])


def test_console_sink_does_not_render_traceback_locals() -> None:
    sink = io.StringIO()
    handler_id = add_console_sink(sink, "DEBUG")
    try:
        try:
            _poll()
        except ConnectionError:
            logger.exception("polling network issue")
    finally:
        logger.remove(handler_id)

    output = sink.getvalue()
    assert "polling network issue" in output
    assert "ConnectionError" in output
    assert _FAKE_TOKEN not in output
