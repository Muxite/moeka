"""Group G: per-token channel lock and Telegram Conflict (FR-041..FR-043)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import stat
from pathlib import Path

import pytest

from conftest import wait_for

TOKEN = "123456789:HELDOUT-fake-token-value-AAAA"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _lock_paths(run_dir: Path, channel: str, token: str) -> tuple[Path, Path]:
    base = run_dir / "channel-locks" / f"{channel}-{_digest(token)[:16]}"
    return Path(f"{base}.lock"), Path(f"{base}.json")


def _read(p: Path) -> dict:
    return json.loads(p.read_text())


# -- FR-041 helper and files -----------------------------------------------------------------------


@pytest.mark.fr("FR-041", "FR-042")
def test_token_lock_files_and_secrecy(h):
    from nanobot.channels.token_lock import acquire_channel_token_lock

    lock = acquire_channel_token_lock("telegram", TOKEN, run_dir=h.run_dir)
    try:
        lock_file, sidecar = _lock_paths(h.run_dir, "telegram", TOKEN)
        assert lock_file.exists() and sidecar.exists()
        meta = _read(sidecar)
        assert {"pid", "workspace", "config", "started_at"} <= set(meta)
        assert meta["pid"] == os.getpid()
        for f in (lock_file, sidecar):
            data = f.read_bytes()
            assert TOKEN.encode() not in data
            assert _digest(TOKEN).encode() not in data
        # The caller-supplied run_dir is not asserted on; moeka-created entries are.
        assert stat.S_IMODE(lock_file.parent.stat().st_mode) == 0o700
        for f in (lock_file, sidecar):
            assert stat.S_IMODE(f.stat().st_mode) & 0o077 == 0, oct(f.stat().st_mode)
    finally:
        lock.release()


@pytest.mark.fr("FR-041", "FR-042")
def test_token_lock_cross_process(h):
    from nanobot.channels.token_lock import acquire_channel_token_lock

    lock = acquire_channel_token_lock("telegram", TOKEN, run_dir=h.run_dir)
    out = h.aux / "try.json"
    r = h.worker("token_try", str(out), "telegram", TOKEN, str(h.run_dir))
    assert r.returncode == 0, r.stderr[-2000:]
    res = _read(out)
    assert res["ok"] is False and res["error"]["type"] == "ChannelTokenInUseError"
    assert isinstance(res["error"]["holder"], dict)
    assert res["error"]["holder"].get("pid") == os.getpid()
    assert TOKEN not in res["error"]["message"]
    # A different token and a different channel are independent.
    r = h.worker("token_try", str(out), "telegram", TOKEN + "x", str(h.run_dir))
    assert _read(out)["ok"] is True
    r = h.worker("token_try", str(out), "discord", TOKEN, str(h.run_dir))
    assert _read(out)["ok"] is True
    lock.release()
    r = h.worker("token_try", str(out), "telegram", TOKEN, str(h.run_dir))
    assert _read(out)["ok"] is True


@pytest.mark.fr("FR-041", "FR-042")
def test_token_lock_context_manager_and_in_process(h):
    from nanobot.channels.token_lock import ChannelTokenInUseError, acquire_channel_token_lock

    with acquire_channel_token_lock("discord", TOKEN, run_dir=h.run_dir):
        with pytest.raises(ChannelTokenInUseError) as info:
            acquire_channel_token_lock("discord", TOKEN, run_dir=h.run_dir)
        assert info.value.holder is None or isinstance(info.value.holder, dict)
    again = acquire_channel_token_lock("discord", TOKEN, run_dir=h.run_dir)
    again.release()


@pytest.mark.fr("FR-041")
def test_token_lock_released_on_sigkill(h):
    from nanobot.channels.token_lock import ChannelTokenInUseError, acquire_channel_token_lock

    ready = h.aux / "ready.json"
    p = h.python("token_hold", str(ready), "telegram", TOKEN, str(h.run_dir))
    assert wait_for(ready.exists, 30)
    with pytest.raises(ChannelTokenInUseError):
        acquire_channel_token_lock("telegram", TOKEN, run_dir=h.run_dir)
    os.kill(p.pid, signal.SIGKILL)
    p.wait(timeout=10)
    lock = acquire_channel_token_lock("telegram", TOKEN, run_dir=h.run_dir)
    lock.release()


@pytest.mark.fr("FR-041")
def test_run_dir_resolution(h, monkeypatch):
    from nanobot.channels.token_lock import acquire_channel_token_lock

    monkeypatch.setenv("MOEKA_RUN_DIR", str(h.aux / "envrun"))
    with acquire_channel_token_lock("telegram", TOKEN):
        assert _lock_paths(h.aux / "envrun", "telegram", TOKEN)[0].exists()
        # Spec Terms: the run dir is created with mode 0700 (here moeka creates it).
        assert stat.S_IMODE((h.aux / "envrun").stat().st_mode) == 0o700
    monkeypatch.delenv("MOEKA_RUN_DIR")
    xdg = h.aux / "xdg"
    xdg.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    with acquire_channel_token_lock("telegram", TOKEN):
        assert _lock_paths(xdg / "moeka", "telegram", TOKEN)[0].exists()
        assert stat.S_IMODE((xdg / "moeka").stat().st_mode) == 0o700


# -- FR-042 channel manager -----------------------------------------------------------------------


def _manager_config(root: Path, token: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    cfg = {
        "agents": {"defaults": {"workspace": str(root)}},
        "channels": {
            "telegram": {"enabled": True, "token": token, "allowFrom": ["1"]},
            "websocket": {"enabled": True, "unixSocketPath": str(root / "run" / "ws.sock")},
        },
    }
    p = root / "config.json"
    p.write_text(json.dumps(cfg))
    return p


async def _start_manager(cfg_path: Path, calls: list[str], seconds: float = 2.0):
    import workers

    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.loader import load_config

    workers.patch_channel_starts(calls)
    manager = ChannelManager(load_config(cfg_path), MessageBus(), config_path=cfg_path)
    task = asyncio.create_task(manager.start_all())
    await asyncio.sleep(seconds)
    return manager, task


async def _stop(manager, task):
    try:
        await asyncio.wait_for(manager.stop_all(), 10)
    finally:
        task.cancel()
        try:
            await task
        except BaseException:
            pass


@pytest.mark.fr("FR-042", "FR-041")
@pytest.mark.timeout(120)
async def test_second_manager_reports_locked_without_starting(h):
    w1 = h.aux / "inst1"
    w2 = h.aux / "inst2"
    cfg1 = _manager_config(w1, TOKEN)
    cfg2 = _manager_config(w2, TOKEN)
    ready = h.aux / "ready.json"
    p = h.python("manager_hold", str(ready), str(cfg1))
    lock_file, sidecar = _lock_paths(h.run_dir, "telegram", TOKEN)
    assert await asyncio.to_thread(wait_for, ready.exists, 60), "first manager did not start"
    assert lock_file.exists() and sidecar.exists()
    meta = _read(sidecar)
    assert meta["pid"] == p.pid
    assert Path(meta["workspace"]).resolve() == w1.resolve()
    calls: list[str] = []
    manager, task = await _start_manager(cfg2, calls)
    try:
        st = manager.get_status()
        assert st["telegram"]["state"] == "locked", st
        err = st["telegram"]["error"]
        assert err.startswith("channel_token_in_use:")
        assert str(w1) in err or str(w1.resolve()) in err
        assert str(p.pid) in err
        assert TOKEN not in err
        assert "TelegramChannel" not in calls, "a locked channel must not be started"
        assert "WebSocketChannel" in calls, "other channels must start normally"
        assert st["websocket"]["state"] == "running"
    finally:
        await _stop(manager, task)
        p.kill()


@pytest.mark.fr("FR-041")
@pytest.mark.timeout(120)
async def test_manager_takes_and_releases_lock(h):
    w1 = h.aux / "inst1"
    cfg1 = _manager_config(w1, TOKEN)
    calls: list[str] = []
    manager, task = await _start_manager(cfg1, calls)
    lock_file, sidecar = _lock_paths(h.run_dir, "telegram", TOKEN)
    try:
        assert "TelegramChannel" in calls
        assert lock_file.exists()
        meta = _read(sidecar)
        assert meta["pid"] == os.getpid()
        assert Path(meta["workspace"]).resolve() == w1.resolve()
        assert Path(meta["config"]).resolve() == cfg1.resolve()
    finally:
        await _stop(manager, task)
    out = h.aux / "try.json"
    r = await asyncio.to_thread(h.worker, "token_try", str(out), "telegram", TOKEN,
                                str(h.run_dir))
    assert _read(out)["ok"] is True, "lock not released when the channel stopped"


@pytest.mark.fr("FR-041")
@pytest.mark.timeout(120)
@pytest.mark.parametrize("token", ["${TELEGRAM_TOKEN}", ""])
async def test_unexpanded_or_empty_token_takes_no_lock(h, token):
    cfg = _manager_config(h.aux / "inst", token)
    calls: list[str] = []
    manager, task = await _start_manager(cfg, calls, seconds=1.5)
    try:
        locks = list((h.run_dir / "channel-locks").glob("telegram-*")) \
            if (h.run_dir / "channel-locks").exists() else []
        assert not locks
    finally:
        await _stop(manager, task)


# -- FR-043 Telegram Conflict -----------------------------------------------------------------------


def _tg(**cfg):
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.telegram.runtime import TelegramChannel

    base = {"enabled": True, "token": TOKEN, "allowFrom": ["1"]}
    base.update(cfg)
    return TelegramChannel(base, MessageBus())


class _Logs:
    def __init__(self):
        from loguru import logger

        self.records: list[tuple[str, str]] = []
        self._id = logger.add(lambda m: self.records.append(
            (m.record["level"].name, m.record["message"])), level="DEBUG")

    def close(self):
        from loguru import logger

        logger.remove(self._id)

    def errors(self, *needles):
        return [m for lvl, m in self.records if lvl in ("ERROR", "CRITICAL")
                and all(n in m for n in needles)]


@pytest.fixture
def logs():
    cap = _Logs()
    yield cap
    cap.close()


@pytest.mark.fr("FR-043")
async def test_conflict_sets_state_and_logs_once(h, logs):
    from telegram.error import Conflict

    ch = _tg()
    ch.handle_polling_error(Conflict("terminated by other getUpdates request"))
    assert ch.polling_state == "conflict"
    ch.handle_polling_error(Conflict("terminated by other getUpdates request"))
    assert ch.polling_state == "conflict"
    hits = logs.errors("Conflict", "another process is polling this bot token")
    assert len(hits) == 1, logs.records
    assert not [m for _, m in logs.records if TOKEN in m]


@pytest.mark.fr("FR-043")
async def test_other_errors_keep_handling(h, logs):
    from telegram.error import NetworkError

    ch = _tg()
    before = ch.polling_state
    ch.handle_polling_error(NetworkError("boom"))
    assert ch.polling_state != "conflict"
    assert ch.polling_state == before


@pytest.mark.fr("FR-043")
async def test_conflict_in_webhook_mode_logs_once_no_state_change(h, logs):
    from telegram.error import Conflict

    ch = _tg(mode="webhook", webhookUrl="https://example.invalid/telegram",
             webhookSecretToken="s3cret-s3cret")
    before = ch.polling_state
    ch.handle_polling_error(Conflict("conflict"))
    ch.handle_polling_error(Conflict("conflict"))
    assert ch.polling_state == before
    assert len([m for lvl, m in logs.records if "Conflict" in m]) == 1


@pytest.mark.fr("FR-043")
def test_conflict_retry_config(h):
    ch = _tg()
    assert ch.conflict_retry_s == 60
    assert _tg(conflictRetryS=5).conflict_retry_s == 5
    assert _tg(conflictRetryS=3600).conflict_retry_s == 3600
    for bad in (4, 3601, 0, -1):
        with pytest.raises(Exception) as info:
            _tg(conflictRetryS=bad)
        assert "conflict" in str(info.value).lower() or "retry" in str(info.value).lower()


@pytest.mark.fr("FR-043")
@pytest.mark.timeout(60)
async def test_manager_reports_conflict_state(h):
    from telegram.error import Conflict

    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.loader import load_config

    cfg = _manager_config(h.aux / "inst", TOKEN)
    manager = ChannelManager(load_config(cfg), MessageBus(), config_path=cfg)
    ch = manager.get_channel("telegram")
    assert ch is not None
    ch.handle_polling_error(Conflict("terminated by other getUpdates request"))
    assert manager.get_status()["telegram"]["state"] == "conflict"
