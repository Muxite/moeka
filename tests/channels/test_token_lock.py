"""Spec 005 FR-041/FR-042: one poller per bot token (host-wide channel token lock)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager
from nanobot.channels.token_lock import (
    ChannelTokenInUseError,
    acquire_channel_token_lock,
    token_digest,
)
from nanobot.config.schema import Config

TOKEN = "123456:test-token-not-real"


def test_lock_file_names_and_sidecar_hold_no_token(tmp_path: Path) -> None:
    lock = acquire_channel_token_lock(
        "telegram", TOKEN, run_dir=tmp_path, owner={"workspace": "/ws/a", "config": "/ws/a/c"},
    )
    try:
        digest = hashlib.sha256(TOKEN.encode()).hexdigest()
        assert token_digest(TOKEN) == digest[:16]
        path = tmp_path / "channel-locks" / f"telegram-{digest[:16]}.lock"
        sidecar = path.with_suffix(".json")
        assert path.exists() and sidecar.exists()
        record = json.loads(sidecar.read_text())
        assert record["pid"] == os.getpid() and record["workspace"] == "/ws/a"
        assert record["config"] == "/ws/a/c" and record["started_at"]
        for file in (path, sidecar):
            raw = file.read_text()
            assert TOKEN not in raw and digest not in raw
        with pytest.raises(ChannelTokenInUseError) as info:
            acquire_channel_token_lock("telegram", TOKEN, run_dir=tmp_path)
        assert info.value.holder["workspace"] == "/ws/a"
        assert str(info.value).startswith("channel_token_in_use:")
        # Another channel or token is independent.
        acquire_channel_token_lock("discord", TOKEN, run_dir=tmp_path).release()
        acquire_channel_token_lock("telegram", TOKEN + "x", run_dir=tmp_path).release()
    finally:
        lock.release()
    with acquire_channel_token_lock("telegram", TOKEN, run_dir=tmp_path):
        pass


@pytest.mark.parametrize("token", ["", "   ", "${TELEGRAM_TOKEN}"])
def test_empty_or_unexpanded_tokens_take_no_lock(tmp_path: Path, token: str) -> None:
    with pytest.raises(ValueError):
        acquire_channel_token_lock("telegram", token, run_dir=tmp_path)
    assert not (tmp_path / "channel-locks").exists() or \
        list((tmp_path / "channel-locks").iterdir()) == []


_HOLDER = textwrap.dedent("""
    import sys, time
    from nanobot.channels.token_lock import acquire_channel_token_lock
    lock = acquire_channel_token_lock("telegram", sys.argv[2], run_dir=sys.argv[1],
                                      owner={"workspace": "/other/instance"})
    print("held", flush=True)
    time.sleep(120)
""")


def test_cross_process_holder_is_reported(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(tmp_path), TOKEN], stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
        with pytest.raises(ChannelTokenInUseError) as info:
            acquire_channel_token_lock("telegram", TOKEN, run_dir=tmp_path)
        assert info.value.holder["pid"] == proc.pid
        assert "/other/instance" in str(info.value) and f"pid {proc.pid}" in str(info.value)
    finally:
        proc.kill()
        proc.wait()
    acquire_channel_token_lock("telegram", TOKEN, run_dir=tmp_path).release()


class _Dummy(BaseChannel):
    name = "dummy"
    display_name = "Dummy"

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg: Any) -> None:
        return None


def _manager(tmp_path: Path, token: str = TOKEN) -> ChannelManager:
    config = Config.model_validate({
        "agents": {"defaults": {"workspace": str(tmp_path / "ws")}},
        "channels": {
            "telegram": {"enabled": True, "token": token, "allowFrom": ["*"]},
            "websocket": {"enabled": False},
        },
    })
    return ChannelManager(config, MessageBus(), config_path=tmp_path / "config.json")


async def test_manager_reports_locked_and_starts_other_channels(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MOEKA_RUN_DIR", str(tmp_path / "run"))
    holder = acquire_channel_token_lock(
        "telegram", TOKEN, run_dir=tmp_path / "run", owner={"workspace": "/elsewhere/ws"},
    )
    manager = _manager(tmp_path)
    telegram = manager.channels["telegram"]
    started: list[bool] = []

    async def no_network() -> None:  # never reached while the token is locked
        started.append(True)

    monkeypatch.setattr(telegram, "start", no_network)
    manager.channels["dummy"] = _Dummy({}, manager.bus)
    try:
        await manager.start_all()
        status = manager.get_status()
        assert status["telegram"]["state"] == "locked"
        assert status["telegram"]["error"].startswith("channel_token_in_use:")
        assert "/elsewhere/ws" in status["telegram"]["error"]
        assert f"pid {os.getpid()}" in status["telegram"]["error"]
        assert started == []
        assert status["dummy"]["state"] == "running"
    finally:
        await manager.stop_all()
        holder.release()


async def test_manager_takes_and_releases_the_lock(tmp_path: Path, monkeypatch) -> None:
    run = tmp_path / "run"
    monkeypatch.setenv("MOEKA_RUN_DIR", str(run))
    manager = _manager(tmp_path)
    telegram = manager.channels["telegram"]

    async def fake_start() -> None:
        telegram._running = True

    async def fake_stop() -> None:
        telegram._running = False

    monkeypatch.setattr(telegram, "start", fake_start)
    monkeypatch.setattr(telegram, "stop", fake_stop)
    await manager._start_channel("telegram", telegram)
    with pytest.raises(ChannelTokenInUseError):
        acquire_channel_token_lock("telegram", TOKEN, run_dir=run)
    sidecar = next((run / "channel-locks").glob("*.json"))
    record = json.loads(sidecar.read_text())
    assert record["workspace"] == str(tmp_path / "ws")
    assert record["config"] == str((tmp_path / "config.json").resolve())
    await manager._stop_channel("telegram")
    acquire_channel_token_lock("telegram", TOKEN, run_dir=run).release()


async def test_unexpanded_token_takes_no_lock(tmp_path: Path, monkeypatch) -> None:
    run = tmp_path / "run"
    monkeypatch.setenv("MOEKA_RUN_DIR", str(run))
    manager = _manager(tmp_path, token="${TELEGRAM_TOKEN}")
    telegram = manager.channels["telegram"]

    async def fake_start() -> None:
        return None

    monkeypatch.setattr(telegram, "start", fake_start)
    await manager._start_channel("telegram", telegram)
    assert not (run / "channel-locks").exists() or not list((run / "channel-locks").iterdir())
    assert manager.get_status()["telegram"]["state"] != "locked"
