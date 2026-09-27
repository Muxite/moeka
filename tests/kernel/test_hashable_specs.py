"""Sampling and ProviderSpec are hashable (frozen mappings) and still read like dicts."""

from __future__ import annotations

import pytest

from nanobot.kernel.frozen import FrozenMap, freeze, thaw
from nanobot.kernel.hostenv import ModelSpec, ProviderSpec
from nanobot.kernel.sampling import Sampling


def test_sampling_with_logit_bias_hashes_by_value() -> None:
    a = Sampling(logit_bias={"50256": -100})
    b = Sampling(logit_bias={"50256": -100})
    assert hash(a) == hash(b)
    assert a == b
    assert len({a, b}) == 1
    assert a.logit_bias == {"50256": -100}
    assert dict(a.logit_bias) == {"50256": -100}
    assert Sampling(logit_bias={"1": 5}) != a


def test_sampling_logit_bias_is_read_only() -> None:
    source = {"7": 1.0}
    s = Sampling(logit_bias=source)
    source["8"] = 2.0
    assert s.logit_bias == {"7": 1.0}
    with pytest.raises(TypeError):
        s.logit_bias["9"] = 3.0  # type: ignore[index]


def test_sampling_set_fields() -> None:
    assert Sampling().set_fields() == ()
    assert Sampling(top_k=1, stop=["x"]).set_fields() == ("top_k", "stop")


def test_provider_spec_hashes_by_value_with_nested_body() -> None:
    body = {"a": 1, "chat_template_kwargs": {"enable_thinking": False}, "list": [1, {"b": 2}]}
    a = ProviderSpec(name="x", extra_body=body, extra_headers={"X-Team": "a"})
    b = ProviderSpec(name="x", extra_body=dict(body), extra_headers={"X-Team": "a"})
    assert hash(a) == hash(b)
    assert a == b
    assert hash(ProviderSpec(name="x", extra_body={"a": 1})) == hash(
        ProviderSpec(name="x", extra_body={"a": 1})
    )
    assert a.extra_body["chat_template_kwargs"] == {"enable_thinking": False}
    assert isinstance(a.extra_body, FrozenMap)
    assert a.extra_body.as_dict() == body  # lists thaw back to lists
    assert hash(ProviderSpec(name="x")) == hash(ProviderSpec(name="x"))


def test_model_spec_with_sampling_is_hashable() -> None:
    spec = ModelSpec(
        name="m", model="gpt", provider="x", sampling=Sampling(logit_bias={"1": 1.0}, stop=["a"]),
    )
    assert hash(spec) == hash(
        ModelSpec(name="m", model="gpt", provider="x",
                  sampling=Sampling(logit_bias={"1": 1.0}, stop=("a",)))
    )


def test_freeze_thaw_roundtrip() -> None:
    value = {"a": [1, {"b": (2, 3)}], "c": {"d": None}}
    frozen = freeze(value)
    assert hash(frozen) == hash(freeze(thaw(frozen)))
    assert thaw(frozen) == {"a": [1, {"b": [2, 3]}], "c": {"d": None}}
