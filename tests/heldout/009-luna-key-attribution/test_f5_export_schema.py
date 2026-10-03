"""FR-005: ``usage-record.v1`` rows carry ``key_fp``; SCHEMA_VERSION 1.2; schema documents it."""

from __future__ import annotations

import importlib.util
import json

import pytest
from _h009 import KEY_AWORK, KEY_MOEKA, record, ref_fp, repo_root

from nanobot.llm_usage import query

FP1, FP2 = ref_fp(KEY_MOEKA), ref_fp(KEY_AWORK)
SCHEMAS = repo_root() / "schemas"


def _validate_mod():
    pytest.importorskip("jsonschema")
    spec = importlib.util.spec_from_file_location("h009_validate", SCHEMAS / "validate.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _schema():
    return json.loads((SCHEMAS / "usage-record.v1.schema.json").read_text(encoding="utf-8"))


def _example():
    return json.loads((SCHEMAS / "examples" / "usage-record.model-call.json").read_text())


def _ledgered(store, fp):
    from nanobot.kernel.ledger import LedgerObserver, ModelPricing

    obs = LedgerObserver(store=store, pricing={
        ("openrouter", "openai/gpt-6-luna"): ModelPricing("fast", 0.10, 0.50, None)})
    obs(record(key_fp=fp))


@pytest.mark.fr("FR-005")
def test_schema_version_is_1_2():
    assert query.SCHEMA_VERSION == "1.2"


@pytest.mark.fr("FR-005")
def test_row_has_key_fp_and_version(store):
    store.record(record(key_fp=FP1))
    store.record(record(key_fp=None))
    rows = [r for r in query.records(store) if r["kind"] == "model_call"]
    assert len(rows) == 2
    assert sorted((r["key_fp"] for r in rows), key=str) == sorted([FP1, None], key=str)
    assert all(r["schema_version"] == "1.2" for r in rows)


@pytest.mark.fr("FR-005")
def test_key_fp_is_string_or_null_in_json(store):
    store.record(record(key_fp=FP2))
    store.record(record())
    for r in query.records(store):
        if r["kind"] != "model_call":
            continue
        back = json.loads(json.dumps(r))
        assert "key_fp" in back
        assert back["key_fp"] is None or (isinstance(back["key_fp"], str)
                                          and len(back["key_fp"]) == 12)


@pytest.mark.fr("FR-005")
def test_totals_carry_new_version(store):
    store.record(record(key_fp=FP1))
    assert query.totals(store)[0].schema_version == "1.2"


@pytest.mark.fr("FR-005")
def test_cache_hit_rows_have_no_key(store):
    store.record_usage_event({
        "event": "cache.hit", "call_id": "hit-9", "consumer": "moeka",
        "model": "openai/gpt-6-luna", "provider": "openrouter", "saved_tokens_in": 1,
        "saved_tokens_out": 1, "saved_cost_usd": 0.0, "started_at_ms": record().started_at_ms,
    })
    hits = [r for r in query.records(store) if r["kind"] == "cache_hit"]
    assert hits
    assert all(r.get("key_fp") is None for r in hits)
    assert all(r["schema_version"] == "1.2" for r in hits)


@pytest.mark.fr("FR-005")
def test_schema_documents_key_fp():
    prop = _schema()["properties"].get("key_fp")
    assert prop is not None
    types = prop.get("type")
    assert sorted(types if isinstance(types, list) else [types]) == ["null", "string"]
    assert prop.get("pattern") == "^[0-9a-f]{12}$"
    assert "key_fp" not in _schema().get("required", [])


@pytest.mark.fr("FR-005")
def test_exported_rows_validate_strict(store):
    mod = _validate_mod()
    _ledgered(store, FP1)
    _ledgered(store, None)
    rows = [r for r in query.records(store) if r["kind"] == "model_call"]
    assert len(rows) == 2
    for r in rows:
        assert mod.problems(r, "usage-record") == []
        assert mod.problems(r, "usage-record", strict=False) == []


@pytest.mark.fr("FR-005")
def test_1_1_document_stays_valid():
    mod = _validate_mod()
    # Head amendment (round 1): the spec does not pin the shipped example's version, so build
    # the 1.1 document from it instead of assuming the example stays 1.1.
    doc = {k: v for k, v in _example().items() if k != "key_fp"}
    doc["schema_version"] = "1.1"
    assert mod.problems(doc, "usage-record") == []


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("fp", [FP1, "0123456789ab", None])
def test_1_2_document_with_key_fp_valid(fp):
    mod = _validate_mod()
    doc = dict(_example(), schema_version="1.2", key_fp=fp)
    assert mod.problems(doc, "usage-record") == []
    doc_no = {k: v for k, v in doc.items() if k != "key_fp"}
    assert mod.problems(doc_no, "usage-record") == []


@pytest.mark.fr("FR-005")
@pytest.mark.parametrize("bad", [
    "0123456789AB", "0123456789a", "0123456789abc", "zzzzzzzzzzzz", "", KEY_MOEKA, 123,
])
def test_bad_key_fp_rejected_by_schema(bad):
    mod = _validate_mod()
    doc = dict(_example(), schema_version="1.2", key_fp=bad)
    probs = mod.problems(doc, "usage-record")
    assert probs, f"key_fp={bad!r} accepted"
    assert any("key_fp" in p for p in probs)


@pytest.mark.fr("FR-005")
def test_bad_key_fp_rejected_by_reader_profile_too():
    """The lenient reader relaxes enums, not patterns."""
    mod = _validate_mod()
    doc = dict(_example(), schema_version="1.2", key_fp="NOTAFINGERPR")
    assert mod.problems(doc, "usage-record", strict=False)
