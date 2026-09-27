"""Kernel skeleton: env/trace wiring, bridge ownership and idempotent close."""

from __future__ import annotations

import asyncio

import pytest

from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel
from nanobot.kernel.trace import NullTraceSink


class _Trace:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


def _env(tmp_path, trace=None) -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        credentials={},
        providers=[ProviderSpec(name="vllm", api_base="http://127.0.0.1:8000/v1")],
        models=[ModelSpec(name="m", model="qwen", provider="vllm")],
        default_model="m",
        trace=trace,
    )


async def _answer() -> int:
    await asyncio.sleep(0)
    return 42


def test_env_and_trace(tmp_path) -> None:
    trace = _Trace()
    env = _env(tmp_path, trace)
    with Kernel(env) as kernel:
        assert kernel.env is env
        assert kernel.trace is trace
    assert isinstance(Kernel(_env(tmp_path)).trace, NullTraceSink)


def test_rejects_non_environment(tmp_path) -> None:
    with pytest.raises(TypeError, match="Environment"):
        Kernel(_env(tmp_path).core)  # type: ignore[arg-type]


def test_sync_context_manager_stops_bridge(tmp_path) -> None:
    with Kernel(_env(tmp_path)) as kernel:
        assert kernel._bridge.run(_answer(), timeout=5) == 42
        thread = kernel._bridge._thread
        assert thread is not None and thread.is_alive()
    assert kernel.closed
    assert not thread.is_alive()
    with pytest.raises(RuntimeError, match="bridge stopped"):
        kernel._bridge.run(_answer(), timeout=5)


async def test_async_context_manager_stops_bridge(tmp_path) -> None:
    async with Kernel(_env(tmp_path)) as kernel:
        assert await asyncio.wrap_future(kernel._bridge.submit(_answer())) == 42
        thread = kernel._bridge._thread
    assert kernel.closed
    assert not thread.is_alive()


async def test_close_is_idempotent(tmp_path) -> None:
    kernel = Kernel(_env(tmp_path))
    kernel._bridge.start()
    kernel.close()
    kernel.close()
    await kernel.aclose()
    assert kernel.closed

    never_started = Kernel(_env(tmp_path))
    await never_started.aclose()
    await never_started.aclose()
    never_started.close()
    assert never_started._bridge._thread is None


def test_exception_in_with_block_still_closes(tmp_path) -> None:
    with pytest.raises(ValueError):
        with Kernel(_env(tmp_path)) as kernel:
            kernel._bridge.start()
            raise ValueError("boom")
    assert kernel.closed
    assert not kernel._bridge._thread.is_alive()
