"""Validate a usage document: JSON Schema plus the cross-field rules a schema cannot say.

    python schemas/validate.py usage-record record.jsonl      # one document per line
    from validate import problems; problems(doc, "usage-record")  # [] when valid

Needs ``jsonschema`` for the schema half (``pip install jsonschema``); the semantic half is
stdlib only and runs without it. ``strict=False`` is the READER profile: enums are not
enforced, so a record written by a newer minor version (a new ``waste_label`` value) is
accepted by an older reader instead of rejected. Producers validate strict; readers lenient.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _open_enums(node: Any) -> Any:
    """A copy of *node* with every ``enum`` relaxed to its value types (the reader profile)."""
    if isinstance(node, dict):
        out = {k: _open_enums(v) for k, v in node.items() if k != "enum"}
        if "enum" in node:
            names = {"NoneType": "null", "str": "string", "int": "integer", "bool": "boolean",
                     "float": "number"}
            kinds = sorted({names.get(type(v).__name__, "string") for v in node["enum"]})
            out.setdefault("type", kinds[0] if len(kinds) == 1 else kinds)
        return out
    if isinstance(node, list):
        return [_open_enums(v) for v in node]
    return node


def load_schema(name: str, *, strict: bool = True) -> dict[str, Any]:
    schema = json.loads((HERE / f"{name}.v1.schema.json").read_text())
    return schema if strict else _open_enums(copy.deepcopy(schema))


def semantic_problems(doc: dict[str, Any], name: str) -> list[str]:
    """Rules that span fields; each message names the rule."""
    out: list[str] = []
    if name == "usage-record":
        tin, tout = doc.get("tokens_in") or 0, doc.get("tokens_out") or 0
        read, write = doc.get("tokens_cache_read"), doc.get("tokens_cache_write")
        if doc.get("record_id") != f"{doc.get('call_id')}:{doc.get('attempt')}":
            out.append("record_id must be '<call_id>:<attempt>'")
        if (read or 0) + (write or 0) > tin:
            out.append("tokens_cache_read + tokens_cache_write must not exceed tokens_in")
        if (doc.get("tokens_reasoning") or 0) > tout:
            out.append("tokens_reasoning is a subset of tokens_out and must not exceed it")
        if doc.get("cost_billed") and doc.get("cost_usd") is None:
            out.append("cost_billed true needs a cost_usd")
        if doc.get("usage_source") in ("estimated", "mixed") and doc.get("cost_billed") \
                and doc.get("price_source") not in ("local_zero", "cache"):
            out.append("an estimated count at a price table is not billed (cost_billed false)")
        if doc.get("waste_label") and not doc.get("waste_set_by"):
            out.append("a waste_label needs waste_set_by")
        if doc.get("kind") == "cache_hit" and doc.get("saved_tokens_in") is None \
                and doc.get("saved_cost_usd") is not None:
            out.append("saved_cost_usd without saved_tokens_in")
    elif name == "budget-event":
        cap, spent, held = doc.get("cap_usd"), doc.get("spent_usd"), doc.get("reserved_usd")
        rem = doc.get("remaining_usd")
        if None not in (cap, spent, held, rem) and abs(rem - max(0.0, cap - spent - held)) > 1e-9:
            out.append("remaining_usd must be max(0, cap_usd - spent_usd - reserved_usd)")
        if cap is None and rem is not None:
            out.append("remaining_usd must be null when uncapped")
    return out


def problems(doc: dict[str, Any], name: str, *, strict: bool = True) -> list[str]:
    """Every problem with *doc* as a *name* document (``[]`` = valid)."""
    out: list[str] = []
    try:
        import jsonschema

        validator = jsonschema.Draft202012Validator(load_schema(name, strict=strict))
        out.extend(f"{'/'.join(map(str, e.path)) or '$'}: {e.message}"
                   for e in validator.iter_errors(doc))
    except ImportError:
        out.append("jsonschema is not installed: schema half skipped")
    return out + semantic_problems(doc, name)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("usage-record", "budget-event", "waste-label"):
        print(__doc__)
        return 2
    bad = 0
    for n, line in enumerate(Path(argv[2]).read_text().splitlines(), 1):
        if line.strip():
            for p in problems(json.loads(line), argv[1]):
                print(f"line {n}: {p}")
                bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
