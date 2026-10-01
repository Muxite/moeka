"""K5: strict sampling on ``AgentSpec`` (FR-028 to FR-034, SC-005, US3)."""

from __future__ import annotations

import dataclasses

import pytest
from _h006 import MAIN, ev, strip, tc

from moeka import ModelSpec, Sampling
from moeka.agents import AgentSpec
from moeka.errors import LLMError, UnsupportedRequestError
from moeka.llm import GenerateOptions
from moeka.testing import FakeProvider

NO_SEED = frozenset({"temperature", "max_tokens", "top_p", "stop", "reasoning_effort"})
ONLY_TEMP = frozenset({"temperature", "max_tokens"})


def _fake(script=("ok",), supported=NO_SEED) -> FakeProvider:
    fake = FakeProvider(list(script), default="ok")
    fake.supported_sampling_fields = supported
    return fake


def _agent(kmaker, fake, spec=None, **kw):
    kernel = kmaker(fake, spec=spec)
    return kernel.agent(AgentSpec(name=kw.pop("name", "k5"), **kw))


def _sent_sampling(call):
    ctx = getattr(call, "provider_context", None)
    request = getattr(ctx, "request", None)
    return getattr(request, "sampling", None)


def _sent_seed(call):
    s = _sent_sampling(call)
    return None if s is None else s.seed


# -- FR-028: the field ------------------------------------------------------------------------


@pytest.mark.fr("FR-028", "FR-041")
def test_field_default_and_values():
    assert AgentSpec(name="a").on_unsupported == "drop"
    assert AgentSpec(name="a", on_unsupported="raise").on_unsupported == "raise"
    assert "on_unsupported" in {f.name for f in dataclasses.fields(AgentSpec)}


@pytest.mark.fr("FR-028")
@pytest.mark.parametrize("bad", ["strict", "RAISE", "Drop", "", None, 1, True])
def test_invalid_values_raise(bad):
    with pytest.raises(ValueError):
        AgentSpec(name="a", on_unsupported=bad)


@pytest.mark.fr("FR-028")
def test_equality_and_hash(kmaker):
    a = AgentSpec(name="a")
    assert a == AgentSpec(name="a", on_unsupported="drop")
    assert hash(a) == hash(AgentSpec(name="a", on_unsupported="drop"))
    r = AgentSpec(name="a", on_unsupported="raise")
    assert a != r
    assert len({a, r, AgentSpec(name="a", on_unsupported="raise")}) == 2
    kernel = kmaker(_fake())
    assert kernel.agent(a) is not kernel.agent(r)
    assert kernel.agent(r) is kernel.agent(AgentSpec(name="a", on_unsupported="raise"))


# -- FR-030 / SC-005: raise -------------------------------------------------------------------


@pytest.mark.fr("FR-030", "SC-005")
def test_us3_raise_makes_zero_provider_calls(kmaker, sink):
    """US3 scenario 1."""
    fake = _fake()
    agent = _agent(kmaker, fake, on_unsupported="raise", sampling=Sampling(seed=7))
    result = agent.run_sync("go")
    assert fake.calls == []
    assert result.stop_reason == "error"
    assert isinstance(result.error, UnsupportedRequestError)
    assert isinstance(result.error, LLMError)
    assert result.error.fields == ("seed",)
    assert ev(sink, "sampling.dropped") == []
    done = ev(sink, "run.completed")
    assert len(done) == 1 and done[0]["stop_reason"] == "error"


@pytest.mark.fr("FR-030")
def test_fields_in_sampling_field_order(kmaker):
    fake = _fake(supported=ONLY_TEMP)
    sampling = Sampling(seed=7, top_k=5, temperature=0.3, presence_penalty=0.1, stop=("x",))
    agent = _agent(kmaker, fake, on_unsupported="raise", sampling=sampling)
    result = agent.run_sync("go")
    assert fake.calls == []
    order = [f.name for f in dataclasses.fields(Sampling)]
    expected = tuple(n for n in order if n in {"seed", "top_k", "presence_penalty", "stop"})
    assert result.error.fields == expected == ("top_k", "presence_penalty", "seed", "stop")


@pytest.mark.fr("FR-030")
def test_run_sampling_argument_is_explicit(kmaker, sink):
    fake = _fake()
    agent = _agent(kmaker, fake, on_unsupported="raise")
    result = agent.run_sync("go", sampling=Sampling(seed=11))
    assert fake.calls == [] and result.stop_reason == "error"
    assert isinstance(result.error, UnsupportedRequestError) and result.error.fields == ("seed",)
    # the run argument replaces the spec's sampling: a supported override runs
    agent2 = _agent(kmaker, _fake(["fine"]), name="k5b", on_unsupported="raise",
                    sampling=Sampling(seed=1))
    ok = agent2.run_sync("go", sampling=Sampling(temperature=0.5))
    assert ok.stop_reason == "completed" and ok.content == "fine"


@pytest.mark.fr("FR-030")
def test_supported_explicit_sampling_runs_under_raise(kmaker, sink):
    fake = _fake(["fine"])
    agent = _agent(kmaker, fake, on_unsupported="raise", sampling=Sampling(temperature=0.1))
    result = agent.run_sync("go")
    assert result.stop_reason == "completed" and len(fake.calls) == 1
    assert ev(sink, "sampling.dropped") == []


@pytest.mark.fr("FR-030")
def test_raise_stream_and_async(kmaker, sink):
    import asyncio

    fake = _fake()
    agent = _agent(kmaker, fake, on_unsupported="raise", sampling=Sampling(seed=7))
    with agent.stream_sync("go") as stream:
        events = list(stream)
        result = stream.result()
    assert result.stop_reason == "error" and isinstance(result.error, UnsupportedRequestError)
    assert str(getattr(events[-1].type, "value", events[-1].type)) == "run.failed"
    res2 = asyncio.run(asyncio.wait_for(agent.run("go"), 30))
    assert res2.stop_reason == "error" and isinstance(res2.error, UnsupportedRequestError)
    assert fake.calls == []


# -- FR-029 / SC-005: drop --------------------------------------------------------------------


@pytest.mark.fr("FR-029", "SC-005")
def test_us3_drop_sends_without_seed(kmaker, sink):
    """US3 scenario 2."""
    fake = _fake(["fine"])
    agent = _agent(kmaker, fake, on_unsupported="drop", sampling=Sampling(seed=7))
    result = agent.run_sync("go")
    assert result.stop_reason == "completed" and len(fake.calls) == 1
    dropped = ev(sink, "sampling.dropped")
    assert len(dropped) == 1 and list(dropped[0]["fields"]) == ["seed"]
    assert _sent_seed(fake.calls[0]) is None


@pytest.mark.fr("FR-029")
def test_default_is_drop_one_event_per_call(kmaker, sink):
    fake = _fake([tc("list_dir", {"path": "."}), "fine"])
    agent = _agent(kmaker, fake, sampling=Sampling(seed=7, temperature=0.2))
    result = agent.run_sync("go")
    assert result.stop_reason == "completed" and len(fake.calls) == 2
    dropped = ev(sink, "sampling.dropped")
    assert len(dropped) == 2 and all(list(e["fields"]) == ["seed"] for e in dropped)
    assert set(strip(dropped[0])) >= {"provider", "model", "fields"}


# -- FR-031: model defaults stay quiet ---------------------------------------------------------


@pytest.mark.fr("FR-031")
def test_model_defaults_dropped_quietly_under_raise(kmaker, sink):
    fake = _fake(["fine"])
    spec = dataclasses.replace(MAIN, sampling=Sampling(seed=3, temperature=0.4))
    agent = _agent(kmaker, fake, spec=spec, on_unsupported="raise")
    result = agent.run_sync("go")
    assert result.stop_reason == "completed" and len(fake.calls) == 1
    assert ev(sink, "sampling.dropped") == []
    assert _sent_seed(fake.calls[0]) is None


@pytest.mark.fr("FR-031", "FR-030")
def test_defaults_quiet_explicit_strict(kmaker, sink):
    fake = _fake(supported=ONLY_TEMP)
    spec = dataclasses.replace(MAIN, sampling=Sampling(top_k=3))
    agent = _agent(kmaker, fake, spec=spec, on_unsupported="raise", sampling=Sampling(seed=1))
    result = agent.run_sync("go")
    assert fake.calls == []
    assert result.error.fields == ("seed",)  # the default top_k is not listed


# -- FR-032: when a field is unsupported -------------------------------------------------------


@pytest.mark.fr("FR-032")
def test_passthrough_provider_fails_closed_under_raise(kmaker, sink):
    fake = _fake(supported=None)  # no support information at all (pass-through)
    agent = _agent(kmaker, fake, on_unsupported="raise", sampling=Sampling(seed=7))
    result = agent.run_sync("go")
    assert fake.calls == [] and result.stop_reason == "error"
    assert isinstance(result.error, UnsupportedRequestError) and result.error.fields == ("seed",)


@pytest.mark.fr("FR-032", "FR-029")
def test_passthrough_provider_sends_under_drop(kmaker, sink):
    fake = _fake(["fine"], supported=None)
    agent = _agent(kmaker, fake, on_unsupported="drop", sampling=Sampling(seed=7))
    result = agent.run_sync("go")
    assert result.stop_reason == "completed" and len(fake.calls) == 1
    assert _sent_seed(fake.calls[0]) == 7


@pytest.mark.fr("FR-032")
def test_passthrough_fails_closed_for_llm_complete_raise(kmaker, sink):
    fake = _fake(["fine"], supported=None)
    kernel = kmaker(fake)
    with pytest.raises(UnsupportedRequestError) as info:
        kernel.llm.complete_sync("q", opts=GenerateOptions(sampling=Sampling(seed=2),
                                                         on_unsupported="raise"))
    assert info.value.fields == ("seed",) and fake.calls == []


@pytest.mark.fr("FR-032")
def test_declared_support_is_respected(kmaker, sink):
    fake = _fake(["fine"], supported=frozenset({"seed", "temperature", "max_tokens"}))
    agent = _agent(kmaker, fake, on_unsupported="raise", sampling=Sampling(seed=7))
    result = agent.run_sync("go")
    assert result.stop_reason == "completed" and _sent_seed(fake.calls[0]) == 7


# -- FR-033: ModelSpec.unsupported_sampling -----------------------------------------------------


@pytest.mark.fr("FR-033", "FR-041")
def test_modelspec_field_and_validation():
    assert ModelSpec(name="m", model="x", provider="p").unsupported_sampling == ()
    spec = ModelSpec(name="m", model="x", provider="p", unsupported_sampling=("seed", "top_k"))
    assert spec.unsupported_sampling == ("seed", "top_k")
    hash(spec)
    for bad in (("sed",), ("seed", "nope"), ("extra_body",), ("",)):
        with pytest.raises(ValueError):
            ModelSpec(name="m", model="x", provider="p", unsupported_sampling=bad)


@pytest.mark.fr("FR-033", "FR-032")
def test_host_declared_unsupported_raise_agent(kmaker, sink):
    fake = _fake(supported=FakeProvider.supported_sampling_fields)  # provider claims seed
    spec = dataclasses.replace(MAIN, unsupported_sampling=("seed",))
    agent = _agent(kmaker, fake, spec=spec, on_unsupported="raise", sampling=Sampling(seed=7))
    result = agent.run_sync("go")
    assert fake.calls == [] and result.stop_reason == "error"
    assert isinstance(result.error, UnsupportedRequestError) and result.error.fields == ("seed",)
    assert ev(sink, "sampling.dropped") == []


@pytest.mark.fr("FR-033", "FR-029")
def test_host_declared_unsupported_drop_agent(kmaker, sink):
    fake = _fake(["fine"], supported=FakeProvider.supported_sampling_fields)
    spec = dataclasses.replace(MAIN, unsupported_sampling=("seed",))
    agent = _agent(kmaker, fake, spec=spec, sampling=Sampling(seed=7, temperature=0.3))
    result = agent.run_sync("go")
    assert result.stop_reason == "completed" and len(fake.calls) == 1
    dropped = ev(sink, "sampling.dropped")
    assert len(dropped) == 1 and list(dropped[0]["fields"]) == ["seed"]
    assert _sent_seed(fake.calls[0]) is None


@pytest.mark.fr("FR-033")
def test_host_declared_unsupported_llm_complete(kmaker, sink):
    fake = _fake(["one", "two"], supported=FakeProvider.supported_sampling_fields)
    spec = dataclasses.replace(MAIN, unsupported_sampling=("seed",))
    kernel = kmaker(fake, spec=spec)
    with pytest.raises(UnsupportedRequestError) as info:
        kernel.llm.complete_sync("q", opts=GenerateOptions(sampling=Sampling(seed=2),
                                                         on_unsupported="raise"))
    assert info.value.fields == ("seed",) and fake.calls == []
    out = kernel.llm.complete_sync("q", opts=GenerateOptions(sampling=Sampling(seed=2),
                                                           on_unsupported="drop"))
    assert len(fake.calls) == 1 and "one" in str(getattr(out, "text", out))
    dropped = ev(sink, "sampling.dropped")
    assert len(dropped) == 1 and list(dropped[0]["fields"]) == ["seed"]
    assert _sent_seed(fake.calls[0]) is None


@pytest.mark.fr("FR-033", "FR-031")
def test_host_declared_unsupported_default_is_quiet(kmaker, sink):
    fake = _fake(["fine"], supported=FakeProvider.supported_sampling_fields)
    spec = dataclasses.replace(MAIN, sampling=Sampling(seed=4), unsupported_sampling=("seed",))
    agent = _agent(kmaker, fake, spec=spec, on_unsupported="raise")
    result = agent.run_sync("go")
    assert result.stop_reason == "completed" and len(fake.calls) == 1
    assert ev(sink, "sampling.dropped") == []
    assert _sent_seed(fake.calls[0]) is None


# -- FR-034: nothing claims a seed was honoured ------------------------------------------------


@pytest.mark.fr("FR-034")
def test_no_honoured_event(kmaker, sink):
    from moeka.trace import EVENTS

    assert not any(("honour" in k or "honor" in k) for k in EVENTS)
    fake = _fake(["fine"], supported=FakeProvider.supported_sampling_fields)
    agent = _agent(kmaker, fake, on_unsupported="raise", sampling=Sampling(seed=7))
    agent.run_sync("go")
    for e in sink.events:
        text = repr(e).lower()
        assert "honour" not in text and "honored" not in text, e["event"]
