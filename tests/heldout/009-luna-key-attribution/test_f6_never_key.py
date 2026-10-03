"""FR-006 / SC-002: the key never appears in rows, trace, exports, logs, errors or repr."""

from __future__ import annotations

import json

import pytest
from _h009 import (
    KEY_AWORK,
    KEY_MOEKA,
    keyed_provider,
    leaks,
    one_call,
    record,
    ref_fp,
    store_bytes,
)

from nanobot.llm_usage import query
from nanobot.llm_usage.models import LLMCallRecord

FP1 = ref_fp(KEY_MOEKA)


def _sink():
    from nanobot.kernel.trace import MemoryTraceSink

    return MemoryTraceSink()


async def _run_keyed_calls(store, sink):
    from nanobot.kernel.ledger import LedgerObserver, ModelPricing

    pricing = {("openrouter", "openai/gpt-6-luna"): ModelPricing("fast", 0.10, 0.50, None)}
    providers = [keyed_provider(KEY_MOEKA), keyed_provider(f"  {KEY_AWORK}\n")]
    for p in providers:
        p.set_llm_call_observer(LedgerObserver(sink=sink, store=store, pricing=pricing))
        await one_call(p)
    return providers


@pytest.mark.fr("FR-006", "SC-002")
async def test_store_files_never_hold_the_key(store, tmp_path):
    sink = _sink()
    await _run_keyed_calls(store, sink)
    rows = [r for r in query.records(store) if r["kind"] == "model_call"]
    assert {r["key_fp"] for r in rows} == {ref_fp(KEY_MOEKA), ref_fp(KEY_AWORK)}
    store.close()
    blob = store_bytes(store.path.parent)
    assert blob, "store wrote nothing"
    for key in (KEY_MOEKA, KEY_AWORK):
        assert leaks(blob, key) == []
    assert ref_fp(KEY_MOEKA).encode() in blob  # the fingerprint IS stored


@pytest.mark.fr("FR-006", "SC-002")
async def test_trace_events_never_hold_the_key(store):
    sink = _sink()
    await _run_keyed_calls(store, sink)
    assert sink.events, "no trace output"
    text = json.dumps(list(sink.events), default=str)
    for key in (KEY_MOEKA, KEY_AWORK):
        assert leaks(text, key) == []


@pytest.mark.fr("FR-006", "SC-002")
async def test_captured_logs_never_hold_the_key(store, log_lines):
    from loguru import logger

    sink = _sink()
    logger.debug("h009 log capture probe")
    await _run_keyed_calls(store, sink)
    assert any("h009 log capture probe" in line for line in log_lines)
    text = "\n".join(log_lines)
    for key in (KEY_MOEKA, KEY_AWORK):
        assert leaks(text, key) == []


@pytest.mark.fr("FR-006")
async def test_observer_failure_log_never_holds_the_key(log_lines):
    """A failing observer is logged (fail-open); the log line must not carry the key."""
    p = keyed_provider(KEY_MOEKA)

    def boom(rec: LLMCallRecord) -> None:
        raise RuntimeError(f"observer saw fp={rec.key_fp}")

    p.set_llm_call_observer(boom)
    resp = await one_call(p)
    assert resp.content == "ok"
    assert leaks("\n".join(log_lines), KEY_MOEKA) == []


@pytest.mark.fr("FR-006")
async def test_store_failure_log_never_holds_the_key(tmp_path, log_lines):
    """A store write that fails is logged by the ledger without the key."""
    from nanobot.kernel.ledger import LedgerObserver

    class _Broken:
        def record(self, call, event=None):
            raise RuntimeError(f"cannot write {call!r}")

        def record_event(self, call, event):
            raise RuntimeError(f"cannot write {call!r}")

    p = keyed_provider(KEY_MOEKA)
    p.set_llm_call_observer(LedgerObserver(store=_Broken(), pricing={}))
    await one_call(p)
    text = "\n".join(log_lines)
    assert "cannot write" in text  # the failure was logged ...
    assert leaks(text, KEY_MOEKA) == []  # ... without the key


@pytest.mark.fr("FR-006", "FR-005")
async def test_export_rows_never_hold_the_key(store):
    await _run_keyed_calls(store, None)
    docs = query.records(store, limit=50)
    text = json.dumps(docs, default=str)
    totals = json.dumps([t.to_dict() for t in query.totals(store, group_by=("key_fp",))],
                        default=str)
    for key in (KEY_MOEKA, KEY_AWORK):
        assert leaks(text, key) == []
        assert leaks(totals, key) == []


@pytest.mark.fr("FR-006", "FR-002")
def test_record_repr_and_str_show_fp_not_key():
    rec = record(key_fp=FP1)
    for text in (repr(rec), str(rec)):
        assert "key_fp" in text and FP1 in text
        assert leaks(text, KEY_MOEKA) == []


@pytest.mark.fr("FR-006", "FR-002")
async def test_observed_record_repr_has_no_key():
    p = keyed_provider(KEY_MOEKA)
    seen: list[LLMCallRecord] = []
    p.set_llm_call_observer(seen.append)
    await one_call(p)
    assert seen and FP1 in repr(seen[0])
    assert leaks(repr(seen[0]) + str(seen[0]), KEY_MOEKA) == []
    assert not any(isinstance(v, str) and leaks(v, KEY_MOEKA)
                   for v in vars_of(seen[0]).values())


def vars_of(rec) -> dict:
    import dataclasses

    return {f.name: getattr(rec, f.name) for f in dataclasses.fields(rec)}


@pytest.mark.fr("FR-006", "FR-002")
def test_validation_error_does_not_echo_a_key_passed_as_fp():
    with pytest.raises(ValueError) as info:
        record(key_fp=KEY_MOEKA)
    assert leaks(str(info.value), KEY_MOEKA) == []
    assert leaks(repr(info.value), KEY_MOEKA) == []


@pytest.mark.fr("FR-006")
def test_fp_is_not_a_key_substring():
    fp = keyed_provider(KEY_MOEKA).key_fp
    assert fp is not None and fp not in KEY_MOEKA
