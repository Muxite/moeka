"""Group E: feedback format, JSON, names, stderr and exit codes (FR-021..026)."""

from __future__ import annotations

import json
import re

import pytest

import _h7
from _h7 import assert_refused

FEEDBACK_KEYS = {"schema", "feature", "round", "round_cap", "status", "cap_reached", "failing",
                 "totals"}
TOTALS_KEYS = {"tests", "failing", "skipped", "ids", "ids_failing"}


def _norm(x):
    return json.loads(json.dumps(x))


# -- FR-021 text format ---------------------------------------------------------------------------

@pytest.mark.fr("FR-021", "FR-026")
def test_all_pass_prints_no_id_lines_exit_0(h, tree):
    h.write_suite({"test_s.py": _h7.US1_SUITE.replace("== 3, \"two_c", "== 2, \"two_c")
                   .replace("== 11, \"two_d", "== 10, \"two_d")})
    res = h.run_std()
    assert res.code == 0
    assert res.err == ""
    assert res.lines() == ["heldout-run: 001-demo round 1/4: passed",
                           "total: 0/6 tests failing, 0 skipped; 0/2 ids failing"]


@pytest.mark.fr("FR-021", "FR-026")
def test_us1_failed_exact_and_exit_1(h, tree):
    h.write_suite({"test_s.py": _h7.US1_SUITE})
    res = h.run_std()
    assert res.code == 1
    assert res.err == ""
    assert res.lines() == _h7.US1_FAIL_LINES


ORDER_SUITE = '''
@pytest.mark.fr("SC-001")
def test_1():
    assert False


@pytest.mark.fr("FR-010")
def test_2():
    assert False


def test_3():
    assert False


@pytest.mark.fr("NFR-002")
def test_4():
    assert False


@pytest.mark.fr("FR-002a")
def test_5():
    assert False


@pytest.mark.fr("not-an-id")
def test_6():
    assert False


@pytest.mark.fr("FR-002")
def test_7():
    assert False


@pytest.mark.fr("FR-001")
def test_8():
    pass


@pytest.mark.fr("FR-100")
def test_9():
    assert False
'''


@pytest.mark.fr("FR-021")
def test_id_line_ordering(h, tree):
    h.write_suite({"test_s.py": ORDER_SUITE})
    res = h.run_std()
    assert res.lines() == [
        "heldout-run: 001-demo round 1/4: failed",
        "FR-002: 1/1 failing",
        "FR-002a: 1/1 failing",
        "FR-010: 1/1 failing",
        "FR-100: 1/1 failing",
        "NFR-002: 1/1 failing",
        "SC-001: 1/1 failing",
        "INVALID_ID: 1/1 failing",
        "UNMAPPED: 1/1 failing",
        "total: 8/9 tests failing, 0 skipped; 8/9 ids failing",
    ]


@pytest.mark.fr("FR-021")
def test_skipped_count_in_total(h, tree):
    suite = '''
@pytest.mark.fr("FR-001")
def test_a():
    assert False


@pytest.mark.fr("FR-001")
@pytest.mark.skip(reason="r")
def test_b():
    pass


@pytest.mark.fr("FR-001")
def test_c():
    pytest.skip("r")
'''
    h.write_suite({"test_s.py": suite})
    res = h.run_std()
    assert res.lines() == ["heldout-run: 001-demo round 1/4: failed", "FR-001: 1/1 failing",
                           "total: 1/1 tests failing, 2 skipped; 1/1 ids failing"]


@pytest.mark.fr("FR-021")
def test_no_count_round_dash(h, tree):
    h.write_suite({"test_s.py": _h7.US1_SUITE})
    res = h.run_std("--no-count")
    assert res.code == 1
    assert res.lines()[0] == "heldout-run: 001-demo round -/4: failed"
    assert res.lines()[1:] == _h7.US1_FAIL_LINES[1:]


@pytest.mark.fr("FR-021", "FR-034")
def test_cap_option_in_header(h, tree):
    h.write_suite({"test_s.py": _h7.US1_SUITE})
    res = h.run_std("--cap", "6")
    assert res.lines()[0] == "heldout-run: 001-demo round 1/6: failed"


# -- FR-022 JSON ----------------------------------------------------------------------------------

@pytest.mark.fr("FR-022")
def test_json_object_exact(h, tree):
    h.write_suite({"test_s.py": _h7.US1_SUITE})
    res = h.run_std("--json")
    assert res.code == 1
    assert res.err == ""
    fb = json.loads(res.out)
    assert set(fb) == FEEDBACK_KEYS
    assert fb == {
        "schema": "heldout-feedback.v1", "feature": "001-demo", "round": 1, "round_cap": 4,
        "status": "failed", "cap_reached": False,
        "failing": {"FR-002": {"failing": 2, "total": 4}},
        "totals": {"tests": 6, "failing": 2, "skipped": 0, "ids": 2, "ids_failing": 1},
    }


@pytest.mark.fr("FR-022")
def test_json_passed_and_no_count(h, tree):
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    fb = h.run_std("--json", "--no-count").json()
    assert fb["round"] is None
    assert fb["status"] == "passed"
    assert fb["failing"] == {}
    assert set(fb["totals"]) == TOTALS_KEYS
    assert fb["cap_reached"] is False


@pytest.mark.fr("FR-022")
def test_feedback_file_with_text_output(h, tree):
    h.write_suite({"test_s.py": _h7.US1_SUITE})
    ff = h.base / "fb" / "feedback-out.json"
    _h7.mkd(ff.parent)
    res = h.run_std("--feedback-file", ff)
    assert res.lines() == _h7.US1_FAIL_LINES
    fb = json.loads(ff.read_text())
    assert fb["schema"] == "heldout-feedback.v1"
    assert fb["failing"] == {"FR-002": {"failing": 2, "total": 4}}


@pytest.mark.fr("FR-022")
def test_feedback_file_equals_json_stdout(h, tree):
    h.write_suite({"test_s.py": _h7.US1_SUITE})
    ff = h.base / "fb.json"
    res = h.run_std("--json", "--feedback-file", ff)
    assert json.loads(ff.read_text()) == res.json()


# -- FR-023 names ---------------------------------------------------------------------------------

@pytest.mark.fr("FR-023")
@pytest.mark.parametrize("feature", ["1-demo", "001-Demo", "001_demo", "001-", "../001-demo",
                                     "001-demo/x", "001--", "0001-demo", "001-demo ", "abc-demo",
                                     "001-dé"])
def test_bad_feature_name(h, tree, feature):
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    res = h.run_std(feature=feature)
    assert_refused(res, "E_FEATURE", 2)
    assert h.runs() == 0


@pytest.mark.fr("FR-023")
@pytest.mark.parametrize("name", ["Demo", "de/mo", "a" * 65, "..", "de mo", "_demo"])
def test_bad_repo_name(h, tree, name):
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    res = h.run_std(repo_name=name)
    assert_refused(res, "E_FEATURE", 2)
    assert h.runs() == 0


@pytest.mark.fr("FR-023")
@pytest.mark.parametrize("feature,name", [("001-a", "a"), ("123-x-y-9", "a" * 64),
                                          ("007-heldout-runner", "awork_resume-2")])
def test_good_names_accepted(h, tree, feature, name):
    suite = h.root / name / feature
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE}, dest=suite)
    res = h.run_std(feature=feature, repo_name=name, suite=suite)
    assert res.code == 0
    assert res.lines()[0] == f"heldout-run: {feature} round 1/4: passed"


# -- FR-025 stderr / FR-026 exit codes ---------------------------------------------------------------

USAGE_CASES = {
    "unknown_flag": ["run", "--zz-bogus-{t}"],
    "unknown_subcommand": ["zz{t}"],
    "missing_feature": ["run", "--repo-name", "demo", "--worktree", "{tree}"],
    "missing_tree": ["run", "--repo-name", "demo", "--feature", "001-demo"],
    "both_trees": ["run", "--repo-name", "demo", "--feature", "001-demo", "--worktree", "{tree}",
                   "--repo", "{tree}", "--ref", "HEAD"],
    "repo_without_ref": ["run", "--repo-name", "demo", "--feature", "001-demo",
                         "--repo", "{tree}"],
    "bad_timeout": ["run", "--repo-name", "demo", "--feature", "001-demo", "--worktree", "{tree}",
                    "--timeout", "{t}"],
    "bad_cap": ["run", "--repo-name", "demo", "--feature", "001-demo", "--worktree", "{tree}",
                "--cap", "x{t}"],
    "no_args": [],
    "rounds_reset_without_reason": ["rounds", "--repo-name", "demo", "--feature", "001-demo",
                                    "--reset"],
    "triage_without_spec": ["triage", "--repo-name", "demo", "--feature", "001-demo"],
    "check_isolation_without_suite": ["check-isolation"],
}


@pytest.mark.fr("FR-025", "FR-026")
@pytest.mark.parametrize("case", sorted(USAGE_CASES))
def test_usage_errors_print_code_only(h, tree, case):
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    t = h.token()
    args = [a.format(t=t, tree=tree) for a in USAGE_CASES[case]]
    res = h.run(*args)
    assert_refused(res, "E_USAGE", 2)
    assert t not in res.out + res.err
    assert res.out == "", "usage error printed to stdout"
    assert h.runs() == 0


@pytest.mark.fr("FR-025", "FR-026")
def test_stderr_empty_for_pass_fail(h, tree):
    h.write_suite({"test_s.py": _h7.US1_SUITE})
    assert h.run_std().err == ""
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    assert h.run_std().err == ""


@pytest.mark.fr("FR-025")
def test_error_codes_constant(mod):
    assert set(mod.ERROR_CODES) == _h7.ERROR_CODES


@pytest.mark.fr("FR-022")
def test_feedback_schema_constant(mod):
    assert mod.FEEDBACK_SCHEMA == "heldout-feedback.v1"


@pytest.mark.fr("FR-025", "FR-026")
def test_main_returns_int(mod, capsys):
    try:
        rc = mod.main(["--version"])
    except SystemExit as e:  # tolerated, but the code must be right
        rc = e.code
    assert rc == 0
    out = capsys.readouterr().out
    assert re.fullmatch(r"heldout-run \S+ sha256:[0-9a-f]{64}\n?", out)
    try:
        rc = mod.main(["run", "--zz-bogus"])
    except SystemExit as e:
        rc = e.code
    assert rc == 2
    cap = capsys.readouterr()
    assert cap.err.rstrip("\n") == "heldout-run: error E_USAGE"


# -- Python API: ID_PATTERN, summarize, format_feedback ------------------------------------------

@pytest.mark.fr("FR-019", "FR-023")
def test_id_pattern(mod):
    pat = mod.ID_PATTERN
    rx = re.compile(pat) if isinstance(pat, str) else pat
    for ok in ("FR-001", "NFR-123", "SC-007", "FR-016a", "SC-999z"):
        assert rx.fullmatch(ok), ok
    for bad in ("FR-01", "FR-0011", "fr-001", "FR-001A", "XX-001", "FR-001ab", " FR-001",
                "FR001", "UNMAPPED", "INVALID_ID", "FR-00a"):
        assert not rx.fullmatch(bad), bad


RECORDS = [
    {"nodeid": "t.py::a", "key": "a", "ids": ["FR-002"], "outcome": "failed"},
    {"nodeid": "t.py::b", "key": "b", "ids": ["FR-002"], "outcome": "passed"},
    {"nodeid": "t.py::c", "key": "c", "ids": ["FR-001", "SC-001"], "outcome": "failed"},
    {"nodeid": "t.py::d", "key": "d", "ids": [], "outcome": "failed"},
    {"nodeid": "t.py::e", "key": "e", "ids": ["FR-003"], "outcome": "skipped"},
    {"nodeid": "t.py::f", "key": "f", "ids": ["bogus zqsecret"], "outcome": "failed"},
    {"nodeid": "t.py::g", "key": "g", "ids": ["NFR-001"], "outcome": "passed"},
]
SUMMARY = {
    "schema": "heldout-feedback.v1", "feature": "001-demo", "round": 2, "round_cap": 4,
    "status": "failed", "cap_reached": False,
    "failing": {"FR-001": {"failing": 1, "total": 1}, "FR-002": {"failing": 1, "total": 2},
                "SC-001": {"failing": 1, "total": 1}, "INVALID_ID": {"failing": 1, "total": 1},
                "UNMAPPED": {"failing": 1, "total": 1}},
    "totals": {"tests": 6, "failing": 4, "skipped": 1, "ids": 6, "ids_failing": 5},
}


@pytest.mark.fr("FR-022", "FR-019", "FR-020")
def test_summarize(mod):
    out = mod.summarize(RECORDS, feature="001-demo", round=2, cap=4, status="failed")
    assert _norm(out) == SUMMARY
    assert "zqsecret" not in json.dumps(out)
    assert "t.py" not in json.dumps(out)


@pytest.mark.fr("FR-022")
def test_summarize_round_none(mod):
    out = mod.summarize(RECORDS[:2], feature="001-demo", round=None, cap=4, status="failed")
    assert out["round"] is None
    assert out["cap_reached"] is False


@pytest.mark.fr("FR-022", "FR-034")
def test_summarize_cap_reached(mod):
    out = mod.summarize(RECORDS[:2], feature="001-demo", round=4, cap=4, status="failed")
    assert out["cap_reached"] is True
    out = mod.summarize(RECORDS[1:2], feature="001-demo", round=4, cap=4, status="passed")
    assert out["cap_reached"] is False


@pytest.mark.fr("FR-021")
def test_format_feedback(mod):
    text = mod.format_feedback(SUMMARY)
    assert text.splitlines() == [
        "heldout-run: 001-demo round 2/4: failed",
        "FR-001: 1/1 failing",
        "FR-002: 1/2 failing",
        "SC-001: 1/1 failing",
        "INVALID_ID: 1/1 failing",
        "UNMAPPED: 1/1 failing",
        "total: 4/6 tests failing, 1 skipped; 5/6 ids failing",
    ]


@pytest.mark.fr("FR-021")
def test_format_feedback_order_dash_and_escalation(mod):
    fb = dict(SUMMARY)
    fb["round"] = None
    fb["failing"] = {k: {"failing": 1, "total": 1} for k in
                     ["SC-001", "FR-010", "UNMAPPED", "NFR-002", "FR-002a", "INVALID_ID", "FR-002",
                      "FR-001", "NFR-010", "SC-000"]}
    text = mod.format_feedback(fb)
    lines = text.splitlines()
    assert lines[0] == "heldout-run: 001-demo round -/4: failed"
    assert [l.split(":")[0] for l in lines[1:-1]] == [
        "FR-001", "FR-002", "FR-002a", "FR-010", "NFR-002", "NFR-010", "SC-000", "SC-001",
        "INVALID_ID", "UNMAPPED"]
    fb2 = dict(SUMMARY, round=4, cap_reached=True)
    lines2 = mod.format_feedback(fb2).splitlines()
    assert lines2[0] == "heldout-run: 001-demo round 4/4: failed"
    assert lines2[-1] == _h7.ESCALATION
    assert lines2[-2].startswith("total: ")
