"""The canonical usage schemas: valid draft 2020-12, examples validate, fields only grow.

``FIELDS_V1`` is the snapshot: removing a name from a schema (or re-meaning it) without
a major bump fails here. Adding a field is allowed (extend the snapshot with it).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"

FIELDS_V1 = {
    "usage-record": {
        "schema_version", "record_id", "kind", "call_id", "attempt", "consumer", "agent",
        "session", "role", "purpose", "slot", "source", "tags", "trace_id", "parent_call_id",
        "request_key", "prompt_version", "started_at_ms", "latency_ms", "model", "provider",
        "alias", "tier", "tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write",
        "tokens_reasoning", "usage_source", "cost_usd", "cost_billed", "price_source",
        "cache_hit", "saved_tokens_in", "saved_tokens_out", "saved_cost_usd", "finish_reason",
        "outcome", "error_kind", "waste_label", "waste_set_by", "producer",
    },
    "budget-event": {
        "schema_version", "kind", "call_id", "ts_ms", "consumer", "agent", "session", "role",
        "purpose", "model", "provider", "alias", "tags", "scope", "cap_usd", "cap_tokens",
        "spent_usd", "spent_tokens", "reserved_usd", "reserved_tokens", "remaining_usd",
        "remaining_tokens", "worst_case_usd", "worst_case_tokens", "refusal", "producer",
    },
    "complete-json-call": {"schema_version", "request", "result"},
}
EXAMPLES = {
    "usage-record": ["usage-record.model-call.json", "usage-record.cache-hit.json"],
    "budget-event": ["budget-event.refuse.json", "budget-event.admit.json"],
    "complete-json-call": ["complete-json-call.json"],
}


def load(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.v1.schema.json").read_text())


@pytest.mark.parametrize("name", sorted(FIELDS_V1))
def test_schema_is_valid_2020_12(name: str) -> None:
    schema = load(name)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["$id"].endswith(f"{name}.v1.schema.json")


@pytest.mark.parametrize("name", sorted(FIELDS_V1))
def test_declared_fields_never_shrink(name: str) -> None:
    declared = set(load(name)["properties"])
    assert FIELDS_V1[name] <= declared, f"removed from {name}.v1: {FIELDS_V1[name] - declared}"


@pytest.mark.parametrize(("name", "example"), [(n, e) for n, es in EXAMPLES.items() for e in es])
def test_examples_validate(name: str, example: str) -> None:
    doc = json.loads((SCHEMAS / "examples" / example).read_text())
    jsonschema.Draft202012Validator(load(name)).validate(doc)


def test_cache_hit_must_bill_nothing() -> None:
    doc = json.loads((SCHEMAS / "examples" / "usage-record.cache-hit.json").read_text())
    doc["tokens_in"] = 5
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(load("usage-record")).validate(doc)


def test_refusal_needs_a_reason() -> None:
    doc = json.loads((SCHEMAS / "examples" / "budget-event.refuse.json").read_text())
    doc["refusal"] = None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(load("budget-event")).validate(doc)


def test_unknown_major_is_rejected() -> None:
    doc = json.loads((SCHEMAS / "examples" / "usage-record.model-call.json").read_text())
    doc["schema_version"] = "2.0"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(load("usage-record")).validate(doc)
