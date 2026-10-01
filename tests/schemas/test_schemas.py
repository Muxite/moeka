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
        "request_key", "key_scheme", "prompt_version", "started_at_ms", "latency_ms", "model", "provider",
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
    "waste-label": {
        "schema_version", "call_id", "attempt", "waste_label", "waste_set_by", "started_at_ms",
        "consumer", "agent", "session", "role", "purpose", "producer",
    },
}
EXAMPLES = {
    "usage-record": ["usage-record.model-call.json", "usage-record.cache-hit.json"],
    "budget-event": ["budget-event.refuse.json", "budget-event.admit.json"],
    "complete-json-call": ["complete-json-call.json"],
    "waste-label": ["waste-label.retry.json"],
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


# -- evolution is enforced, not just named ---------------------------------------------------------
# The snapshot above only notices a removed field name. ``breaking_changes`` compares a schema
# with the frozen copy of its first released version and flags what a reader of the old
# version would trip on: a removed property, a required field added, a type or enum narrowed,
# a bound tightened, ``additionalProperties`` closed, a changed conditional rule.

FROZEN = Path(__file__).parent / "frozen"
_BOUNDS_UP = ("minimum", "minLength", "minItems", "exclusiveMinimum")  # raising tightens
_BOUNDS_DOWN = ("maximum", "maxLength", "maxItems", "exclusiveMaximum", "maxProperties")


def _types(node: dict) -> set[str] | None:
    t = node.get("type")
    return None if t is None else set([t] if isinstance(t, str) else t)


def breaking_changes(old: dict, new: dict, path: str = "$") -> list[str]:
    out: list[str] = []
    if "const" in old and old.get("const") != new.get("const"):
        out.append(f"{path}: const changed")
    if _types(old) is not None:
        nt = _types(new)
        if nt is None or not _types(old) <= nt:
            out.append(f"{path}: type narrowed {_types(old)} -> {nt}")
    if "enum" in old and ("enum" in new and not set(map(repr, old["enum"])) <= set(
            map(repr, new["enum"]))):
        out.append(f"{path}: enum value removed")
    if "enum" not in old and "enum" in new:
        out.append(f"{path}: enum added")
    if "enum" in old and "enum" not in new and "pattern" not in new and not (
            "type" in new and "type" in old and _types(old) <= _types(new)):
        out.append(f"{path}: enum removed without a replacement rule")
    for k in _BOUNDS_UP:
        if k in new and (k not in old or new[k] > old[k]):
            out.append(f"{path}: {k} tightened")
    for k in _BOUNDS_DOWN:
        if k in new and (k not in old or new[k] < old[k]):
            out.append(f"{path}: {k} tightened")
    if old.get("pattern") != new.get("pattern"):
        import re

        # an enum relaxed to a pattern that still accepts every old value is a widening
        relaxed = "pattern" not in old and "enum" in old and "enum" not in new and all(
            isinstance(v, str) and re.search(new["pattern"], v) for v in old["enum"])
        if not relaxed:
            out.append(f"{path}: pattern changed")
    if "required" in new and not set(new["required"]) <= set(old.get("required", [])):
        out.append(f"{path}: required grew {set(new['required']) - set(old.get('required', []))}")
    if old.get("additionalProperties", True) is True and new.get("additionalProperties", True) is not True:
        out.append(f"{path}: additionalProperties closed")
    if old.get("allOf") != new.get("allOf"):
        out.append(f"{path}: conditional rules changed")
    for name, sub in old.get("properties", {}).items():
        got = new.get("properties", {}).get(name)
        if got is None:
            out.append(f"{path}.{name}: removed")
        else:
            out.extend(breaking_changes(sub, got, f"{path}.{name}"))
    for name, sub in old.get("$defs", {}).items():
        got = new.get("$defs", {}).get(name)
        if got is None:
            out.append(f"{path}.$defs.{name}: removed")
        else:
            out.extend(breaking_changes(sub, got, f"{path}.$defs.{name}"))
    return out


@pytest.mark.parametrize("name", ["usage-record", "budget-event", "complete-json-call"])
def test_current_schema_is_a_compatible_evolution_of_1_0(name: str) -> None:
    old = json.loads((FROZEN / f"{name}.1.0.schema.json").read_text())
    assert breaking_changes(old, load(name)) == []


def test_breaking_change_detector_detects() -> None:
    import copy

    old = load("usage-record")
    cases = {
        "removed property": lambda d: d["properties"].pop("tokens_in"),
        "required grew": lambda d: d["required"].append("tags"),
        "enum shrunk": lambda d: d["properties"]["outcome"]["enum"].remove("timeout"),
        "type narrowed": lambda d: d["properties"]["cost_usd"].update(type="number"),
        "bound tightened": lambda d: d["properties"]["tokens_in"].update(minimum=1),
        "closed": lambda d: d.update(additionalProperties=False),
        "rule changed": lambda d: d["allOf"].pop(),
    }
    for label, mutate in cases.items():
        new = copy.deepcopy(old)
        mutate(new)
        assert breaking_changes(old, new), label
    grown = copy.deepcopy(old)
    grown["properties"]["outcome"]["enum"].append("rate_limited")
    grown["properties"]["extra_field"] = {"type": "string"}
    assert breaking_changes(old, grown) == []  # additive growth is fine


# -- a lagging reader --------------------------------------------------------------------------------


def test_reader_profile_accepts_a_newer_minor_that_strict_rejects() -> None:
    import sys

    sys.path.insert(0, str(SCHEMAS))
    import validate

    doc = json.loads((SCHEMAS / "examples" / "usage-record.model-call.json").read_text())
    doc["schema_version"] = "1.7"
    doc["outcome"] = "rate_limited"  # a value a later minor added
    doc["waste_label"] = "speculative_branch"
    doc["brand_new_field"] = {"anything": 1}
    assert any("outcome" in p for p in validate.problems(doc, "usage-record", strict=True))
    assert validate.problems(doc, "usage-record", strict=False) == []
    doc["schema_version"] = "2.0"
    assert validate.problems(doc, "usage-record", strict=False)  # a new major is still refused


# -- the semantic rules --------------------------------------------------------------------------------


def _doc() -> dict:
    return json.loads((SCHEMAS / "examples" / "usage-record.model-call.json").read_text())


@pytest.mark.parametrize(("mutate", "needle"), [
    (lambda d: d.update(record_id="x:9"), "record_id"),
    (lambda d: d.update(tokens_cache_read=2000), "tokens_cache_read"),
    (lambda d: d.update(tokens_reasoning=10**6), "tokens_reasoning"),
    (lambda d: d.update(usage_source="estimated", cost_billed=True), "not billed"),
    (lambda d: d.update(waste_set_by=None), "waste_set_by"),
])
def test_semantic_rules_catch_what_the_schema_cannot(mutate, needle: str) -> None:
    import sys

    sys.path.insert(0, str(SCHEMAS))
    import validate

    doc = _doc()
    assert validate.problems(doc, "usage-record") == []
    mutate(doc)
    assert any(needle in p for p in validate.problems(doc, "usage-record")), needle


def test_a_third_party_emitter_written_from_the_docs_validates() -> None:
    """The 30-line emitter below uses only ATTRIBUTION.md and the schema, as a new consumer
    would. It carries a call that retried once and a cache hit."""
    import sys
    import time
    import uuid

    sys.path.insert(0, str(SCHEMAS))
    import validate

    def emit(call_id, attempt, *, model, tokens_in, tokens_out, cost, outcome="ok", hit=False):
        doc = {
            "schema_version": "1.1", "record_id": f"{call_id}:{attempt}",
            "kind": "cache_hit" if hit else "model_call", "call_id": call_id, "attempt": attempt,
            "consumer": "third-party-dash", "model": model, "provider": "acme",
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "usage_source": "none" if hit else "reported",
            "cost_usd": cost, "cost_billed": cost is not None,
            "price_source": "cache" if hit else ("price_table" if cost is not None else "none"),
            "cache_hit": hit, "outcome": outcome,
            "started_at_ms": int(time.time() * 1000), "producer": {"name": "mini"},
        }
        if hit:
            doc.update(saved_tokens_in=500, saved_tokens_out=80, saved_cost_usd=0.002)
        return doc

    call = uuid.uuid4().hex
    docs = [
        emit(call, 1, model="m1", tokens_in=500, tokens_out=80, cost=0.002, outcome="parse_failure"),
        emit(call, 2, model="m1", tokens_in=520, tokens_out=90, cost=0.0022),
        emit(uuid.uuid4().hex, 0, model="m1", tokens_in=0, tokens_out=0, cost=0.0, hit=True),
        emit(uuid.uuid4().hex, 1, model="m2", tokens_in=10, tokens_out=0, cost=None),
    ]
    docs[3].update(price_source="none", cost_billed=False)
    for d in docs:
        assert validate.problems(d, "usage-record") == [], d
    wl = {"schema_version": "1.0", "call_id": call, "attempt": 1, "waste_label": "retry",
          "waste_set_by": "consumer"}
    assert validate.problems(wl, "waste-label") == []


# -- two producers, one analysis -----------------------------------------------------------------------


def test_mixed_producer_fixture_validates_and_reduces_without_lying() -> None:
    """awr.llm and moeka records (one duplicate delivery, one record_id collision) merge under
    the definitions in ATTRIBUTION.md. awork-resume's copy of this test asserts the same numbers
    with its own reducer."""
    import sys

    sys.path.insert(0, str(SCHEMAS))
    import validate
    from nanobot.llm_usage.query import reduce_records

    docs = [json.loads(line) for line in (SCHEMAS / "examples" / "mixed-producers.jsonl").read_text().splitlines()]
    for d in docs:
        assert validate.problems(d, "usage-record") == [], d
    total = reduce_records(docs)[0]
    assert (total.requests, total.calls, total.retries) == (5, 3, 2)  # 6 distinct records, 1 hit
    assert total.cache_hits == 1 and total.saved_tokens_in == 100
    assert total.tokens_in == 100 + 120 + 50 + 100 + 0 and total.tokens_out == 20 + 25 + 2048 + 20 + 0
    assert total.cost_usd == pytest.approx(0.001 + 0.0012 + 0.004 + 0.0005)
    assert total.billed_cost_usd == pytest.approx(0.001 + 0.0012 + 0.0005)
    assert total.estimated_cost_usd == pytest.approx(0.004)
    assert (total.unpriced_requests, total.estimated_requests, total.failed_requests) == (1, 1, 2)
    assert total.wasted_tokens == 120  # only the superseded awr attempt carries a label
    assert total.cache_read_tokens == 60  # null counted as 0 in a sum; the doc keeps the null
    by = {t.group["consumer"]: t for t in reduce_records(docs, ("consumer",))}
    assert by["awork-resume"].requests == 3 and by["awork"].requests == 2
