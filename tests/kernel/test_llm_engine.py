"""Kernel LLM layer: generate / complete / complete_json / stream on a FakeProvider."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from typing import Any

import pytest
from pydantic import BaseModel

from moeka.errors import (
    AuthError,
    ContentFilterError,
    LLMError,
    LLMTimeoutError,
    ModelNotFound,
    ParseError,
    QuotaError,
    RateLimitError,
    TransientError,
    TruncatedError,
    UnsupportedRequestError,
)
from moeka.llm import GenerateOptions, Sampling, assistant, image_part, system, user
from moeka.testing import FakeProvider, error, reply
from nanobot.kernel import solvers as solvers_mod
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel
from nanobot.providers.base import LLMUsage


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def model_calls(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("event") == "model.call"]


MAIN = ModelSpec(name="main", model="fake-main", provider="openai", price_in=1.0, price_out=2.0)


def _env(tmp_path, *, trace=None, state: str = "state", data_dir=None) -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / state,
        data_dir=data_dir,
        work_dir=tmp_path / "work",
        credentials={"oa": "sk-test"},
        providers=[
            ProviderSpec(name="openai", credential="oa"),
            ProviderSpec(name="vllm", api_base="http://127.0.0.1:9/v1"),
            ProviderSpec(name="anthropic"),
        ],
        models=[
            ModelSpec(name="main", model="gpt-4.1", provider="openai"),
            ModelSpec(name="local", model="qwen", provider="vllm", tier="local"),
            ModelSpec(name="claude", model="claude-x", provider="anthropic"),
        ],
        default_model="main",
        trace=trace,
    )


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def kernel(tmp_path, sink):
    k = Kernel(_env(tmp_path, trace=sink))
    yield k
    k.close()


def _fake(kernel: Kernel, *script: Any, spec: ModelSpec = MAIN, **kwargs: Any) -> FakeProvider:
    fake = FakeProvider(script, **kwargs)
    kernel.llm.register_provider(spec.name, fake, spec)
    return fake


# -- generate --------------------------------------------------------------------------


async def test_messages_go_out_byte_identical(kernel) -> None:
    fake = _fake(kernel, "ok")
    messages = [
        system("be brief"),
        {"role": "user", "content": "hi", "name": "alice"},
        assistant("hello"),
        user("look", images=["data:image/png;base64,AAAA"]),
        {"role": "user", "content": [{"type": "text", "text": ""}], "_meta": {"k": 1}},
    ]
    before = copy.deepcopy(messages)
    completion = await kernel.llm.generate(messages)
    assert completion.text == "ok"
    sent = fake.calls[0].messages
    assert json.dumps(sent, sort_keys=False) == json.dumps(before, sort_keys=False)
    assert messages == before  # the caller's list is untouched
    assert sent is not messages


def test_image_part_shapes(tmp_path) -> None:
    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG")
    assert image_part("https://x/y.png") == {
        "type": "image_url", "image_url": {"url": "https://x/y.png"},
    }
    assert image_part(png)["image_url"]["url"].startswith("data:image/png;base64,")
    assert image_part(b"raw")["image_url"]["url"].startswith("data:image/png;base64,")
    import sys

    import nanobot.api.complete  # noqa: F401

    legacy = sys.modules["nanobot.api.complete"]
    assert legacy._image_part is image_part
    assert user("t") == {"role": "user", "content": "t"}


def test_alias_resolution_and_model_not_found(kernel) -> None:
    llm = kernel.llm
    main = llm._route("main")
    assert (main.alias, main.provider.provider_name, main.model) == ("main", "openai", "gpt-4.1")
    local = llm._route("local")
    assert (local.provider.provider_name, local.model) == ("vllm", "qwen")
    assert llm._route(None).provider is main.provider  # one provider per alias, cached
    raw = llm._route("gpt-4o-mini")  # a raw id rides the default model's provider
    assert (raw.alias, raw.provider, raw.model) == (None, main.provider, "gpt-4o-mini")
    # A known alias whose provider has no credential is an auth problem, naming the ref.
    with pytest.raises(AuthError, match="providers/anthropic/api_key") as info:
        llm._route("claude")
    assert info.value.kind == "auth" and info.value.provider == "anthropic"
    with pytest.raises(AuthError):
        llm.generate_sync([user("hi")], GenerateOptions(model="claude"))


def test_kernel_close_releases_the_usage_store(tmp_path) -> None:
    from nanobot.llm_usage import get_llm_usage_store

    kernel = Kernel(_env(tmp_path))
    kernel.llm.register_provider(
        "main", FakeProvider(default="x"), ModelSpec(name="main", model="m", provider="openai"),
    )
    kernel.llm.generate_sync([user("hi")])
    store = get_llm_usage_store(data_dir=kernel.core_env.paths.data_dir)
    assert store._connection is not None  # the ledger recorded the call
    kernel.close()
    assert store._connection is None


def test_closing_one_kernel_keeps_a_shared_usage_store_open(tmp_path) -> None:
    from nanobot.llm_usage import get_llm_usage_store

    # One writer per state dir (spec 005): two state dirs sharing one data dir.
    shared = tmp_path / "data"
    first = Kernel(_env(tmp_path, state="state-a", data_dir=shared))
    second = Kernel(_env(tmp_path, state="state-b", data_dir=shared))
    try:
        for kernel in (first, second):
            kernel.llm.register_provider(
                "main", FakeProvider(default="x"),
                ModelSpec(name="main", model="m", provider="openai"),
            )
        second.llm.generate_sync([user("hi")])
        store = get_llm_usage_store(data_dir=second.core_env.paths.data_dir)
        connection = store._connection
        assert connection is not None
        first.close()
        assert store._connection is connection  # the other kernel still uses it
        second.close()
        assert store._connection is None  # the last kernel out closes it
    finally:
        first.close()
        second.close()


def test_missing_credential_names_the_host_ref(tmp_path) -> None:
    env = Environment.for_host(
        state_dir=tmp_path / "state", work_dir=tmp_path / "work",
        credentials={},
        providers=[ProviderSpec(name="openai", credential="OPENAI_KEY")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main",
    )
    with Kernel(env) as kernel:
        with pytest.raises(AuthError, match="'OPENAI_KEY'"):
            kernel.llm.generate_sync([user("hi")])


def test_unknown_alias_without_default_is_model_not_found(tmp_path, monkeypatch) -> None:
    with Kernel(_env(tmp_path)) as kernel:
        monkeypatch.setattr(kernel.llm, "_spec_for", lambda alias: None)
        with pytest.raises(ModelNotFound):
            kernel.llm._route("nope")


async def test_affinity_key_reaches_session_id(kernel) -> None:
    fake = _fake(kernel, "a", "b")
    await kernel.llm.generate([user("x")], GenerateOptions(affinity_key="conv-7"))
    await kernel.llm.generate([user("x")])
    assert fake.calls[0].provider_context.session_id == "conv-7"
    assert fake.calls[1].provider_context.session_id is None


async def test_deadline_cancels_a_slow_call(kernel) -> None:
    fake = _fake(kernel, "late", delay=10.0)
    started = time.monotonic()
    with pytest.raises(LLMTimeoutError) as info:
        await kernel.llm.generate([user("x")], GenerateOptions(timeout_s=0.2))
    assert time.monotonic() - started < 3.0
    assert isinstance(info.value, TimeoutError)
    assert info.value.call_id
    for _ in range(50):  # the cancel lands on the kernel loop
        if fake.calls[0].cancelled:
            break
        await asyncio.sleep(0.01)
    assert fake.calls[0].cancelled


@pytest.mark.parametrize(("attempts", "expected"), [(1, 1), (2, 2), (None, 4)])
async def test_attempts_cap(kernel, attempts, expected) -> None:
    fake = _fake(kernel, default=error(503, "server error"))
    with pytest.raises(TransientError):
        await kernel.llm.generate([user("x")], GenerateOptions(attempts=attempts))
    assert len(fake.calls) == expected


@pytest.mark.parametrize(
    ("response", "cls", "attrs"),
    [
        (error(401, "invalid api key"), AuthError, {}),
        (error(402, ""), QuotaError, {}),
        (error(429, "quota", error_type="insufficient_quota"), QuotaError, {}),
        (error(429, "slow down", retry_after=7.0), RateLimitError, {"retry_after": 7.0}),
        (error(503, "busy"), TransientError, {"retryable": True}),
        (error(400, "bad field"), LLMError, {"kind": "invalid_request"}),
        (reply("", finish_reason="content_filter"), ContentFilterError, {}),
    ],
)
async def test_errors_raise_typed(kernel, response, cls, attrs) -> None:
    _fake(kernel, response)
    with pytest.raises(cls) as info:
        await kernel.llm.generate([user("x")], GenerateOptions(attempts=1))
    exc = info.value
    assert exc.call_id and exc.provider == "fake" and exc.model == "fake-main"
    for name, value in attrs.items():
        assert getattr(exc, name) == value


async def test_on_unsupported_raise_reaches_caller(kernel) -> None:
    fake = FakeProvider(["never"])
    fake.supported_sampling_fields = frozenset({"temperature", "max_tokens"})
    kernel.llm.register_provider("main", fake, MAIN)
    with pytest.raises(UnsupportedRequestError) as info:
        await kernel.llm.generate(
            [user("x")], GenerateOptions(sampling=Sampling(seed=3), on_unsupported="raise"),
        )
    assert info.value.fields == ("seed",) and info.value.call_id
    assert fake.calls == []


async def test_usage_cost_and_ledger_attribution(kernel, sink) -> None:
    usage = LLMUsage.reported(input_tokens=1000, output_tokens=500, cache_read_tokens=0)
    _fake(kernel, error(503, "busy"), reply("hi", usage=usage))
    completion = await kernel.llm.generate(
        [user("x")], GenerateOptions(tags={"stage": "draft", "run": 3}),
    )
    assert completion.usage.input_tokens == 1000 and completion.usage.output_tokens == 500
    assert completion.usage.source == "reported"
    assert completion.cost_usd == pytest.approx((1000 * 1.0 + 500 * 2.0) / 1e6)
    assert (completion.model, completion.provider, completion.alias) == (
        "fake-main", "fake", "main",
    )
    assert completion.attempts == 2 and completion.latency_ms > 0
    assert completion.cached is False and completion.truncated is False

    events = sink.model_calls()
    assert [e["attempt"] for e in events] == [1, 2]
    assert [e["finish_reason"] for e in events] == ["error", "stop"]
    for event in events:
        assert event["call_id"] == completion.call_id
        assert event["alias"] == "main"
        assert event["tags"] == {"stage": "draft", "run": 3}
        assert event["cached"] is False
        assert event["provider"] == "fake"
    assert events[-1]["cost_usd"] == pytest.approx(completion.cost_usd)


async def test_truncated_flag(kernel) -> None:
    _fake(kernel, reply("cut sh", finish_reason="length"))
    completion = await kernel.llm.complete("x")
    assert completion.truncated and completion.text == "cut sh"


async def test_complete_builds_messages_and_sampling(kernel) -> None:
    spec = ModelSpec(
        name="main", model="fake-main", provider="openai", max_tokens=321,
        sampling=Sampling(temperature=0.3, top_p=0.9),
    )
    fake = _fake(kernel, "ok", spec=spec)
    await kernel.llm.complete(
        "q", system="sys", images=[b"img"], opts=GenerateOptions(sampling=Sampling(seed=5)),
    )
    call = fake.calls[0]
    assert call.messages[0] == {"role": "system", "content": "sys"}
    assert call.messages[1]["content"][0] == {"type": "text", "text": "q"}
    assert call.messages[1]["content"][1]["type"] == "image_url"
    # The model's core defaults become generation settings; extras ride the request.
    assert call.kwargs["temperature"] == 0.3 and call.kwargs["max_tokens"] == 321
    assert call.provider_context.request.sampling == Sampling(
        temperature=0.3, top_p=0.9, seed=5, max_tokens=321,
    )
    assert fake.generation.temperature != 0.3  # the provider object is not reconfigured


async def test_one_provider_under_two_aliases_keeps_each_specs_defaults_and_price(
    kernel, sink,
) -> None:
    fake = FakeProvider(default="ok")
    spec_a = ModelSpec(name="a", model="m-a", provider="host", price_in=1.0, price_out=1.0,
                       max_tokens=100, sampling=Sampling(temperature=0.1))
    spec_b = ModelSpec(name="b", model="m-b", provider="host", price_in=10.0, price_out=10.0,
                       sampling=Sampling(temperature=0.9))
    kernel.llm.register_provider("a", fake, spec_a)
    kernel.llm.register_provider("b", fake, spec_b)
    usage = LLMUsage.reported(input_tokens=1000, output_tokens=0)
    fake.push(reply("x", usage=usage), reply("y", usage=usage))
    ca = await kernel.llm.generate([user("q")], GenerateOptions(model="a"))
    cb = await kernel.llm.generate([user("q")], GenerateOptions(model="b"))
    assert fake.calls[0].kwargs["temperature"] == 0.1
    assert fake.calls[0].kwargs["max_tokens"] == 100
    assert fake.calls[0].kwargs["model"] == "m-a"
    assert fake.calls[1].kwargs["temperature"] == 0.9
    assert fake.calls[1].kwargs["model"] == "m-b"
    assert ca.cost_usd == pytest.approx(1000 * 1.0 / 1e6)
    assert cb.cost_usd == pytest.approx(1000 * 10.0 / 1e6)
    events = sink.model_calls()
    assert [(e["alias"], e["cost_usd"]) for e in events] == [
        ("a", pytest.approx(0.001)), ("b", pytest.approx(0.01)),
    ]


async def test_pool_provider_gets_spec_defaults_like_injected(tmp_path) -> None:
    from nanobot.providers.base import LLMResponse

    spec = ModelSpec(name="main", model="gpt-4.1", provider="openai", max_tokens=111,
                     sampling=Sampling(temperature=0.3, top_p=0.8))
    env = Environment.for_host(
        state_dir=tmp_path / "s", work_dir=tmp_path / "w", credentials={"oa": "sk"},
        providers=[ProviderSpec(name="openai", credential="oa")], models=[spec],
        default_model="main",
    )
    async with Kernel(env) as kernel:
        provider = kernel.llm._route("main").provider
        # The factory's preset path sets the same core defaults as generation settings.
        assert provider.generation.temperature == 0.3
        assert provider.generation.max_tokens == 111
        seen: list[dict[str, Any]] = []

        async def record(*, provider_context, **kwargs):
            seen.append({"context": provider_context, **kwargs})
            return LLMResponse(content="ok", usage=LLMUsage.reported(
                input_tokens=1, output_tokens=1))

        provider.chat_with_context = record
        await kernel.llm.generate([user("q")])
        await kernel.llm.generate([user("q")], GenerateOptions(
            sampling=Sampling(temperature=0.7, max_tokens=5)))
    assert seen[0]["temperature"] == 0.3 and seen[0]["max_tokens"] == 111
    assert seen[0]["context"].request.sampling.top_p == 0.8
    assert seen[1]["temperature"] == 0.7 and seen[1]["max_tokens"] == 5
    assert seen[1]["context"].request.sampling.top_p == 0.8


@pytest.mark.parametrize("mode", ["drop", "raise"])
async def test_unsupported_spec_defaults_drop_quietly(kernel, sink, mode) -> None:
    fake = FakeProvider(default="ok")
    fake.supported_sampling_fields = frozenset({"temperature", "max_tokens", "reasoning_effort"})
    spec = ModelSpec(name="main", model="fake-main", provider="openai",
                     sampling=Sampling(temperature=0.2, top_p=0.9, seed=1))
    kernel.llm.register_provider("main", fake, spec)
    await kernel.llm.generate([user("q")], GenerateOptions(on_unsupported=mode))
    assert not [e for e in sink.events if e.get("event") == "sampling.dropped"]
    sent = fake.calls[0].provider_context.request.sampling
    assert sent == Sampling(temperature=0.2)
    assert fake.calls[0].kwargs["temperature"] == 0.2

    explicit = GenerateOptions(sampling=Sampling(seed=4), on_unsupported=mode)
    if mode == "raise":
        with pytest.raises(UnsupportedRequestError) as info:
            await kernel.llm.generate([user("q")], explicit)
        assert info.value.fields == ("seed",)
        assert len(fake.calls) == 1
    else:
        await kernel.llm.generate([user("q")], explicit)
        dropped = [e for e in sink.events if e.get("event") == "sampling.dropped"]
        assert [e["fields"] for e in dropped] == [["seed"]]


def test_generate_options_hashable_and_validated() -> None:
    opts = GenerateOptions(tags={"a": [1]}, extra_body={"x": {"y": 1}},
                           response_format={"type": "json_object"})
    assert hash(opts) == hash(GenerateOptions(tags={"a": [1]}, extra_body={"x": {"y": 1}},
                                              response_format={"type": "json_object"}))
    with pytest.raises(ValueError):
        GenerateOptions(attempts=0)
    with pytest.raises(ValueError):
        GenerateOptions(timeout_s=0)


# -- complete_json -------------------------------------------------------------------------

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}


async def test_complete_json_native_one_round(kernel) -> None:
    fake = _fake(kernel, '{"a": 1}')
    completion = await kernel.llm.complete_json("give a", schema=SCHEMA)
    assert completion.parsed == {"a": 1} and completion.attempts == 1
    rf = fake.calls[0].provider_context.request.response_format
    assert rf["type"] == "json_schema" and rf["json_schema"]["schema"] == SCHEMA


async def test_complete_json_rejected_native_falls_back(kernel) -> None:
    fake = _fake(kernel, error(400, "response_format is not supported"), '{"a": 2}')
    completion = await kernel.llm.complete_json("give a", schema=SCHEMA)
    assert completion.parsed == {"a": 2} and completion.attempts == 2
    assert fake.calls[0].provider_context.request.response_format is not None
    assert fake.calls[1].provider_context.request.response_format is None
    assert "valid JSON" in fake.calls[1].messages[0]["content"]


async def test_complete_json_skips_native_when_model_says_so(kernel) -> None:
    spec = ModelSpec(name="main", model="fake-main", provider="openai", native_json=False)
    fake = _fake(kernel, '{"a": 3}', spec=spec)
    completion = await kernel.llm.complete_json("give a", schema=SCHEMA)
    assert completion.parsed == {"a": 3}
    assert fake.calls[0].provider_context.request.response_format is None


async def test_complete_json_reprompts_then_parse_error(kernel) -> None:
    fake = _fake(kernel, default="definitely not json")
    with pytest.raises(ParseError) as info:
        await kernel.llm.complete_json("give a", schema=SCHEMA, retries=2)
    assert info.value.attempts == 3 and info.value.raw == "definitely not json"
    assert len(fake.calls) == 3
    assert "Your previous reply was not valid" in fake.calls[1].messages[-1]["content"]


async def test_complete_json_reprompt_recovers_with_model_cls(kernel) -> None:
    class Thing(BaseModel):
        a: int

    _fake(kernel, '{"a": "nope"}', 'Sure: {"a": 4}')
    completion = await kernel.llm.complete_json("give a", model_cls=Thing)
    assert completion.parsed == Thing(a=4) and completion.attempts == 2
    assert completion.usage.input_tokens == 20  # summed over rounds


async def test_complete_json_truncated(kernel) -> None:
    _fake(kernel, reply('{"a": ', finish_reason="length"))
    with pytest.raises(TruncatedError) as info:
        await kernel.llm.complete_json("give a", schema=SCHEMA)
    assert info.value.raw == '{"a": '


async def test_complete_json_deadline_covers_all_rounds(kernel) -> None:
    # Each round (0.15s) fits the 0.35s deadline; three rounds together do not.
    fake = _fake(kernel, default="not json", delay=0.15)
    started = time.monotonic()
    with pytest.raises(LLMTimeoutError) as info:
        await kernel.llm.complete_json(
            "give a", schema=SCHEMA, retries=2, opts=GenerateOptions(timeout_s=0.35),
        )
    assert time.monotonic() - started < 0.6
    assert info.value.call_id
    assert len(fake.calls) == 3
    for _ in range(50):
        if fake.calls[-1].cancelled:
            break
        await asyncio.sleep(0.01)
    assert fake.calls[-1].cancelled


async def test_complete_json_deterministic_solver_makes_no_call(kernel, monkeypatch) -> None:
    registry = solvers_mod.SolverRegistry()
    monkeypatch.setattr(solvers_mod, "_DEFAULT_REGISTRY", registry)
    registry.register("sum", lambda p: solvers_mod.Solved({"a": p["x"] + 1}, "adder"))
    fake = _fake(kernel, '{"a": 0}')
    completion = await kernel.llm.complete_json(
        "sum", schema=SCHEMA, task_type="sum", task_payload={"x": 1},
    )
    assert completion.parsed == {"a": 2} and completion.attempts == 0
    assert completion.provider == "solver" and completion.model == "adder"
    assert fake.calls == []


# -- stream ------------------------------------------------------------------------------------


async def test_stream_deltas_and_completion(kernel, sink) -> None:
    _fake(kernel, "hello streaming world", chunk_size=5)
    async with kernel.llm.stream([user("x")], GenerateOptions(tags={"s": 1})) as stream:
        deltas = [d async for d in stream]
        completion = await stream.completion()
    assert len(deltas) > 1 and "".join(deltas) == "hello streaming world"
    assert completion.text == "hello streaming world"
    event = sink.model_calls()[-1]
    assert event["call_id"] == completion.call_id and event["tags"] == {"s": 1}


async def test_stream_error_is_typed(kernel) -> None:
    _fake(kernel, error(401, "bad key"))
    stream = kernel.llm.stream([user("x")], GenerateOptions(attempts=1))
    with pytest.raises(AuthError):
        await stream.completion()


async def test_stream_early_exit_cancels_the_call(kernel) -> None:
    fake = _fake(kernel, "a" * 200, chunk_size=1, delay=0.02)
    async with kernel.llm.stream([user("x")]) as stream:
        async for _ in stream:
            break
    for _ in range(100):
        if fake.calls[0].cancelled:
            break
        await asyncio.sleep(0.01)
    assert fake.calls[0].cancelled


async def test_abandoned_stream_is_finalised_without_gc(kernel) -> None:
    import gc

    fake = _fake(kernel, "c" * 400, chunk_size=1, delay=0.02)
    gc.disable()
    try:
        stream = kernel.llm.stream([user("x")])
        async for _ in stream:
            break
        del stream
        for _ in range(100):
            if fake.calls[0].cancelled:
                break
            await asyncio.sleep(0.01)
        assert fake.calls[0].cancelled
    finally:
        gc.enable()


def test_stream_sync_and_early_close(kernel) -> None:
    fake = _fake(kernel, "sync stream text", "b" * 200, chunk_size=4)
    with kernel.llm.stream_sync([user("x")]) as stream:
        assert "".join(stream) == "sync stream text"
        assert stream.completion().text == "sync stream text"
    fake.delay = 0.02
    with kernel.llm.stream_sync([user("x")]) as stream:
        next(stream)
    deadline = time.monotonic() + 2
    while not fake.calls[1].cancelled and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake.calls[1].cancelled


# -- sync twins and lifecycle -------------------------------------------------------------------


def test_sync_twins_from_a_thread_and_inside_a_running_loop(kernel) -> None:
    _fake(kernel, default='{"a": 9}')
    results: list[Any] = []

    def worker() -> None:
        results.append(kernel.llm.generate_sync([user("x")]).text)
        results.append(kernel.llm.complete_sync("x").text)
        results.append(kernel.llm.complete_json_sync("x", schema=SCHEMA).parsed)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(10)
    assert results == ['{"a": 9}', '{"a": 9}', {"a": 9}]

    async def inside() -> Any:
        sync_value = kernel.llm.complete_sync("x").text
        async_value = (await kernel.llm.complete_json("x", schema=SCHEMA)).parsed
        return sync_value, async_value

    assert asyncio.run(inside()) == ('{"a": 9}', {"a": 9})


async def test_register_provider_is_metered(kernel, sink) -> None:
    spec = ModelSpec(name="persona", model="p-1", provider="host", price_in=3.0, price_out=3.0)
    fake = FakeProvider(["x"], provider_name="host-llm")
    kernel.llm.register_provider("persona", fake, spec)
    usage = LLMUsage.reported(input_tokens=100, output_tokens=100)
    fake.push(reply("y", usage=usage))
    await kernel.llm.generate([user("a")], GenerateOptions(model="persona"))
    completion = await kernel.llm.generate([user("a")], GenerateOptions(model="persona"))
    assert completion.cost_usd == pytest.approx(600 / 1e6)
    events = [e for e in sink.model_calls() if e["alias"] == "persona"]
    assert len(events) == 2 and events[-1]["provider"] == "host-llm"
    assert events[-1]["cost_usd"] == pytest.approx(600 / 1e6)


async def test_kernel_aclose_closes_pool_providers(tmp_path, monkeypatch) -> None:
    built: list[FakeProvider] = []

    def fake_make_provider(config, *, preset_name=None, env=None, **_: Any) -> FakeProvider:
        provider = FakeProvider(default="pooled")
        built.append(provider)
        return provider

    monkeypatch.setattr("nanobot.providers.factory.make_provider", fake_make_provider)
    kernel = Kernel(_env(tmp_path))
    injected = FakeProvider(default="host")
    kernel.llm.register_provider("local", injected, ModelSpec(
        name="local", model="q", provider="vllm",
    ))
    assert (await kernel.llm.complete("x")).text == "pooled"
    assert (await kernel.llm.complete("x", opts=GenerateOptions(model="local"))).text == "host"
    await kernel.aclose()
    assert len(built) == 1 and built[0].closed
    assert not injected.closed  # host-injected providers stay host-owned
    with pytest.raises(RuntimeError, match="kernel is closed"):
        kernel.llm  # noqa: B018


async def test_fallback_candidates_keep_their_own_settings(tmp_path, monkeypatch) -> None:
    from dataclasses import replace
    from types import MappingProxyType

    from nanobot.providers.fallback_provider import FallbackProvider

    sink = RecordingSink()
    config = {
        "agents": {"defaults": {"model_preset": "main", "fallback_models": ["fb"]}},
        "providers": {"openai": {"api_key": "sk-test"}},
        "model_presets": {
            "main": {"model": "gpt-4.1", "provider": "openai", "max_tokens": 1000,
                     "temperature": 0.2, "reasoning_effort": "high"},
            "fb": {"model": "gpt-4.1-mini", "provider": "openai", "max_tokens": 50,
                   "temperature": 0.9},
        },
    }
    env = Environment.from_config(
        config, state_dir=tmp_path / "s", work_dir=tmp_path / "w", trace=sink,
    )
    # A non-core model default too, so a leak would show up as a sampling.dropped.
    models = dict(env.models)
    main = models["main"]
    models["main"] = replace(main, sampling=replace(main.sampling, top_p=0.5))
    monkeypatch.setattr(env, "_models", MappingProxyType(models))

    core_only = frozenset({"temperature", "max_tokens", "reasoning_effort"})
    primary = FakeProvider(default=error(503, "primary down"))
    primary.supported_sampling_fields = core_only
    candidate = FakeProvider(default="from fallback")
    candidate.supported_sampling_fields = core_only
    async with Kernel(env) as kernel:
        wrapper = kernel.llm._route("main").provider
        assert isinstance(wrapper, FallbackProvider)
        wrapper._primary = primary
        wrapper._provider_factory = lambda preset: candidate
        completion = await kernel.llm.generate([user("q")], GenerateOptions(attempts=1))

    assert completion.text == "from fallback"
    # The primary got the main spec's defaults (top_p quietly dropped)...
    assert primary.calls[0].kwargs["max_tokens"] == 1000
    assert primary.calls[0].kwargs["temperature"] == 0.2
    assert primary.calls[0].kwargs["reasoning_effort"] == "high"
    # ...the candidate kept its own preset: no primary defaults leaked in.
    sent = candidate.calls[0]
    assert sent.kwargs["model"] == "gpt-4.1-mini"
    assert sent.kwargs["max_tokens"] == 50
    assert sent.kwargs["temperature"] == 0.9
    assert sent.kwargs.get("reasoning_effort") is None
    assert sent.provider_context.request.sampling is None
    assert not [e for e in sink.events if e.get("event") == "sampling.dropped"]


# -- strict sampling (spec 006 K5) -----------------------------------------------------


@pytest.mark.parametrize("mode", ["drop", "raise"])
async def test_model_spec_unsupported_sampling_on_llm_calls(kernel, sink, mode) -> None:
    fake = FakeProvider(default="ok")  # declares every field
    spec = ModelSpec(name="main", model="fake-main", provider="openai",
                     unsupported_sampling=("seed",), sampling=Sampling(seed=9, top_p=0.5))
    kernel.llm.register_provider("main", fake, spec)
    # A host-declared default is dropped quietly under both modes.
    await kernel.llm.complete("q", opts=GenerateOptions(on_unsupported=mode))
    assert fake.calls[0].provider_context.request.sampling == Sampling(top_p=0.5)
    assert not [e for e in sink.events if e.get("event") == "sampling.dropped"]
    explicit = GenerateOptions(sampling=Sampling(seed=4, top_k=3), on_unsupported=mode)
    if mode == "raise":
        with pytest.raises(UnsupportedRequestError) as info:
            await kernel.llm.complete("q", opts=explicit)
        assert info.value.fields == ("seed",) and len(fake.calls) == 1
    else:
        await kernel.llm.complete("q", opts=explicit)
        sent = fake.calls[1].provider_context.request.sampling
        assert sent.seed is None and sent.top_k == 3
        dropped = [e for e in sink.events if e.get("event") == "sampling.dropped"]
        assert [e["fields"] for e in dropped] == [["seed"]]


async def test_pass_through_provider_fails_closed_under_raise_only(kernel, sink) -> None:
    fake = FakeProvider(default="ok")
    fake.supported_sampling_fields = None  # type: ignore[assignment]
    kernel.llm.register_provider("main", fake, MAIN)
    with pytest.raises(UnsupportedRequestError) as info:
        await kernel.llm.complete("q", opts=GenerateOptions(
            sampling=Sampling(temperature=0.1, seed=4), on_unsupported="raise",
        ))
    assert info.value.fields == ("temperature", "seed") and fake.calls == []
    await kernel.llm.complete("q", opts=GenerateOptions(sampling=Sampling(seed=4)))
    assert fake.calls[0].provider_context.request.sampling.seed == 4
    assert not [e for e in sink.events if e.get("event") == "sampling.dropped"]


async def test_strict_fallback_decides_on_the_primary(tmp_path) -> None:
    from nanobot.providers.fallback_provider import FallbackProvider

    sink = RecordingSink()
    config = {
        "agents": {"defaults": {"model_preset": "main", "fallback_models": ["fb"]}},
        "providers": {"openai": {"api_key": "sk-test"}},
        "model_presets": {
            "main": {"model": "gpt-4.1", "provider": "openai"},
            "fb": {"model": "gpt-4.1-mini", "provider": "openai"},
        },
    }
    env = Environment.from_config(
        config, state_dir=tmp_path / "s", work_dir=tmp_path / "w", trace=sink,
    )
    primary = FakeProvider(default=error(503, "primary down"))  # supports seed
    candidate = FakeProvider(default="from fallback")
    candidate.supported_sampling_fields = frozenset({"temperature", "max_tokens"})
    async with Kernel(env) as kernel:
        wrapper = kernel.llm._route("main").provider
        assert isinstance(wrapper, FallbackProvider)
        wrapper._primary = primary
        wrapper._provider_factory = lambda preset: candidate
        completion = await kernel.llm.complete("q", opts=GenerateOptions(
            attempts=1, sampling=Sampling(seed=5), on_unsupported="raise",
        ))
        assert completion.text == "from fallback"
        assert primary.calls[0].provider_context.request.sampling.seed == 5
        sent = candidate.calls[0].provider_context.request.sampling
        assert sent is None or sent.seed is None
        # The primary not supporting seed raises before anything is sent.
        primary.supported_sampling_fields = frozenset({"temperature", "max_tokens"})
        before = len(primary.calls), len(candidate.calls)
        with pytest.raises(UnsupportedRequestError):
            await kernel.llm.complete("q", opts=GenerateOptions(
                attempts=1, sampling=Sampling(seed=5), on_unsupported="raise",
            ))
        assert (len(primary.calls), len(candidate.calls)) == before
