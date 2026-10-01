"""Group G: round state, counting, cap, rounds subcommand, reset (FR-032..035, SC-006).

Default for 007-Q1 (tester-bug rounds): a round counts unless the head resets with
`--reason tester-bug`; resets keep the history.
"""

from __future__ import annotations

import json
import os

import pytest

import _h7
from _h7 import ESCALATION, assert_refused


def _rounds_cli(h):
    return h.run("rounds", "--repo-name", "demo", "--feature", "001-demo")


# -- FR-032 rounds.json ------------------------------------------------------------------------

@pytest.mark.fr("FR-032")
def test_rounds_json_schema(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std()
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    h.run_std()
    data = h.rounds_json()
    assert data["schema"] == "heldout-rounds.v1"
    assert data["cap"] == 4
    assert [r["n"] for r in data["rounds"]] == [1, 2]
    for r in data["rounds"]:
        assert {"n", "started_at", "status", "failing_ids", "tree"} <= set(r)
    assert data["rounds"][0]["status"] == "failed"
    assert list(data["rounds"][0]["failing_ids"]) == ["FR-002"]
    assert data["rounds"][1]["status"] == "passed"
    assert list(data["rounds"][1]["failing_ids"]) == []
    assert data["resets"] == []


# -- FR-033 what counts ----------------------------------------------------------------------------

@pytest.mark.fr("FR-033")
def test_passed_failed_collection_error_count(h, tree):
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    h.run_std()
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std()
    h.write_suite({"test_s.py": "import no_such_mod_zz\n"})
    h.run_std()
    assert [r["status"] for r in h.rounds_json()["rounds"]] == ["passed", "failed",
                                                               "collection_error"]


@pytest.mark.slow
@pytest.mark.fr("FR-033")
def test_timeout_counts(h, tree):
    h.write_suite({"test_s.py": '''
@pytest.mark.fr("FR-001")
def test_hang():
    _time.sleep(3600)
'''})
    res = h.run_std("--timeout", "1")
    assert res.code == 3
    assert [r["status"] for r in h.rounds_json()["rounds"]] == ["timeout"]


@pytest.mark.fr("FR-033")
def test_no_count_refusal_infra_and_triage_do_not_count(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    assert h.run_std().code == 1
    assert h.counted() == 1
    assert h.run_std("--no-count").code == 1
    os.chmod(h.suite, 0o755)
    assert h.run_std().code == 2
    os.chmod(h.suite, 0o700)
    assert h.run_std(feature="BAD").code == 2
    h.fake_uv()
    h.env["FAKE_UV_SYNC"] = "fail"
    assert h.run_std(profile="moeka").code == 3
    spec = _h7.write(h.base / "spec.md", "- **FR-002**: text\n")
    h.run("triage", "--repo-name", "demo", "--feature", "001-demo", "--spec", spec)
    assert h.counted() == 1
    res = h.run_std()
    assert res.lines()[0] == "heldout-run: 001-demo round 2/4: failed"


# -- FR-034 cap --------------------------------------------------------------------------------------

@pytest.mark.fr("FR-034", "SC-006")
def test_cap_four_escalates_then_refuses(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    for r in (1, 2, 3):
        res = h.run_std()
        assert res.code == 1
        assert res.lines()[0] == f"heldout-run: 001-demo round {r}/4: failed"
        assert ESCALATION not in res.out
    res = h.run_std()
    assert res.code == 1
    assert res.lines()[0] == "heldout-run: 001-demo round 4/4: failed"
    assert res.lines()[-1] == ESCALATION
    assert res.lines()[-2].startswith("total: ")
    assert h.runs() == 4
    fifth = h.run_std()
    assert_refused(fifth, "E_ROUND_CAP", 4)
    assert h.runs() == 4, "tests ran on the refused fifth round"
    assert h.counted() == 4
    assert not h.round_dir(5).exists()
    assert h.leftovers() == []


@pytest.mark.fr("FR-034", "FR-022")
def test_cap_reached_in_json(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    for _ in range(3):
        assert h.run_std("--json").json()["cap_reached"] is False
    fb = h.run_std("--json").json()
    assert fb["round"] == 4 and fb["round_cap"] == 4 and fb["cap_reached"] is True
    assert json.loads((h.round_dir(4) / "feedback.json").read_text())["cap_reached"] is True


@pytest.mark.fr("FR-034")
def test_passing_last_round_does_not_escalate(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std("--cap", "2")
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    res = h.run_std("--cap", "2", "--json")
    fb = res.json()
    assert fb["status"] == "passed" and fb["cap_reached"] is False
    res2 = h.run_std("--cap", "2")
    assert_refused(res2, "E_ROUND_CAP", 4)


@pytest.mark.fr("FR-034")
def test_custom_cap(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    a = h.run_std("--cap", "2")
    assert a.lines()[0] == "heldout-run: 001-demo round 1/2: failed"
    b = h.run_std("--cap", "2")
    assert b.lines()[0] == "heldout-run: 001-demo round 2/2: failed"
    assert b.lines()[-1] == ESCALATION
    c = h.run_std("--cap", "2")
    assert_refused(c, "E_ROUND_CAP", 4)
    assert h.runs() == 2


@pytest.mark.fr("FR-034", "SC-006")
def test_no_count_allowed_at_cap_and_never_changes_count(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std("--cap", "1")
    before = (h.report_root / "rounds.json").read_bytes()
    res = h.run_std("--cap", "1", "--no-count")
    assert res.code == 1
    assert res.lines()[0] == "heldout-run: 001-demo round -/1: failed"
    assert ESCALATION not in res.out
    assert json.loads((h.report_root / "rounds.json").read_bytes()) == json.loads(before)


@pytest.mark.fr("FR-034")
def test_cap_refusal_before_copy(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std("--cap", "1")
    scratch = _h7.mkd(h.base / "scratch")
    res = h.run_std("--cap", "1", "--scratch", scratch)
    assert_refused(res, "E_ROUND_CAP", 4)
    assert os.listdir(scratch) == []
    assert h.runs() == 1


# -- FR-035 rounds subcommand and reset ----------------------------------------------------------

@pytest.mark.fr("FR-035")
def test_rounds_listing(h, tree):
    multi = '''
@pytest.mark.fr("FR-003", "SC-001")
def test_m():
    assert False


@pytest.mark.fr("FR-001")
def test_n():
    assert False
'''
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std()
    h.write_suite({"test_s.py": multi})
    h.run_std()
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    h.run_std()
    res = _rounds_cli(h)
    assert res.code == 0
    lines = [l.rstrip() for l in res.lines()]
    assert len(lines) == 3
    assert lines[0] == "round 1: failed FR-002"
    head, *ids = lines[1].split()
    assert lines[1].startswith("round 2: failed ")
    assert set(lines[1].split()[3:]) == {"FR-001", "FR-003", "SC-001"}
    assert lines[2] == "round 3: passed"


@pytest.mark.fr("FR-035")
def test_rounds_listing_empty(h):
    res = _rounds_cli(h)
    assert res.code == 0
    assert res.out.strip() == ""


@pytest.mark.fr("FR-035")
def test_rounds_listing_never_echoes_invalid_ids(h, tree):
    t = h.token()
    h.write_suite({"test_s.py": f'''
@pytest.mark.fr("free {t}")
def test_x():
    assert False
'''})
    h.run_std()
    res = _rounds_cli(h)
    assert t not in res.out
    assert res.lines()[0].rstrip() == "round 1: failed INVALID_ID"


@pytest.mark.fr("FR-035")
def test_reset_moves_rounds_and_restarts(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std()
    h.run_std()
    old = h.rounds_json()["rounds"]
    r = h.run("rounds", "--repo-name", "demo", "--feature", "001-demo", "--reset",
              "--reason", "tester-bug")
    assert r.code == 0
    data = h.rounds_json()
    assert data["rounds"] == []
    assert len(data["resets"]) == 1
    reset = data["resets"][0]
    assert {"at", "reason", "rounds"} <= set(reset)
    assert reset["reason"] == "tester-bug"
    assert reset["rounds"] == old
    res = h.run_std()
    assert res.lines()[0] == "heldout-run: 001-demo round 1/4: failed"
    assert _rounds_cli(h).lines()[0].rstrip() == "round 1: failed FR-002"


@pytest.mark.fr("FR-035")
def test_reset_history_kept_across_resets(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std()
    h.run("rounds", "--repo-name", "demo", "--feature", "001-demo", "--reset", "--reason",
          "tester-bug")
    h.run_std()
    h.run_std()
    h.run("rounds", "--repo-name", "demo", "--feature", "001-demo", "--reset", "--reason",
          "spec changed")
    data = h.rounds_json()
    assert [x["reason"] for x in data["resets"]] == ["tester-bug", "spec changed"]
    assert [len(x["rounds"]) for x in data["resets"]] == [1, 2]
    assert data["rounds"] == []


@pytest.mark.fr("FR-035", "FR-034")
def test_reset_lifts_cap(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std("--cap", "1")
    assert h.run_std("--cap", "1").code == 4
    h.run("rounds", "--repo-name", "demo", "--feature", "001-demo", "--reset", "--reason",
          "tester-bug")
    res = h.run_std("--cap", "1")
    assert res.code == 1
    assert res.lines()[0] == "heldout-run: 001-demo round 1/1: failed"


@pytest.mark.fr("FR-035")
def test_reset_requires_reason(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std()
    before = (h.report_root / "rounds.json").read_bytes()
    r = h.run("rounds", "--repo-name", "demo", "--feature", "001-demo", "--reset")
    assert_refused(r, "E_USAGE", 2)
    assert (h.report_root / "rounds.json").read_bytes() == before


@pytest.mark.fr("FR-035")
def test_tester_bug_round_counts_until_reset(h, tree):
    """007-Q1 default: a failing round caused by a tester bug still counts."""
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std("--cap", "2")
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    res = h.run_std("--cap", "2")
    assert res.lines()[0] == "heldout-run: 001-demo round 2/2: passed"


@pytest.mark.fr("FR-035")
def test_rounds_bad_names(h):
    r = h.run("rounds", "--repo-name", "demo", "--feature", "Bad")
    assert_refused(r, "E_FEATURE", 2)
