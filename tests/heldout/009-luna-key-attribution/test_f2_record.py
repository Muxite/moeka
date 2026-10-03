"""FR-002: ``LLMCallRecord.key_fp`` (validated) and ``_observe_llm_call`` filling it."""

from __future__ import annotations

import dataclasses

import pytest
from _h009 import KEY_AWORK, KEY_MOEKA, keyed_provider, one_call, record, ref_fp

from nanobot.llm_usage.models import LLMCallRecord

GOOD = ref_fp(KEY_MOEKA)


@pytest.mark.fr("FR-002")
def test_field_exists_optional_default_none():
    names = {f.name: f for f in dataclasses.fields(LLMCallRecord)}
    assert "key_fp" in names
    assert names["key_fp"].default is None
    assert record().key_fp is None


@pytest.mark.fr("FR-002")
@pytest.mark.parametrize("fp", [GOOD, "0123456789ab", "000000000000", "ffffffffffff"])
def test_valid_fingerprint_accepted(fp):
    assert record(key_fp=fp).key_fp == fp


@pytest.mark.fr("FR-002")
@pytest.mark.parametrize("bad", [
    "",
    " ",
    "0123456789a",          # 11
    "0123456789abc",        # 13
    "0123456789AB",         # upper-case
    "ABCDEF012345",
    "0123456789ag",         # non-hex
    "ghijklmnopqr",
    " 0123456789a",
    "0123456789ab ",
    "0123456789ab\n",
    "0123-56789ab",
    "0" * 64,               # full digest
    KEY_MOEKA,              # a key is not a fingerprint
    "０１２３４５６７８９ａｂ",  # full-width digits
])
def test_invalid_fingerprint_rejected(bad):
    with pytest.raises(ValueError):
        record(key_fp=bad)


@pytest.mark.fr("FR-002")
@pytest.mark.parametrize("bad", [123456789012, b"0123456789ab", 1.5])
def test_non_string_fingerprint_rejected(bad):
    assert "key_fp" in {f.name for f in dataclasses.fields(LLMCallRecord)}
    with pytest.raises((ValueError, TypeError)):
        record(key_fp=bad)


@pytest.mark.fr("FR-002")
def test_replace_revalidates():
    rec = record(key_fp=GOOD)
    with pytest.raises(ValueError):
        dataclasses.replace(rec, key_fp="NOT-A-FP")
    assert dataclasses.replace(rec, key_fp=None).key_fp is None


@pytest.mark.fr("FR-002")
def test_existing_validation_still_applies():
    with pytest.raises(ValueError):
        record(key_fp=GOOD, provider=" ")
    with pytest.raises(ValueError):
        record(key_fp=GOOD, cost_usd=-1.0)


@pytest.mark.fr("FR-002")
async def test_observe_sets_fp_from_provider():
    p = keyed_provider(KEY_MOEKA)
    seen: list[LLMCallRecord] = []
    p.set_llm_call_observer(seen.append)
    await one_call(p)
    assert len(seen) == 1
    assert seen[0].key_fp == ref_fp(KEY_MOEKA)


@pytest.mark.fr("FR-002")
async def test_observe_keyless_provider_is_none():
    p = keyed_provider(None)
    seen: list[LLMCallRecord] = []
    p.set_llm_call_observer(seen.append)
    await one_call(p)
    assert seen and seen[0].key_fp is None


@pytest.mark.fr("FR-002")
async def test_observe_whitespace_key():
    p = keyed_provider(f"  {KEY_AWORK}\n")
    seen: list[LLMCallRecord] = []
    p.set_llm_call_observer(seen.append)
    await one_call(p)
    assert seen[0].key_fp == ref_fp(KEY_AWORK)


@pytest.mark.fr("FR-002", "FR-001")
async def test_observe_follows_key_change_between_calls():
    p = keyed_provider(KEY_MOEKA)
    seen: list[LLMCallRecord] = []
    p.set_llm_call_observer(seen.append)
    await one_call(p)
    p.api_key = KEY_AWORK
    await one_call(p)
    p.api_key = ""
    await one_call(p)
    assert [r.key_fp for r in seen] == [ref_fp(KEY_MOEKA), ref_fp(KEY_AWORK), None]


@pytest.mark.fr("FR-002")
async def test_observe_streaming_path_sets_fp():
    p = keyed_provider(KEY_MOEKA)
    seen: list[LLMCallRecord] = []
    p.set_llm_call_observer(seen.append)
    stream = getattr(p, "chat_stream_with_retry", None)
    if stream is None:
        pytest.skip("no streaming retry path")
    await stream(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-6-luna")
    assert seen and all(r.key_fp == ref_fp(KEY_MOEKA) for r in seen)
