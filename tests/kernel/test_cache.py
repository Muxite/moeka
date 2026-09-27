"""Response cache: lookup before admission, request keys, what gets stored."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from moeka.budget import CapBudget, ResponseCache
from moeka.llm import Completion, GenerateOptions, Request, Sampling, system, user
from moeka.testing import FakeProvider, reply
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def named(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("event") == name]


class DictCache:
    def __init__(self) -> None:
        self.data: dict[str, Completion] = {}
        self.gets = 0

    def get(self, key: str) -> Completion | None:
        self.gets += 1
        return self.data.get(key)

    def put(self, key: str, completion: Completion) -> None:
        self.data[key] = completion


MAIN = ModelSpec(name="main", model="fake-main", provider="openai", price_in=1.0, price_out=2.0)
OTHER = ModelSpec(name="other", model="fake-other", provider="openai")
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
def cache() -> DictCache:
    return DictCache()


@pytest.fixture
def kernel(tmp_path, sink, cache):
    k = Kernel(_env(tmp_path, sink), cache=cache)
    yield k
    k.close()


def _fake(kernel: Kernel, *script: Any, spec: ModelSpec = MAIN, **kwargs: Any) -> FakeProvider:
    fake = FakeProvider(script, **kwargs)
    kernel.llm.register_provider(spec.name, fake, spec)
    return fake


async def test_hit_makes_no_provider_call(kernel, sink, cache) -> None:
    assert isinstance(cache, ResponseCache)
    fake = _fake(kernel, "first", "second")
    messages = [system("s"), user("q")]
    first = await kernel.llm.generate(messages)
    assert first.text == "first" and not first.cached and len(cache.data) == 1

    again = await kernel.llm.generate(messages)
    assert again.text == "first" and again.cached
    assert len(fake.calls) == 1
    assert again.call_id and again.call_id != first.call_id
    assert again.cost_usd == 0.0 and again.attempts == 0
    [hit] = sink.named("cache.hit")
    assert hit["call_id"] == again.call_id
    assert hit["key"] == kernel.llm.request_key(Request(messages))[:16]
    assert len(sink.named("model.call")) == 1


async def test_hit_skips_budget_admission(tmp_path, sink, cache) -> None:
    async with Kernel(_env(tmp_path, sink), cache=cache) as warm:
        _fake(warm, "warm")
        await warm.llm.generate([user("q")])
    # A budget that refuses everything still lets a cached answer through.
    async with Kernel(_env(tmp_path, sink), cache=cache, budget=CapBudget(limit_usd=0.0)) as k:
        fake = _fake(k, "never")
        completion = await k.llm.generate([user("q")])
    assert completion.cached and completion.text == "warm" and fake.calls == []
    assert not sink.named("budget.admit") and not sink.named("budget.refuse")


async def test_opts_cache_false_bypasses(kernel, cache) -> None:
    fake = _fake(kernel, "a", "b")
    off = GenerateOptions(cache=False)
    assert (await kernel.llm.generate([user("q")], off)).text == "a"
    assert (await kernel.llm.generate([user("q")], off)).text == "b"
    assert cache.gets == 0 and cache.data == {} and len(fake.calls) == 2


async def test_truncated_is_not_cached(kernel, cache) -> None:
    fake = _fake(kernel, reply("cut", finish_reason="length"), "whole")
    assert (await kernel.llm.generate([user("q")])).truncated
    assert cache.data == {}
    assert (await kernel.llm.generate([user("q")])).text == "whole"
    assert len(fake.calls) == 2 and len(cache.data) == 1


async def test_json_hit_keeps_parsed(kernel) -> None:
    class Thing(BaseModel):
        a: int

    fake = _fake(kernel, '{"a": 5}')
    first = await kernel.llm.complete_json("give a", model_cls=Thing)
    again = await kernel.llm.complete_json("give a", model_cls=Thing)
    assert again.cached and again.parsed == Thing(a=5) == first.parsed
    assert len(fake.calls) == 1


async def test_stream_uses_the_cache(kernel) -> None:
    fake = _fake(kernel, "streamed text")
    async with kernel.llm.stream([user("q")]) as stream:
        first = await stream.completion()
    async with kernel.llm.stream([user("q")]) as stream:
        deltas = [d async for d in stream]
        again = await stream.completion()
    assert "".join(deltas) == "streamed text" == first.text
    assert again.cached and len(fake.calls) == 1
    # generate and stream share keys: the same request is the same answer.
    assert (await kernel.llm.generate([user("q")])).cached


def test_request_keys(kernel) -> None:
    _fake(kernel, default="x")
    _fake(kernel, default="x", spec=OTHER)
    key = kernel.llm.request_key
    msgs = [system("s"), user("q")]
    base = Request(msgs, schema=SCHEMA, retries=1)

    assert key(base) == key(Request(list(msgs), schema=dict(SCHEMA), retries=1))
    assert len(key(base)) == 64
    # The default model resolves the same as naming it.
    assert key(base) == key(Request(msgs, GenerateOptions(model="main"), SCHEMA, retries=1))
    # Tags, timeouts and affinity do not change the answer.
    same_answer = GenerateOptions(tags={"stage": "x"}, timeout_s=5, affinity_key="k")
    assert key(base) == key(Request(msgs, same_answer, SCHEMA, retries=1))

    other_schema = {"type": "object", "properties": {"b": {"type": "string"}}}
    different = [
        Request(msgs, schema=other_schema, retries=1),
        Request(msgs, GenerateOptions(model="other"), SCHEMA, retries=1),
        Request(msgs, GenerateOptions(sampling=Sampling(temperature=0.1)), SCHEMA, retries=1),
        Request(msgs, GenerateOptions(sampling=Sampling(seed=7)), SCHEMA, retries=1),
        Request(msgs, schema=SCHEMA, retries=2),
        Request(msgs),  # plain generate
        Request([system("s"), user("q?")], schema=SCHEMA, retries=1),
    ]
    keys = {key(r) for r in different}
    assert len(keys) == len(different) and key(base) not in keys

    class A(BaseModel):
        a: int

    class B(BaseModel):
        a: int

    assert key(Request(msgs, model_cls=A)) != key(Request(msgs, model_cls=B))


def test_request_key_includes_spec_sampling_defaults(tmp_path, sink) -> None:
    hot = ModelSpec(
        name="main", model="fake-main", provider="openai", sampling=Sampling(temperature=0.9),
    )
    cold = ModelSpec(
        name="main", model="fake-main", provider="openai", sampling=Sampling(temperature=0.1),
    )
    with Kernel(_env(tmp_path, sink)) as a, Kernel(_env(tmp_path, sink)) as b:
        _fake(a, default="x", spec=hot)
        _fake(b, default="x", spec=cold)
        request = Request([user("q")])
        assert a.llm.request_key(request) != b.llm.request_key(request)
        # An explicit value equal to the default is the same request.
        explicit = Request([user("q")], GenerateOptions(sampling=Sampling(temperature=0.9)))
        assert a.llm.request_key(request) == a.llm.request_key(explicit)


def test_deterministic_solver_is_not_cached(kernel, cache, monkeypatch) -> None:
    from nanobot.kernel import solvers as solvers_mod

    registry = solvers_mod.SolverRegistry()
    monkeypatch.setattr(solvers_mod, "_DEFAULT_REGISTRY", registry)
    registry.register("sum", lambda p: solvers_mod.Solved({"a": p["x"] + 1}, "adder"))
    _fake(kernel, default='{"a": 0}')
    completion = kernel.llm.complete_json_sync(
        "sum", schema=SCHEMA, task_type="sum", task_payload={"x": 1},
    )
    assert completion.parsed == {"a": 2} and not completion.cached
    assert cache.gets == 0 and cache.data == {}
