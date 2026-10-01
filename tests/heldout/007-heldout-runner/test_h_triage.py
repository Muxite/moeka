"""Group H: tester-bug triage and requirement extraction (FR-036..038, SC-007)."""

from __future__ import annotations

import json
import re

import pytest

import _h7

BANNER = "PRIVATE TRIAGE: not for the implementer"

SPEC = """# Spec

## Requirements

### Functional Requirements

- **FR-001a**: Decoy item that must not be returned for FR-001.
- **FR-001**: The toy MUST add {a}
  across two lines.
- **FR-002 (held-out note)**: The toy MUST subtract numbers
  correctly across lines {b}.
  - nested detail {c}
- **FR-003**: Other requirement {d}.

#### Next group

- **FR-004**: Four {e}.
"""


def _norm(s: str) -> str:
    return " ".join(s.split())


def _spec(h):
    toks = {k: h.token() for k in "abcde"}
    return _h7.write(h.base / "spec.md", SPEC.format(**toks)), toks


def _triage(h, spec, *extra):
    return h.run("triage", "--repo-name", "demo", "--feature", "001-demo", "--spec", spec, *extra)


def _items(res) -> list[dict]:
    """FR-038 (erratum 2026-10-01): exactly one JSON object, no banner, never a bare list."""
    assert res.code == 0
    assert not res.out.startswith(BANNER), "banner printed in --json mode"
    data = json.loads(res.out)
    assert isinstance(data, dict), "triage --json is not one JSON object"
    assert set(data) == {"schema", "entries"}
    assert data["schema"] == "heldout-triage.v1"
    assert isinstance(data["entries"], list)
    for e in data["entries"]:
        assert set(e) == {"id", "nodeid", "assertion", "spec"}
    return data["entries"]


TRIAGE_SUITE = '''
@pytest.mark.fr("FR-001")
def test_add_ok():
    pass


@pytest.mark.fr("FR-001")
def test_add_bad():
    # SOURCE-{src}
    assert 1 + 1 == 3, "ADD-{m1}"


@pytest.mark.fr("FR-002")
def test_sub_bad():
    assert 3 - 1 == 1, "SUB-{m2}"


@pytest.mark.fr("FR-002", "FR-003")
def test_both_bad():
    pytest.fail("PLAIN-{m3}", pytrace=False)


@pytest.mark.fr("FR-009")
def test_unspecified_bad():
    assert False, "NOSPEC-{m4}"
'''


@pytest.fixture
def failed_round(h, tree):
    m = {k: h.token() for k in ("src", "m1", "m2", "m3", "m4")}
    h.write_suite({"test_s.py": TRIAGE_SUITE.format(**m)})
    res = h.run_std()
    assert res.code == 1
    return m


@pytest.mark.fr("FR-036", "FR-038", "SC-007")
def test_triage_filtered_by_id(h, failed_round):
    spec, toks = _spec(h)
    res = _triage(h, spec, "--id", "FR-002")
    assert res.code == 0
    lines = res.lines()
    assert lines[0] == BANNER
    headers = [l for l in lines if l.startswith("== ")]
    assert len(headers) == 2, "expected the two failing FR-002 tests"
    for hd in headers:
        assert hd.startswith("== FR-002: ")
    assert any("test_sub_bad" in hd for hd in headers)
    assert any("test_both_bad" in hd for hd in headers)
    assert "assertion:" in res.out and "spec:" in res.out
    assert f"SUB-{failed_round['m2']}" in res.out
    assert f"PLAIN-{failed_round['m3']}" in res.out
    assert f"ADD-{failed_round['m1']}" not in res.out
    flat = _norm(res.out)
    assert f"The toy MUST subtract numbers correctly across lines {toks['b']}." in flat
    assert f"nested detail {toks['c']}" in flat
    assert f"Other requirement {toks['d']}." in flat, "spec of every id the test carries"


@pytest.mark.fr("FR-036", "SC-007")
def test_triage_all_failing(h, failed_round):
    spec, toks = _spec(h)
    res = _triage(h, spec)
    assert res.code == 0
    assert res.lines()[0] == BANNER
    headers = [l for l in res.lines() if l.startswith("== ")]
    names = ["test_add_bad", "test_sub_bad", "test_both_bad", "test_unspecified_bad"]
    for n in names:
        assert any(n in hd for hd in headers), n
    assert not any("test_add_ok" in hd for hd in headers)
    for hd in headers:
        assert re.match(r"== (FR-00[1239]): \S", hd)
    flat = _norm(res.out)
    assert f"The toy MUST add {toks['a']} across two lines." in flat
    assert "Decoy item" not in flat
    assert "(not found in spec)" in res.out, "FR-009 is absent from the spec"


@pytest.mark.fr("FR-036")
def test_assertion_block_prefers_e_lines(h, failed_round):
    spec, _ = _spec(h)
    res = _triage(h, spec, "--id", "FR-001", "--json")
    items = _items(res)
    assert len(items) == 1
    a = items[0]["assertion"]
    assert f"ADD-{failed_round['m1']}" in a
    assert f"SOURCE-{failed_round['src']}" not in a, "non-E lines included although E lines exist"


@pytest.mark.fr("FR-036")
def test_assertion_capped_at_4000(h, tree):
    h.write_suite({"test_s.py": '''
@pytest.mark.fr("FR-001")
def test_long():
    raise AssertionError("L" * 20000)
'''})
    h.run_std()
    spec, _ = _spec(h)
    items = _items(_triage(h, spec, "--json"))
    assert len(items) == 1
    assert 0 < len(items[0]["assertion"]) <= 4000
    assert "LLLL" in items[0]["assertion"]


@pytest.mark.fr("FR-036")
def test_assertion_last_20_lines_without_e_lines(h, tree):
    body = "\\n".join(f"line{i:02d}" for i in range(40))
    h.write_suite({"test_s.py": f'''
@pytest.mark.fr("FR-001")
def test_plain():
    pytest.fail("{body}", pytrace=False)
'''})
    h.run_std()
    spec, _ = _spec(h)
    a = _items(_triage(h, spec, "--json"))[0]["assertion"]
    assert "line39" in a and "line25" in a
    assert "line05" not in a


@pytest.mark.fr("FR-036")
def test_triage_round_option(h, tree):
    t1, t2 = h.token(), h.token()
    h.write_suite({"test_s.py": f'''
@pytest.mark.fr("FR-001")
def test_r():
    assert False, "R1-{t1}"
'''})
    h.run_std()
    h.write_suite({"test_s.py": f'''
@pytest.mark.fr("FR-001")
def test_r():
    assert False, "R2-{t2}"
'''})
    h.run_std()
    spec, _ = _spec(h)
    latest = _triage(h, spec)
    assert f"R2-{t2}" in latest.out and f"R1-{t1}" not in latest.out
    first = _triage(h, spec, "--round", "1")
    assert f"R1-{t1}" in first.out and f"R2-{t2}" not in first.out


@pytest.mark.fr("FR-038")
def test_triage_json_schema(h, failed_round):
    spec, toks = _spec(h)
    res = _triage(h, spec, "--json", "--id", "FR-002")
    assert res.code == 0
    items = _items(res)
    assert len(items) == 2
    for it in items:
        assert {"id", "nodeid", "assertion", "spec"} <= set(it)
        assert it["id"] == "FR-002"
    sub = [it for it in items if "test_sub_bad" in it["nodeid"]][0]
    assert f"SUB-{failed_round['m2']}" in sub["assertion"]
    assert f"correctly across lines {toks['b']}" in _norm(json.dumps(sub["spec"]))


@pytest.mark.fr("FR-038")
def test_triage_counts_nothing_writes_nothing(h, failed_round):
    spec, _ = _spec(h)
    runs_before = h.runs()
    before = _h7.snapshot(h.root)
    before.pop("_reports/demo/001-demo/.lock", None)
    _triage(h, spec)
    _triage(h, spec, "--json", "--id", "FR-001")
    after = _h7.snapshot(h.root)
    after.pop("_reports/demo/001-demo/.lock", None)
    assert after == before, "triage changed the report tree"
    assert h.counted() == 1
    assert h.runs() == runs_before, "triage ran tests"


# -- FR-037 extract_requirement --------------------------------------------------------------------

EXTRACT_SPEC = """# Title

### Functional Requirements

- **FR-001a**: Decoy.
- **FR-001**: First line
  continuation line.
- **FR-002 (held-out pattern)**: Two
  more two
  - nested two
    deeper two
- **FR-003**: Three

  | col | table |
  |---|---|

  after table three
- **FR-004**: Four
#### Heading after four
- **FR-005**: Five

Paragraph not part of five.

  - **FR-006**: indented item six
    continuation six
  - **FR-007**: indented item seven
- **FR-008**: Eight
"""


@pytest.mark.fr("FR-037")
def test_extract_basic_and_continuation(mod):
    t = mod.extract_requirement(EXTRACT_SPEC, "FR-001")
    assert t is not None
    assert "**FR-001**" in t
    assert "First line" in t and "continuation line." in t
    assert "Decoy" not in t and "FR-002" not in t


@pytest.mark.fr("FR-037")
def test_extract_parenthesised_suffix_and_nested(mod):
    t = mod.extract_requirement(EXTRACT_SPEC, "FR-002")
    assert t is not None
    for frag in ("Two", "more two", "nested two", "deeper two"):
        assert frag in t
    assert "Three" not in t


@pytest.mark.fr("FR-037")
def test_extract_blank_then_indented_continues(mod):
    t = mod.extract_requirement(EXTRACT_SPEC, "FR-003")
    assert "| col | table |" in t and "after table three" in t
    assert "Four" not in t


@pytest.mark.fr("FR-037")
def test_extract_stops_at_heading(mod):
    t = mod.extract_requirement(EXTRACT_SPEC, "FR-004")
    assert "Four" in t and "Heading" not in t


@pytest.mark.fr("FR-037")
def test_extract_stops_at_blank_then_unindented(mod):
    t = mod.extract_requirement(EXTRACT_SPEC, "FR-005")
    assert "Five" in t and "Paragraph" not in t


@pytest.mark.fr("FR-037")
def test_extract_indented_items(mod):
    t6 = mod.extract_requirement(EXTRACT_SPEC, "FR-006")
    assert "indented item six" in t6 and "continuation six" in t6
    assert "seven" not in t6
    t7 = mod.extract_requirement(EXTRACT_SPEC, "FR-007")
    assert "indented item seven" in t7 and "Eight" not in t7


@pytest.mark.fr("FR-037")
def test_extract_last_item_and_missing(mod):
    assert "Eight" in mod.extract_requirement(EXTRACT_SPEC, "FR-008")
    assert mod.extract_requirement(EXTRACT_SPEC, "FR-099") is None
    assert mod.extract_requirement(EXTRACT_SPEC, "FR-00") is None


@pytest.mark.fr("FR-037")
def test_extract_from_real_shaped_spec(mod):
    spec = """- **FR-034**: The cap is `--cap N` (default 4). A counted run when `len(rounds) >= cap` MUST be refused with
  `E_ROUND_CAP`, exit 4, before anything is copied.
- **FR-035**: `heldout-run rounds` MUST print one line per round. [NEEDS CLARIFICATION: when the head
  finds a tester bug? Recommended default: it counts.]

#### Tester-bug triage (head only)

- **FR-036**: next
"""
    t = mod.extract_requirement(spec, "FR-035")
    assert "Recommended default: it counts.]" in t
    assert "Tester-bug" not in t and "FR-036" not in t
    t34 = mod.extract_requirement(spec, "FR-034")
    assert "before anything is copied." in t34 and "FR-035" not in t34
