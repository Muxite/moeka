"""Spec 005 FR-043: ``telegram.error.Conflict`` pauses polling, once per episode."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("telegram")

from loguru import logger  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from telegram.error import Conflict, NetworkError  # noqa: E402

from nanobot.bus.queue import MessageBus  # noqa: E402
from nanobot.channels.manager import ChannelManager  # noqa: E402
from nanobot.channels.telegram.runtime import TelegramChannel, _LivenessTrackedRequest  # noqa: E402
from nanobot.config.schema import Config  # noqa: E402

TOKEN = "123456:secret-token-value"


@pytest.fixture
def errors():
    records: list[Any] = []
    sink = logger.add(lambda m: records.append(m.record), level="ERROR")
    yield records
    logger.remove(sink)


def _channel(**extra: Any) -> TelegramChannel:
    return TelegramChannel({"enabled": True, "token": TOKEN, **extra}, MessageBus())


class _Updater:
    def __init__(self) -> None:
        self.running = True
        self.stops = 0
        self.starts: list[float] = []

    async def stop(self) -> None:
        self.running = False
        self.stops += 1

    async def start_polling(self, **kwargs: Any) -> None:
        self.running = True
        self.starts.append(asyncio.get_running_loop().time())


class _App:
    def __init__(self) -> None:
        self.updater = _Updater()


def test_conflict_retry_config_and_range() -> None:
    assert _channel().conflict_retry_s == 60
    assert _channel(conflictRetryS=5).conflict_retry_s == 5
    assert _channel(conflictRetryS=3600).conflict_retry_s == 3600
    for bad in (4, 3601):
        with pytest.raises(ValidationError):
            _channel(conflictRetryS=bad)


def test_conflict_episode_logs_once_and_ends_on_success(errors) -> None:
    channel = _channel()
    assert channel.polling_state == "polling"
    for _ in range(3):
        channel.handle_polling_error(Conflict("Conflict: terminated by other getUpdates request"))
    assert channel.polling_state == "conflict"
    conflict_lines = [r for r in errors if "Conflict" in r["message"]]
    assert len(conflict_lines) == 1
    message = conflict_lines[0]["message"]
    assert "another process is polling this bot token" in message
    assert TOKEN not in message and "secret-token" not in message
    channel._note_poll_ok()  # a successful getUpdates round trip ends the episode
    assert channel.polling_state == "polling"
    channel.handle_polling_error(Conflict("again"))
    assert len([r for r in errors if "Conflict" in r["message"]]) == 2


def test_other_errors_keep_their_handling(errors) -> None:
    channel = _channel()
    channel.handle_polling_error(NetworkError("blip"))
    assert channel.polling_state == "polling"
    assert errors == []


def test_webhook_conflict_logged_once_without_state_change(errors) -> None:
    channel = _channel(
        mode="webhook", webhookUrl="https://example.com/tg", webhookSecretToken="abc_DEF-1",
    )
    channel.handle_polling_error(Conflict("x"))
    channel.handle_polling_error(Conflict("x"))
    assert channel.polling_state == "polling"
    assert len([r for r in errors if "Conflict" in r["message"]]) == 1


async def test_conflict_stops_polling_and_retries_no_earlier_than_configured() -> None:
    channel = _channel(conflictRetryS=5)
    app = _App()
    channel._app = app  # type: ignore[assignment]
    channel._running = True
    loop = asyncio.get_running_loop()
    began = loop.time()
    channel.handle_polling_error(Conflict("c"))
    await asyncio.sleep(0.2)
    assert app.updater.stops == 1 and app.updater.running is False
    # Fast-forward the retry deadline check without waiting 5 real seconds.
    assert channel._conflict_retry_at is not None
    assert channel._conflict_retry_at - __import__("time").monotonic() > 4
    channel._conflict_retry_at = __import__("time").monotonic() + 0.3
    await asyncio.sleep(1.5)
    assert len(app.updater.starts) == 1 and app.updater.starts[0] - began >= 0.3
    assert channel.polling_state == "conflict"  # until a successful round trip
    channel._running = False


async def test_liveness_wrapper_ends_the_episode_only_on_2xx() -> None:
    channel = _channel()
    channel.handle_polling_error(Conflict("c"))

    class _Inner:
        read_timeout = 1.0

        def __init__(self, code: int) -> None:
            self.code = code

        async def initialize(self) -> None: ...

        async def shutdown(self) -> None: ...

        async def do_request(self, *a: Any, **k: Any) -> tuple[int, bytes]:
            return self.code, b"{}"

    await _LivenessTrackedRequest(_Inner(409), channel._note_poll_alive,
                                  on_success=channel._note_poll_ok).do_request()
    assert channel.polling_state == "conflict"
    await _LivenessTrackedRequest(_Inner(200), channel._note_poll_alive,
                                  on_success=channel._note_poll_ok).do_request()
    assert channel.polling_state == "polling"


def test_manager_reports_conflict_state(tmp_path: Path) -> None:
    config = Config.model_validate({
        "agents": {"defaults": {"workspace": str(tmp_path / "ws")}},
        "channels": {"telegram": {"enabled": True, "token": TOKEN, "allowFrom": ["*"]},
                     "websocket": {"enabled": False}},
    })
    manager = ChannelManager(config, MessageBus(), config_path=tmp_path / "c.json")
    channel = manager.channels["telegram"]
    channel.handle_polling_error(Conflict("c"))
    assert manager.get_status()["telegram"]["state"] == "conflict"
    channel._note_poll_ok()
    assert manager.get_status()["telegram"]["state"] != "conflict"
