"""LLM.batch: ordering, per-item failures, systemic aborts and rate-limit pauses."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from moeka.budget import CapBudget
from moeka.errors import AuthError, BudgetExceeded, LLMError, QuotaError, RateLimitError
from moeka.llm import BatchResult, Completion, GenerateOptions, Request, user
from moeka.testing import FakeCall, FakeProvider, error
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel

MAIN = ModelSpec(name="main", model="fake-main", provider="openai", price_in=1.0, price_out=2.0)
ONE_SHOT = GenerateOptions(attempts=1)  # no provider-level retries: the batch decides


def _env(tmp_path) -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        credentials={"oa": "sk-test"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main",
    )


@pytest.fixture
def kernel(tmp_path):
    k = Kernel(_env(tmp_path), max_concurrency=8)
    yield k
    k.close()


def _item(call: FakeCall) -> int:
    return int(call.messages[-1]["content"].split()[-1])


def _requests(n: int) -> list[Request]:
    return [Request([user(f"item {i}")], ONE_SHOT) for i in range(n)]


async def test_hundred_items_keep_input_order_with_per_item_failures(kernel) -> None:
    in_flight = 0
    peak = 0

    async def answer(call: FakeCall) -> Any:
        nonlocal in_flight, peak
        i = _item(call)
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep((i * 37 % 10) / 2000)  # finish out of order
        finally:
            in_flight -= 1
        if i % 7 == 0:
            return error(400, f"bad item {i}")
        return f"answer {i}"

    fake = FakeProvider(default=answer)
    kernel.llm.register_provider("main", fake, MAIN)
    result = await kernel.llm.batch(_requests(100), concurrency=5)

    assert isinstance(result, BatchResult) and result.systemic is None
    assert len(result.outcomes) == 100 and len(fake.calls) == 100
    assert 1 < peak <= 5
    for i, outcome in enumerate(result.outcomes):
        if i % 7 == 0:
            assert isinstance(outcome, LLMError) and outcome.kind == "invalid_request"
        else:
            assert isinstance(outcome, Completion) and outcome.text == f"answer {i}"
    assert len(result.errors) == 15 and len(result.completions) == 85


@pytest.mark.parametrize("fatal", [error(401, "invalid api key"), error(402, "out of credits")])
async def test_systemic_error_cancels_the_rest(kernel, fatal) -> None:
    cancelled: list[int] = []

    async def answer(call: FakeCall) -> Any:
        if _item(call) == 3:
            return fatal
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            cancelled.append(_item(call))
            raise
        return "late"

    fake = FakeProvider(default=answer)
    kernel.llm.register_provider("main", fake, MAIN)
    started = time.monotonic()
    result = await kernel.llm.batch(_requests(40), concurrency=4)

    assert time.monotonic() - started < 0.4  # nobody waited out the slow calls
    assert isinstance(result.systemic, (AuthError, QuotaError))
    assert all(outcome is result.systemic for outcome in result.outcomes)
    assert len(fake.calls) == 4  # items 4.. were never dispatched
    assert sorted(cancelled) == [0, 1, 2]  # in-flight calls were cancelled


async def test_budget_refusal_is_systemic(tmp_path) -> None:
    async with Kernel(_env(tmp_path), budget=CapBudget(limit_usd=1e-9)) as kernel:
        fake = FakeProvider(default="x")
        kernel.llm.register_provider("main", fake, MAIN)
        result = await kernel.llm.batch(_requests(5), concurrency=1)
    assert isinstance(result.systemic, BudgetExceeded)
    assert all(isinstance(o, BudgetExceeded) for o in result.outcomes)
    assert fake.calls == []


async def test_rate_limit_pauses_new_dispatches_then_succeeds(kernel) -> None:
    starts: dict[int, list[float]] = {}
    limited: list[float] = []

    async def answer(call: FakeCall) -> Any:
        i = _item(call)
        now = time.monotonic()
        starts.setdefault(i, []).append(now)
        if i == 0 and len(starts[i]) == 1:
            limited.append(now)
            return error(429, "rate limit exceeded", retry_after=0.2)
        await asyncio.sleep(0.02)
        return f"ok {i}"

    fake = FakeProvider(default=answer)
    kernel.llm.register_provider("main", fake, MAIN)
    result = await kernel.llm.batch(_requests(6), concurrency=1)

    assert result.systemic is None
    assert [o.text for o in result.outcomes] == [f"ok {i}" for i in range(6)]
    assert len(starts[0]) == 2  # retried once
    # Nothing was dispatched during the pause.
    later = [t for times in starts.values() for t in times if t > limited[0]]
    assert later and min(later) >= limited[0] + 0.19
    assert len(fake.calls) == 7


async def test_rate_limit_gives_up_after_three_retries(kernel) -> None:
    async def answer(call: FakeCall) -> Any:
        if _item(call) == 1:
            return error(429, "rate limit exceeded", retry_after=0.01)
        return "ok"

    fake = FakeProvider(default=answer)
    kernel.llm.register_provider("main", fake, MAIN)
    result = await kernel.llm.batch(_requests(3))
    assert result.systemic is None
    assert isinstance(result.outcomes[1], RateLimitError)
    assert [o.text for o in (result.outcomes[0], result.outcomes[2])] == ["ok", "ok"]
    assert sum(1 for c in fake.calls if _item(c) == 1) == 4


async def test_json_requests_in_a_batch(kernel) -> None:
    fake = FakeProvider(default=lambda call: '{"a": %d}' % _item(call))
    kernel.llm.register_provider("main", fake, MAIN)
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    requests = [Request([user(f"item {i}")], schema=schema) for i in range(5)]
    result = await kernel.llm.batch(requests)
    assert [o.parsed for o in result.outcomes] == [{"a": i} for i in range(5)]


def test_batch_sync_from_a_thread(kernel) -> None:
    kernel.llm.register_provider("main", FakeProvider(default="t"), MAIN)
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["result"] = kernel.llm.batch_sync(_requests(10), concurrency=3)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(10)
    assert "error" not in box
    assert [o.text for o in box["result"].outcomes] == ["t"] * 10


def test_batch_validates_inputs(kernel) -> None:
    kernel.llm.register_provider("main", FakeProvider(default="t"), MAIN)
    with pytest.raises(TypeError):
        kernel.llm.batch_sync([[user("not a request")]])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="concurrency"):
        kernel.llm.batch_sync(_requests(1), concurrency=0)
    assert kernel.llm.batch_sync([]).outcomes == []
