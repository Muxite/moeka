"""Group D: mapping tests to requirement ids and outcome rules (FR-017..020, SC-001)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import _h7
from _h7 import assert_refused

# Tests without markers; ids come from a map. test_delta also has a marker (union).
MAP_SUITE = '''
@pytest.mark.parametrize("x", [1, 2, 3])
def test_param(x):
    assert x == 1


def test_alpha():
    _mark_run()


def test_beta():
    assert False


def test_gamma():
    assert False


@pytest.mark.fr("FR-004")
def test_delta():
    assert False
'''
MAP = {"test_alpha": ["FR-001"], "test_beta": ["FR-002", "SC-001"], "test_param": ["FR-003"],
       "test_delta": ["FR-001"]}
MAP_EXPECTED = [
    "heldout-run: 001-demo round 1/4: failed",
    "FR-001: 1/2 failing",
    "FR-002: 1/1 failing",
    "FR-003: 2/3 failing",
    "FR-004: 1/1 failing",
    "SC-001: 1/1 failing",
    "UNMAPPED: 1/1 failing",
    "total: 5/7 tests failing, 0 skipped; 6/6 ids failing",
]
WRONG_MAP = {"test_alpha": ["FR-009"], "test_beta": ["FR-009"], "test_param": ["FR-009"],
             "test_gamma": ["FR-009"]}

FR_REPORT_TAIL = '''

def main(junit):
    import collections, re, xml.etree.ElementTree as ET
    fail = collections.Counter()
    tot = collections.Counter()
    for tc in ET.parse(junit).iter("testcase"):
        key = re.sub(r"\\[.*\\]$", "", tc.get("name"))
        if tc.find("skipped") is not None:
            continue
        bad = tc.find("failure") is not None or tc.find("error") is not None
        for i in MAP.get(key, ["UNMAPPED"]):
            tot[i] += 1
            fail[i] += bad
    for i in sorted(tot):
        print(f"{i}: {fail[i]} failing of {tot[i]}")


if __name__ == "__main__":
    import sys
    main(sys.argv[1])
'''


def _fr_report(map_obj, annotated=False) -> str:
    head = "MAP: dict[str, list[str]] = " if annotated else "MAP = "
    return '"""fr_report for the canary suite."""\nimport sys\n\n' + head + repr(map_obj) + \
        "\n" + FR_REPORT_TAIL


# -- FR-017 markers + map union, keys ---------------------------------------------------------

@pytest.mark.fr("FR-017", "SC-001")
def test_marker_mode_us1_exact_output(h, tree):
    h.write_suite({"test_s.py": _h7.US1_SUITE})
    res = h.run_std()
    assert res.code == 1
    assert res.err == ""
    assert res.lines() == _h7.US1_FAIL_LINES


@pytest.mark.fr("FR-017", "FR-018", "SC-001")
def test_spec_map_json_mode(h, tree):
    h.write_suite({"test_s.py": MAP_SUITE, "SPEC-MAP.json": json.dumps(MAP)})
    res = h.run_std()
    assert res.code == 1
    assert res.lines() == MAP_EXPECTED


@pytest.mark.fr("FR-018", "SC-001")
def test_spec_map_json_wrapped(h, tree):
    h.write_suite({"test_s.py": MAP_SUITE, "SPEC-MAP.json": json.dumps({"map": MAP})})
    res = h.run_std()
    assert res.lines() == MAP_EXPECTED


@pytest.mark.fr("FR-018", "SC-001")
@pytest.mark.parametrize("annotated", [False, True], ids=["plain", "annotated"])
def test_fr_report_py_mode(h, tree, annotated):
    h.write_suite({"test_s.py": MAP_SUITE, "fr_report.py": _fr_report(MAP, annotated)})
    res = h.run_std()
    assert res.code == 1
    assert res.lines() == MAP_EXPECTED


@pytest.mark.fr("FR-018", "SC-001")
def test_counts_equal_fr_report_on_same_junit(h, tree):
    """US1-3: awork-resume style suite (no markers), counts equal fr_report.py's."""
    suite = '''
@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_keep(n):
    assert n < 2


def test_purge():
    assert False


def test_audit():
    pass
'''
    m = {"test_keep": ["FR-007"], "test_purge": ["FR-007", "FR-008"], "test_audit": ["FR-009"]}
    h.write_suite({"test_s.py": suite, "fr_report.py": _fr_report(m)})
    res = h.run_std("--json")
    assert res.code == 1
    fb = res.json()
    junit = h.round_dir(1) / "junit.xml"
    r = subprocess.run([sys.executable, str(h.suite / "fr_report.py"), str(junit)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    theirs = {}
    for line in r.stdout.splitlines():
        mm = re.fullmatch(r"(\S+): (\d+) failing of (\d+)", line)
        theirs[mm.group(1)] = (int(mm.group(2)), int(mm.group(3)))
    ours = {k: (v["failing"], v["total"]) for k, v in fb["failing"].items()}
    assert ours == {k: v for k, v in theirs.items() if v[0] > 0}
    assert ours == {"FR-007": (3, 5), "FR-008": (1, 1)}


@pytest.mark.fr("FR-017")
def test_parametrised_key_is_original_name(h, tree):
    suite = '''
@pytest.mark.parametrize("v", ["a", "b"], ids=["first", "second"])
def test_pk(v):
    assert v == "a"
'''
    h.write_suite({"test_s.py": suite, "SPEC-MAP.json": json.dumps({"test_pk": ["FR-005"]})})
    res = h.run_std()
    assert res.lines()[1:] == ["FR-005: 1/2 failing",
                               "total: 1/2 tests failing, 0 skipped; 1/1 ids failing"]


@pytest.mark.fr("FR-017")
def test_class_method_key_and_several_markers(h, tree):
    suite = '''
@pytest.mark.fr("FR-001")
class TestGroup:
    @pytest.mark.fr("FR-002")
    def test_m(self):
        assert False

    def test_n(self):
        pass
'''
    h.write_suite({"test_s.py": suite, "SPEC-MAP.json": json.dumps({"test_n": ["FR-003"]})})
    res = h.run_std()
    assert res.lines()[1:] == ["FR-001: 1/2 failing", "FR-002: 1/1 failing",
                               "total: 1/2 tests failing, 0 skipped; 2/3 ids failing"]


@pytest.mark.fr("FR-017", "FR-019")
def test_duplicate_ids_on_one_test_count_once(h, tree):
    suite = '''
@pytest.mark.fr("FR-001", "FR-001")
@pytest.mark.fr("FR-001")
def test_dup():
    assert False
'''
    h.write_suite({"test_s.py": suite, "SPEC-MAP.json": json.dumps({"test_dup": ["FR-001"]})})
    res = h.run_std()
    assert res.lines()[1:] == ["FR-001: 1/1 failing",
                               "total: 1/1 tests failing, 0 skipped; 1/1 ids failing"]


# -- FR-018 map source precedence, literal_eval only ------------------------------------------

@pytest.mark.fr("FR-018")
def test_spec_map_json_wins_over_fr_report(h, tree):
    h.write_suite({"test_s.py": MAP_SUITE, "SPEC-MAP.json": json.dumps(MAP),
                   "fr_report.py": _fr_report(WRONG_MAP)})
    res = h.run_std()
    assert res.lines() == MAP_EXPECTED


@pytest.mark.fr("FR-018")
@pytest.mark.parametrize("fmt", ["json", "py"])
def test_map_option_wins_over_suite_files(h, tree, fmt):
    h.write_suite({"test_s.py": MAP_SUITE, "SPEC-MAP.json": json.dumps(WRONG_MAP),
                   "fr_report.py": _fr_report(WRONG_MAP)})
    mp = h.base / ("map.json" if fmt == "json" else "fr_map.py")
    _h7.write(mp, json.dumps(MAP) if fmt == "json" else _fr_report(MAP))
    res = h.run_std("--map", mp)
    assert res.lines() == MAP_EXPECTED


@pytest.mark.fr("FR-018")
def test_fr_report_is_never_executed(h, tree):
    pwn = h.out / "pwned-module-level"
    src = ("import os, pathlib\n"
           f"pathlib.Path({str(pwn)!r}).write_text('executed')\n"
           f"MAP = {MAP!r}\n"
           f"pathlib.Path({str(pwn)!r} + '-after').write_text('executed')\n")
    h.write_suite({"test_s.py": MAP_SUITE, "fr_report.py": src})
    res = h.run_std()
    assert not pwn.exists() and not Path(str(pwn) + "-after").exists(), "fr_report.py executed"
    assert res.lines() == MAP_EXPECTED


@pytest.mark.fr("FR-018")
def test_map_with_code_is_e_map_and_not_executed(h, tree):
    pwn = h.out / "pwned-literal"
    src = ("MAP = {'test_alpha': [__import__('pathlib').Path("
           f"{str(pwn)!r}).write_text('x') and 'FR-001']}}\n")
    h.write_suite({"test_s.py": MAP_SUITE, "fr_report.py": src})
    res = h.run_std()
    assert_refused(res, "E_MAP", 2)
    assert not pwn.exists(), "MAP expression was evaluated"
    assert h.runs() == 0
    assert h.counted() == 0


@pytest.mark.fr("FR-018")
@pytest.mark.parametrize("case", ["json_syntax", "json_list", "py_syntax", "missing_file",
                                  "json_wrapped_list"])
def test_malformed_map_is_e_map(h, tree, case):
    h.write_suite({"test_s.py": MAP_SUITE})
    extra = []
    if case == "json_syntax":
        _h7.write(h.suite / "SPEC-MAP.json", "{not json")
    elif case == "json_list":
        _h7.write(h.suite / "SPEC-MAP.json", '["FR-001"]')
    elif case == "json_wrapped_list":
        _h7.write(h.suite / "SPEC-MAP.json", '{"map": ["FR-001"]}')
    elif case == "py_syntax":
        _h7.write(h.suite / "fr_report.py", "MAP = {'test_alpha': ['FR-001']\n")
    elif case == "missing_file":
        extra = ["--map", h.base / "no-such-map.json"]
    res = h.run_std(*extra)
    assert_refused(res, "E_MAP", 2)
    assert h.runs() == 0


# -- FR-019 INVALID_ID / UNMAPPED -----------------------------------------------------------------

@pytest.mark.fr("FR-019")
def test_invalid_ids_counted_and_never_echoed(h, tree):
    free = "free text " + h.token()
    suite = f'''
@pytest.mark.fr({free!r})
def test_x():
    assert False


@pytest.mark.fr("FR-001", "FR-1")
def test_y():
    assert False


def test_z():
    pass
'''
    bad_map_id = "fr-001-" + h.token()
    h.write_suite({"test_s.py": suite,
                   "SPEC-MAP.json": json.dumps({"test_z": [bad_map_id]})})
    res = h.run_std()
    assert res.lines() == [
        "heldout-run: 001-demo round 1/4: failed",
        "FR-001: 1/1 failing",
        "INVALID_ID: 2/3 failing",
        "total: 2/3 tests failing, 0 skipped; 2/2 ids failing",
    ]
    assert "FR-1" not in res.out.replace("FR-001", "")
    assert not h.leaks(res.out, res.err)


@pytest.mark.fr("FR-019")
def test_unmapped_and_multi_id_counting(h, tree):
    suite = '''
def test_u1():
    assert False


def test_u2():
    pass


@pytest.mark.fr("FR-001", "NFR-001", "SC-002")
def test_multi():
    assert False
'''
    h.write_suite({"test_s.py": suite})
    res = h.run_std("--json")
    fb = res.json()
    assert fb["failing"] == {
        "FR-001": {"failing": 1, "total": 1},
        "NFR-001": {"failing": 1, "total": 1},
        "SC-002": {"failing": 1, "total": 1},
        "UNMAPPED": {"failing": 1, "total": 2},
    }
    assert fb["totals"] == {"tests": 3, "failing": 2, "skipped": 0, "ids": 4, "ids_failing": 4}


# -- FR-020 outcomes ------------------------------------------------------------------------------------

OUTCOME_SUITE = '''
@pytest.fixture
def broken_setup():
    raise RuntimeError("setup boom")


@pytest.fixture
def broken_teardown():
    yield 1
    raise RuntimeError("teardown boom")


@pytest.mark.fr("FR-001")
def test_pass():
    pass


@pytest.mark.fr("FR-001")
def test_fail():
    assert False


@pytest.mark.fr("FR-002")
def test_setup_error(broken_setup):
    pass


@pytest.mark.fr("FR-002")
def test_teardown_error(broken_teardown):
    pass


@pytest.mark.fr("FR-003")
@pytest.mark.skip(reason="skipped")
def test_skip_marker():
    assert False


@pytest.mark.fr("FR-003")
def test_skip_call():
    pytest.skip("inside")


@pytest.mark.fr("FR-004")
@pytest.mark.xfail(reason="known")
def test_xfail():
    assert False


@pytest.mark.fr("FR-004")
@pytest.mark.xfail(reason="strict", strict=True)
def test_xpass_strict():
    pass


@pytest.mark.fr("FR-005")
@pytest.mark.xfail(reason="lenient", strict=False)
def test_xpass_lenient():
    pass


@pytest.mark.fr("FR-005")
def test_imperative_xfail():
    pytest.xfail("imperative")


@pytest.mark.fr("FR-006")
@pytest.mark.skipif(True, reason="never")
def test_skipif():
    pass
'''


@pytest.mark.fr("FR-020", "FR-021")
def test_outcome_rules(h, tree):
    h.write_suite({"test_s.py": OUTCOME_SUITE})
    res = h.run_std()
    assert res.code == 1
    assert res.lines() == [
        "heldout-run: 001-demo round 1/4: failed",
        "FR-001: 1/2 failing",
        "FR-002: 2/2 failing",
        "FR-004: 1/2 failing",
        "total: 4/8 tests failing, 3 skipped; 3/4 ids failing",
    ]


@pytest.mark.fr("FR-020", "FR-022")
def test_outcome_rules_json(h, tree):
    h.write_suite({"test_s.py": OUTCOME_SUITE})
    fb = h.run_std("--json").json()
    assert fb["failing"] == {"FR-001": {"failing": 1, "total": 2},
                             "FR-002": {"failing": 2, "total": 2},
                             "FR-004": {"failing": 1, "total": 2}}
    assert fb["totals"] == {"tests": 8, "failing": 4, "skipped": 3, "ids": 4, "ids_failing": 3}


@pytest.mark.fr("FR-020", "FR-028")
def test_outcomes_in_private_report(h, tree):
    h.write_suite({"test_s.py": OUTCOME_SUITE})
    h.run_std()
    report = json.loads((h.round_dir(1) / "report.json").read_text())
    by_key = {}
    for t in report["tests"]:
        by_key[t["key"]] = t["outcome"]
    assert by_key == {
        "test_pass": "passed", "test_fail": "failed", "test_setup_error": "failed",
        "test_teardown_error": "failed", "test_skip_marker": "skipped",
        "test_skip_call": "skipped", "test_xfail": "passed", "test_xpass_strict": "failed",
        "test_xpass_lenient": "passed", "test_imperative_xfail": "passed",
        "test_skipif": "skipped",
    }


@pytest.mark.fr("FR-020")
def test_all_skipped_id_not_counted(h, tree):
    suite = '''
@pytest.mark.fr("FR-001")
def test_a():
    pass


@pytest.mark.fr("FR-002")
@pytest.mark.skip(reason="x")
def test_b():
    pass
'''
    h.write_suite({"test_s.py": suite})
    res = h.run_std()
    assert res.code == 0
    assert res.lines() == ["heldout-run: 001-demo round 1/4: passed",
                           "total: 0/1 tests failing, 1 skipped; 0/1 ids failing"]


# -- Python API --------------------------------------------------------------------------------------------

@pytest.mark.fr("FR-018")
def test_load_id_map_json_and_wrapped(mod, h):
    p = _h7.write(h.base / "m.json", json.dumps({"test_a": ["FR-001", "SC-002"], "test_b": ["FR-003"]}))
    m = mod.load_id_map(p)
    assert dict(m) == {"test_a": ("FR-001", "SC-002"), "test_b": ("FR-003",)}
    assert all(isinstance(v, tuple) for v in m.values())
    p2 = _h7.write(h.base / "w.json", json.dumps({"map": {"test_c": ["NFR-001"]}}))
    assert dict(mod.load_id_map(p2)) == {"test_c": ("NFR-001",)}


@pytest.mark.fr("FR-018")
@pytest.mark.parametrize("annotated", [False, True], ids=["plain", "annotated"])
def test_load_id_map_py(mod, h, annotated):
    pwn = h.out / "pwned-api"
    src = (f"import pathlib\npathlib.Path({str(pwn)!r}).write_text('x')\n"
           + ("MAP: dict = " if annotated else "MAP = ")
           + "{'test_a': ['FR-001'], 'test_b': ('FR-002', 'FR-003')}\n"
           "def main():\n    raise SystemExit(5)\nmain()\n")
    p = _h7.write(h.base / "fr_report.py", src)
    m = mod.load_id_map(p)
    assert dict(m) == {"test_a": ("FR-001",), "test_b": ("FR-002", "FR-003")}
    assert not pwn.exists()


@pytest.mark.fr("FR-018")
def test_load_id_map_rejects_code(mod, h):
    pwn = h.out / "pwned-api2"
    p = _h7.write(h.base / "fr_report.py",
                  f"MAP = {{'t': [open({str(pwn)!r}, 'w').write('x')]}}\n")
    with pytest.raises(Exception):
        mod.load_id_map(p)
    assert not pwn.exists()
