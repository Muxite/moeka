"""Worker processes for the multi-process held-out tests.

Usage: python workers.py <command> <args...>. Every command writes a JSON result to the
path given as its first argument (or a ready file), using public surfaces only.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import random
import socket
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _mi  # noqa: E402


def dump(path: str, obj: dict) -> None:
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def wait_file(path: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise TimeoutError(path)
        time.sleep(0.005)


def forever() -> None:
    while True:
        time.sleep(1)


def err_info(exc: BaseException) -> dict:
    holder = getattr(exc, "holder", None)
    state_dir = getattr(exc, "state_dir", None)
    return {
        "type": type(exc).__name__,
        "mro": [c.__name__ for c in type(exc).__mro__],
        "message": str(exc),
        "reason": getattr(exc, "reason", None),
        "reason_code": getattr(exc, "reason_code", None),
        "holder": holder if isinstance(holder, (dict, type(None))) else repr(holder),
        "state_dir": str(state_dir) if state_dir is not None else None,
    }


# -- locks --------------------------------------------------------------------------------


def cmd_hold_flock(lock_path: str, ready: str, sidecar: str = "") -> None:
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    if sidecar:
        dump(sidecar, {
            "pid": os.getpid(), "hostname": socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(), "argv0": sys.argv[0], "mode": "write",
        })
    dump(ready, {"pid": os.getpid()})
    forever()


def cmd_kernel_hold(state: str, work: str, ready: str, data_dir: str = "-") -> None:
    from moeka import Kernel

    env = _mi.make_env(state, work, data_dir=None if data_dir == "-" else data_dir)
    kernel = Kernel(env)
    dump(ready, {"pid": os.getpid()})
    forever()
    kernel.close()


def cmd_kernel_try(out: str, state: str, work: str, attach: str = "write") -> None:
    from moeka import Kernel

    env = _mi.make_env(state, work)
    t0 = time.monotonic()
    try:
        kernel = Kernel(env) if attach == "write" else Kernel(env, attach=attach)
    except BaseException as exc:  # noqa: BLE001
        dump(out, {"ok": False, "elapsed": time.monotonic() - t0, "error": err_info(exc),
                   "pid": os.getpid()})
        return
    kernel.close()
    dump(out, {"ok": True, "elapsed": time.monotonic() - t0, "pid": os.getpid()})


# -- shared budget ------------------------------------------------------------------------


def cmd_budget_stress(out: str, data_dir: str, budget_id: str, limit: str, n: str,
                      seed: str, go: str) -> None:
    rng = random.Random(int(seed))
    budget = _mi.shared_budget(data_dir, budget_id, limit_usd=float(limit))
    wait_file(go)
    res = {"admitted": 0, "refused": {}, "max_exposure": 0.0, "errors": [], "pid": os.getpid()}
    open_res = []
    for i in range(int(n)):
        usd = rng.choice([0.01, 0.013, 0.02, 0.0371, 0.05, 0.003])
        try:
            r = budget.admit(_mi.estimate(f"p{os.getpid()}-{i}", usd, tokens=rng.randint(10, 500)))
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "reason_code", type(exc).__name__)
            res["refused"][code] = res["refused"].get(code, 0) + 1
            r = None
        if r is not None:
            res["admitted"] += 1
            snap = budget.snapshot()
            res["max_exposure"] = max(res["max_exposure"],
                                      snap["spent_usd"] + snap["reserved_usd"])
            open_res.append((r, usd))
        # settle/release some of the open reservations in random order
        while open_res and (rng.random() < 0.6 or i == int(n) - 1):
            r, usd = open_res.pop(rng.randrange(len(open_res)))
            mode = rng.random()
            try:
                if mode < 0.7:
                    budget.settle(r, _mi.event(usd * rng.choice([1.0, 0.5, 0.0, 0.99])))
                elif mode < 0.8:
                    budget.settle(r, _mi.event(None, tokens_in=5, tokens_out=5))
                budget.release(r)
            except Exception as exc:  # noqa: BLE001
                res["errors"].append(repr(exc))
        time.sleep(rng.random() * 0.002)
    for r, _usd in open_res:
        try:
            budget.release(r)
        except Exception as exc:  # noqa: BLE001
            res["errors"].append(repr(exc))
    flush = getattr(budget, "flush", None)
    if callable(flush):
        flush()
    dump(out, res)


def cmd_budget_monitor(out: str, data_dir: str, budget_id: str, limit: str, stop: str,
                       ready: str) -> None:
    budget = _mi.shared_budget(data_dir, budget_id, limit_usd=float(limit))
    res = {"samples": 0, "max_exposure": 0.0, "max_spent": 0.0, "errors": []}
    dump(ready, {"pid": os.getpid()})
    while not os.path.exists(stop):
        try:
            snap = budget.snapshot()
        except Exception as exc:  # noqa: BLE001
            res["errors"].append(repr(exc))
            time.sleep(0.01)
            continue
        res["samples"] += 1
        res["max_exposure"] = max(res["max_exposure"], snap["spent_usd"] + snap["reserved_usd"])
        res["max_spent"] = max(res["max_spent"], snap["spent_usd"])
    dump(out, res)


def cmd_budget_fixed(out: str, data_dir: str, budget_id: str, limit: str, n: str, usd: str,
                     go: str) -> None:
    budget = _mi.shared_budget(data_dir, budget_id, limit_usd=float(limit))
    wait_file(go)
    res = {"admitted": 0, "refused": {}, "errors": []}
    amount = float(usd)
    for i in range(int(n)):
        try:
            r = budget.admit(_mi.estimate(f"f{os.getpid()}-{i}", amount))
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "reason_code", type(exc).__name__)
            res["refused"][code] = res["refused"].get(code, 0) + 1
            continue
        res["admitted"] += 1
        try:
            budget.settle(r, _mi.event(amount))
            budget.release(r)
        except Exception as exc:  # noqa: BLE001
            res["errors"].append(repr(exc))
    dump(out, res)


def cmd_budget_hang(out: str, data_dir: str, budget_id: str, limit: str, usd: str,
                    tokens: str, lease: str) -> None:
    budget = _mi.shared_budget(data_dir, budget_id, limit_usd=float(limit),
                               lease_s=float(lease))
    call_id = f"hang-{os.getpid()}"
    budget.admit(_mi.estimate(call_id, float(usd), tokens=int(tokens)))
    dump(out, {"pid": os.getpid(), "call_id": call_id, "t": time.time()})
    forever()


def cmd_budget_construct(out: str, data_dir: str, budget_id: str, limit: str) -> None:
    try:
        _mi.shared_budget(data_dir, budget_id, limit_usd=float(limit))
    except BaseException as exc:  # noqa: BLE001
        dump(out, {"ok": False, "error": err_info(exc)})
        return
    dump(out, {"ok": True})


def cmd_kernel_budget_calls(out: str, state: str, work: str, data_dir: str, budget_id: str,
                            limit: str, n: str, go: str) -> None:
    from moeka import Kernel

    budget = _mi.shared_budget(data_dir, budget_id, limit_usd=float(limit))
    kernel = Kernel(_mi.make_env(state, work, data_dir=data_dir), budget=budget)
    _mi.attach_fake(kernel)
    wait_file(go)
    res = {"ok": 0, "refused": {}, "errors": []}
    for _ in range(int(n)):
        try:
            kernel.llm.complete_sync("hello")
            res["ok"] += 1
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "reason_code", None)
            if code is None:
                res["errors"].append(repr(exc))
            else:
                res["refused"][code] = res["refused"].get(code, 0) + 1
    kernel.close()
    dump(out, res)


# -- usage store --------------------------------------------------------------------------


def cmd_usage_worker(out: str, data_dir: str, state: str, work: str, consumer: str, n: str,
                     go: str, p1done: str = "", go2: str = "", n2: str = "0",
                     closing: str = "") -> None:
    from moeka import Kernel

    kernel = Kernel(_mi.make_env(state, work, data_dir=data_dir), consumer=consumer)
    _mi.attach_fake(kernel)
    res: dict = {"errors": [], "pid": os.getpid()}
    wait_file(go)

    def call(_i: int) -> None:
        try:
            kernel.llm.complete_sync("hello")
        except Exception as exc:  # noqa: BLE001
            res["errors"].append(repr(exc))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(call, range(int(n))))
    if p1done:
        dump(p1done, {"pid": os.getpid()})
        wait_file(go2)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(call, range(int(n2))))
    res["loss_before_close"] = dict(kernel.usage.loss())
    if closing:
        dump(closing, {"pid": os.getpid()})
    t0 = time.monotonic()
    kernel.close()
    res["close_s"] = time.monotonic() - t0
    dump(out, res)


# -- channel token lock -------------------------------------------------------------------


def cmd_token_try(out: str, channel: str, token: str, run_dir: str) -> None:
    from nanobot.channels.token_lock import acquire_channel_token_lock

    try:
        lock = acquire_channel_token_lock(channel, token, run_dir=Path(run_dir))
    except BaseException as exc:  # noqa: BLE001
        dump(out, {"ok": False, "error": err_info(exc)})
        return
    lock.release()
    dump(out, {"ok": True})


def cmd_token_hold(ready: str, channel: str, token: str, run_dir: str) -> None:
    from nanobot.channels.token_lock import acquire_channel_token_lock

    lock = acquire_channel_token_lock(channel, token, run_dir=Path(run_dir))
    dump(ready, {"pid": os.getpid()})
    forever()
    lock.release()


def patch_channel_starts(calls: list[str]) -> None:
    """Replace network-touching channel start/stop with recorders (no network)."""
    from nanobot.channels.telegram.runtime import TelegramChannel
    from nanobot.channels.websocket.runtime import WebSocketChannel

    async def fake_start(self):
        calls.append(type(self).__name__)
        self._running = True
        while self._running:
            await asyncio.sleep(0.05)

    async def fake_stop(self):
        self._running = False

    for cls in (TelegramChannel, WebSocketChannel):
        cls.start = fake_start  # type: ignore[method-assign]
        cls.stop = fake_stop  # type: ignore[method-assign]


def cmd_manager_hold(ready: str, config_path: str) -> None:
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.loader import load_config

    calls: list[str] = []
    patch_channel_starts(calls)
    cfg = load_config(Path(config_path))

    async def main() -> None:
        manager = ChannelManager(cfg, MessageBus(), config_path=Path(config_path))
        task = asyncio.create_task(manager.start_all())
        for _ in range(400):
            await asyncio.sleep(0.05)
            if "TelegramChannel" in calls:
                break
        dump(ready, {"pid": os.getpid(), "calls": calls, "status": manager.get_status()})
        await task

    asyncio.run(main())


COMMANDS = {name[4:]: fn for name, fn in globals().items() if name.startswith("cmd_")}

if __name__ == "__main__":
    try:
        COMMANDS[sys.argv[1]](*sys.argv[2:])
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        sys.exit(70)
