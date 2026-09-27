"""Budget admission, settlement and release around kernel LLM calls."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from moeka.budget import Budget, CallEstimate, CapBudget
from moeka.errors import BudgetExceeded
from moeka.llm import GenerateOptions, Request, Sampling, system, user
from moeka.testing import FakeProvider, error, reply
from nanobot.kernel.budget import IMAGE_TOKENS, REPROMPT_OVERHEAD_TOKENS, BudgetedProvider
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel
from nanobot.utils.helpers import estimate_prompt_tokens


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def named(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("event") == name]


class RecordingBudget:
    """Wraps a CapBudget and records every hook call."""

    def __init__(self, inner: CapBudget | None = None) -> None:
        self.inner = inner or CapBudget()
        self.admitted: list[CallEstimate] = []
        self.settled: list[tuple[Any, Any]] = []
        self.released: list[Any] = []

    def admit(self, estimate: CallEstimate) -> Any:
        reservation = self.inner.admit(estimate)
        self.admitted.append(estimate)
        return reservation

    def settle(self, reservation: Any, event: Any) -> None:
        self.settled.append((reservation, event))
        self.inner.settle(reservation, event)

    def release(self, reservation: Any) -> None:
        self.released.append(reservation)
        self.inner.release(reservation)


MAIN = ModelSpec(
    name="main", model="fake-main", provider="openai", price_in=1.0, price_out=2.0,
    max_tokens=100,
)
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}


def _env(tmp_path, sink) -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        credentials={"oa": "sk-test"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main",
        trace=sink,
    )


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def make_kernel(tmp_path, sink):
    kernels: list[Kernel] = []

    def make(budget: Any = None, **kwargs: Any) -> Kernel:
        kernel = Kernel(_env(tmp_path, sink), budget=budget, **kwargs)
        kernels.append(kernel)
        return kernel

    yield make
    for kernel in kernels:
        kernel.close()


def _fake(kernel: Kernel, *script: Any, spec: ModelSpec = MAIN, **kwargs: Any) -> FakeProvider:
    fake = FakeProvider(script, **kwargs)
    kernel.llm.register_provider(spec.name, fake, spec)
    return fake


def _worst(prompt: int, out: int, rounds: int = 1, reprompts: int = 0) -> tuple[int, float]:
    tokens_in = rounds * prompt + reprompts * (out + REPROMPT_OVERHEAD_TOKENS)
    tokens_out = rounds * out
    return tokens_in + tokens_out, (tokens_in * 1.0 + tokens_out * 2.0) / 1e6


# -- admission --------------------------------------------------------------------------


async def test_refusal_happens_before_any_provider_call(make_kernel, sink) -> None:
    kernel = make_kernel(CapBudget(limit_usd=1e-9))
    fake = _fake(kernel, "never")
    with pytest.raises(BudgetExceeded) as info:
        await kernel.llm.generate([user("hi")])
    assert fake.calls == []
    refused = sink.named("budget.refuse")
    assert len(refused) == 1 and refused[0]["call_id"] == info.value.call_id
    assert info.value.model == "fake-main"
    assert not sink.named("model.call") and not sink.named("budget.admit")


async def test_reservation_equals_the_estimate_worst_case(make_kernel, sink) -> None:
    budget = RecordingBudget()
    kernel = make_kernel(budget)
    seen: list[float] = []

    def answer(call: Any) -> Any:
        seen.append(budget.inner.reserved_usd)
        return "ok"

    _fake(kernel, answer)
    messages = [system("be brief"), user("hello there")]
    completion = await kernel.llm.generate(messages)

    [estimate] = budget.admitted
    prompt = estimate_prompt_tokens(messages)
    tokens, usd = _worst(prompt, 100)
    assert estimate.prompt_tokens == prompt and estimate.max_output_tokens == 100
    assert estimate.rounds == 1
    assert estimate.worst_case_tokens == tokens
    assert estimate.worst_case_usd == pytest.approx(usd)
    assert seen == [pytest.approx(usd)]  # reserved while the call was in flight
    assert estimate.call_id == completion.call_id
    # The public estimate is the same computation (fresh call id).
    public = kernel.llm.estimate(Request(messages))
    assert replace(public, call_id=estimate.call_id) == estimate
    admitted = sink.named("budget.admit")
    assert admitted[0]["call_id"] == completion.call_id
    assert admitted[0]["worst_case_usd"] == pytest.approx(usd)


async def test_settlement_from_ledger_events_releases_the_rest(make_kernel) -> None:
    budget = CapBudget(limit_usd=1.0)
    kernel = make_kernel(budget)
    _fake(kernel, reply("ok", input_tokens=30, output_tokens=5))
    completion = await kernel.llm.generate([user("hi")])
    assert completion.cost_usd == pytest.approx((30 * 1.0 + 5 * 2.0) / 1e6)
    assert budget.spent_usd == pytest.approx(completion.cost_usd)
    assert budget.spent_tokens == 35
    assert budget.reserved_usd == 0 and budget.reserved_tokens == 0
    assert budget.exposure() == pytest.approx(completion.cost_usd)


async def test_failed_call_releases_its_reservation(make_kernel) -> None:
    budget = CapBudget(limit_usd=1.0)
    kernel = make_kernel(budget)
    _fake(kernel, error(400, "bad request"))
    with pytest.raises(Exception):  # noqa: B017 - the typed error is tested elsewhere
        await kernel.llm.generate([user("hi")], GenerateOptions(attempts=1))
    assert budget.reserved_usd == 0 and budget.spent_usd == 0


async def test_explicit_max_tokens_and_images_shape_the_estimate(make_kernel) -> None:
    kernel = make_kernel(CapBudget())
    _fake(kernel, "ok")
    plain = kernel.llm.estimate(Request([user("look")]))
    with_image = kernel.llm.estimate(Request(
        [user("look", images=["data:image/png;base64,AAAA"])],
        GenerateOptions(sampling=Sampling(max_tokens=7)),
    ))
    assert with_image.max_output_tokens == 7
    assert with_image.prompt_tokens >= plain.prompt_tokens + IMAGE_TOKENS


async def test_per_tag_cap_applies_to_each_tag_value(make_kernel) -> None:
    _, one_call = _worst(estimate_prompt_tokens([user("q")]), 100)
    budget = CapBudget(per_tag={"stage": one_call * 1.5})
    kernel = make_kernel(budget)
    release = threading.Event()

    async def slow(call: Any) -> Any:
        while not release.is_set():
            await asyncio.sleep(0.005)
        return "slow"

    fake = _fake(kernel, slow, default="fast")

    def tagged(stage: str | None) -> GenerateOptions:
        return GenerateOptions(tags={"stage": stage} if stage else {})

    first = asyncio.create_task(kernel.llm.generate([user("q")], tagged("a")))
    while not fake.calls:
        await asyncio.sleep(0.005)
    # Stage "a" already holds one worst case: a second one would exceed 1.5x.
    with pytest.raises(BudgetExceeded, match="stage='a'"):
        await kernel.llm.generate([user("q")], tagged("a"))
    assert (await kernel.llm.generate([user("q")], tagged("b"))).text == "fast"
    assert (await kernel.llm.generate([user("q")], tagged(None))).text == "fast"
    release.set()
    assert (await first).text == "slow"
    assert budget.exposure("stage", "a") < one_call  # settled at the actual cost
    assert (await kernel.llm.generate([user("q")], tagged("a"))).text == "fast"


async def test_unpriced_models_need_allow_unpriced(make_kernel) -> None:
    unpriced = ModelSpec(name="main", model="free?", provider="openai", max_tokens=10)
    kernel = make_kernel(CapBudget(limit_usd=1.0))
    fake = _fake(kernel, default="ok", spec=unpriced)
    with pytest.raises(BudgetExceeded, match="no price"):
        await kernel.llm.generate([user("hi")])
    assert fake.calls == []

    lenient = CapBudget(limit_usd=1.0, allow_unpriced=True)
    kernel = make_kernel(lenient)
    _fake(kernel, default="ok", spec=unpriced)
    assert (await kernel.llm.generate([user("hi")])).text == "ok"
    # Tokens billed at an unknown price are never counted as free.
    assert lenient.spent_tokens > 0 and lenient.reserved_usd == 0

    local = ModelSpec(name="main", model="qwen", provider="openai", tier="local")
    kernel = make_kernel(CapBudget(limit_usd=1e-9))
    _fake(kernel, default="ok", spec=local)
    assert kernel.llm.estimate(Request([user("hi")])).worst_case_usd == 0.0
    assert (await kernel.llm.generate([user("hi")])).text == "ok"


def test_token_cap(make_kernel) -> None:
    kernel = make_kernel(CapBudget(limit_tokens=50))
    fake = _fake(kernel, default="ok")
    with pytest.raises(BudgetExceeded, match="limit_tokens"):
        kernel.llm.generate_sync([user("hi")])
    assert fake.calls == []


async def test_complete_json_worst_case_counts_every_round(make_kernel, sink) -> None:
    budget = RecordingBudget()
    kernel = make_kernel(budget)
    fake = _fake(kernel, "not json", "still not", '{"a": 1}')
    completion = await kernel.llm.complete_json("give a", schema=SCHEMA, retries=2)
    assert completion.parsed == {"a": 1} and completion.attempts == 3

    [estimate] = budget.admitted  # one admission for all three rounds
    first_round = fake.calls[0].messages
    prompt = estimate_prompt_tokens(first_round)
    tokens, usd = _worst(prompt, 100, rounds=4, reprompts=2)  # 1 + 2 retries + native
    assert estimate.rounds == 4
    assert estimate.prompt_tokens == prompt
    assert estimate.worst_case_tokens == tokens
    assert estimate.worst_case_usd == pytest.approx(usd)
    # Each physical round settled once against the one reservation.
    assert len(budget.settled) == 3
    assert {id(r) for r, _ in budget.settled} == {id(budget.released[0])}
    assert budget.inner.reserved_usd == 0

    # The schema is counted once (system suffix), not again for response_format.
    request = Request([user("give a")], schema=SCHEMA, retries=2)
    assert kernel.llm.estimate(request).prompt_tokens == prompt
    no_native = replace(MAIN, native_json=False)
    _fake(kernel, "x", spec=no_native)
    assert kernel.llm.estimate(request).rounds == 3
    assert kernel.llm.estimate(Request([user("hi")])).rounds == 1


# -- the provider wrapper --------------------------------------------------------------


async def test_budgeted_provider_admits_direct_calls_only(make_kernel, sink) -> None:
    budget = RecordingBudget()
    kernel = make_kernel(budget)
    fake = _fake(kernel, default="ok")
    await kernel.llm.generate([user("via llm")])
    assert len(budget.admitted) == 1  # the LLM layer's; the wrapper passed it through

    provider = kernel.llm._provider_for("main")
    assert isinstance(provider, BudgetedProvider) and provider.inner is fake
    assert provider.provider_name == "fake" and provider.get_default_model() == "fake-model"
    response = await provider.chat_with_retry(messages=[user("direct")], max_tokens=20)
    assert response.content == "ok"
    assert len(budget.admitted) == 2 and len(fake.calls) == 2
    direct = budget.admitted[1]
    assert direct.max_output_tokens == 20 and direct.alias == "main"
    event = [e for e in sink.named("model.call") if e["call_id"] == direct.call_id]
    assert len(event) == 1  # the direct call's ledger event is attributed and settled
    assert len(budget.settled) == 2 and budget.inner.reserved_usd == 0

    refusing = make_kernel(CapBudget(limit_usd=1e-9))
    fake2 = _fake(refusing, default="ok")
    with pytest.raises(BudgetExceeded):
        await refusing.llm._provider_for("main").chat_with_retry(messages=[user("x")])
    assert fake2.calls == []


def test_no_budget_means_no_wrapper(make_kernel) -> None:
    kernel = make_kernel()
    fake = _fake(kernel, default="ok")
    assert kernel.llm._provider_for("main") is fake


async def test_fallback_failover_settles_each_physical_attempt_once(make_kernel, sink) -> None:
    from nanobot.providers.fallback_provider import FallbackProvider

    budget = RecordingBudget(CapBudget(limit_usd=1.0))
    kernel = make_kernel(budget)
    primary = FakeProvider(default=error(503, "primary down"))
    candidate = FakeProvider(default=reply("from fallback", input_tokens=40, output_tokens=4))
    preset = SimpleNamespace(
        model="fake-main", max_tokens=50, temperature=0.5, context_window_tokens=None,
        reasoning_effort=None,
    )
    wrapper = FallbackProvider(primary, [preset], lambda p: candidate)
    kernel.llm.register_provider("main", wrapper, MAIN)

    completion = await kernel.llm.generate([user("q")], GenerateOptions(attempts=1))
    assert completion.text == "from fallback"
    assert len(primary.calls) == 1 and len(candidate.calls) == 1
    events = [e for e in sink.named("model.call") if e["call_id"] == completion.call_id]
    assert len(events) == 2
    assert len(budget.admitted) == 1
    assert len(budget.settled) == 2  # one per physical attempt, none for the wrapper
    assert sorted(e.attempt for _, e in budget.settled) == [1, 2]
    assert budget.inner.spent_usd == pytest.approx((40 * 1.0 + 4 * 2.0) / 1e6)
    assert budget.inner.spent_tokens == 44
    assert budget.inner.reserved_usd == 0


def test_kernel_validates_budget_and_cache(tmp_path, sink) -> None:
    env = _env(tmp_path, sink)
    with pytest.raises(TypeError, match="budget"):
        Kernel(env, budget=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="cache"):
        Kernel(env, cache=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_concurrency"):
        Kernel(env, max_concurrency=0)
    assert isinstance(CapBudget(), Budget)


async def test_stream_is_admitted_settled_and_refused(make_kernel, sink) -> None:
    budget = RecordingBudget(CapBudget(limit_usd=1.0))
    kernel = make_kernel(budget)
    _fake(kernel, reply("streamed", input_tokens=12, output_tokens=3))
    async with kernel.llm.stream([user("q")]) as stream:
        completion = await stream.completion()
    assert len(budget.admitted) == 1 and budget.admitted[0].call_id == completion.call_id
    assert len(budget.settled) == 1 and budget.inner.reserved_usd == 0
    assert budget.inner.spent_usd == pytest.approx(completion.cost_usd)

    refusing = make_kernel(CapBudget(limit_usd=1e-9))
    fake = _fake(refusing, "never")
    with pytest.raises(BudgetExceeded):
        async with refusing.llm.stream([user("q")]) as stream:
            await stream.completion()
    assert fake.calls == []
