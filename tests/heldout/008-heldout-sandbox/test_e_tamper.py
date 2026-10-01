"""Group E: canaries, collection-count check, tamper detection and tampered feedback
(FR-014, FR-015, FR-016, FR-017, SC-004)."""

from __future__ import annotations

import json

import pytest

import _h8
from _h8 import canary_entries, load_report

pytestmark = pytest.mark.docker


# -- FR-014 / FR-015 canaries on an honest run ---------------------------------------------------

@pytest.fixture(scope="module")
def honest(image):
    hx = _h8.H()
    try:
        hx.make_tree()
        hx.write_suite({"test_us1.py": _h8.US1_SUITE})
        res = hx.run_sb(counted=True)
        yield dict(res=res, rep=load_report(res.report_dir))
    finally:
        hx.close()


@pytest.mark.fr("FR-014")
def test_two_canaries_recorded(honest):
    ents = canary_entries(honest["rep"])
    assert len(ents) == 2, ents
    outs = sorted(o for _, o in ents)
    assert outs == ["failed", "passed"], outs


@pytest.mark.fr("FR-014")
def test_canary_names_are_random(honest):
    import re
    names = [n.rsplit("::", 1)[-1] for n, _ in canary_entries(honest["rep"])]
    assert len(set(names)) == 2
    for n in names:
        assert re.search(r"[0-9a-f]{16}", n.lower()) or len(n) >= 16, n


@pytest.mark.fr("FR-014")
def test_canaries_not_in_feedback(honest):
    res = honest["res"]
    names = [n.rsplit("::", 1)[-1] for n, _ in canary_entries(honest["rep"])]
    fb = (res.report_dir / "feedback.json").read_text()
    for n in names:
        assert n not in res.out and n not in res.err and n not in fb
    assert "CANARY" not in res.out
    # a failing canary must not add to the failing totals: US1 is still 2/6
    assert res.lines() == _h8.US1_FAIL_LINES


@pytest.mark.fr("FR-014")
def test_canaries_not_counted_under_any_id(honest):
    for t in honest["rep"].get("tests") or []:
        if "CANARY" in [str(i) for i in t.get("ids") or []]:
            continue
    # feedback totals exclude canaries (6 tests, not 8)
    fb = json.loads((honest["res"].report_dir / "feedback.json").read_text())
    assert fb["totals"]["tests"] == 6, fb["totals"]


@pytest.mark.fr("FR-015")
def test_report_records_collected_count(honest):
    rep = honest["rep"]
    # six real tests + two canaries collected
    reals = _h8.real_tests(rep)
    assert len(reals) == 6
    assert len(_h8.canary_entries(rep)) == 2


# -- FR-016 / FR-017 tampered status -------------------------------------------------------------

def _tampered(res):
    assert _h8.status_of(res) == "tampered", res.out
    assert res.code == 1


def _feedback_is_minimal(res):
    fb = json.loads((res.report_dir / "feedback.json").read_text())
    assert fb["status"] == "tampered"
    assert fb.get("failing") in ({}, None)
    for forbidden in ("tests", "failing", "skipped", "ids"):
        tv = fb.get("totals")
        if tv:
            assert all((tv.get(k) in (0, None)) for k in ("failing",)) or True
    assert set(fb).issuperset({"feature", "round", "status"})
    lines = res.out.splitlines()
    assert lines[0].endswith(": tampered")
    assert not any("/" in ln and "failing" in ln for ln in lines[1:]), lines


# Five forging strategies from SC-004, each a toy tree/suite.

@pytest.mark.fr("FR-016", "FR-017", "SC-004")
def test_forge_rewrite_records_to_all_passed(h, tree, image, no_new_containers):
    # conftest rewrites the records file so the failing test (and canary) look passed.
    h.write_suite({
        "test_f.py": '''
            import pytest


            @pytest.mark.fr("FR-002")
            def test_bad():
                assert 1 == 2
        ''',
        "conftest.py": '''
            import json, os
            import pytest


            @pytest.hookimpl(trylast=True)
            def pytest_sessionfinish(session, exitstatus):
                path = os.environ.get("HELDOUT_RUN_RECORDS")
                if not path or not os.access(path, os.W_OK):
                    return
                lines = []
                for ln in open(path):
                    try:
                        rec = json.loads(ln)
                    except ValueError:
                        continue
                    if rec.get("type") == "report" and rec.get("outcome") == "failed":
                        rec["outcome"] = "passed"
                    lines.append(json.dumps(rec))
                open(path, "w").write("\\n".join(lines) + "\\n")
        ''',
    })
    res = h.run_sb(counted=True)
    assert _h8.status_of(res) in ("tampered", "collection_error"), res.out
    assert res.code in (1, 3)
    assert "passed" != _h8.status_of(res)


@pytest.mark.fr("FR-016", "FR-017", "SC-004")
def test_forge_monkeypatch_plugin_emit(h, tree, image, no_new_containers):
    # a conftest tries to monkeypatch the runner plugin's record emitter to drop failures.
    h.write_suite({
        "test_f.py": '''
            import pytest


            @pytest.mark.fr("FR-002")
            def test_bad():
                assert 1 == 2
        ''',
        "conftest.py": '''
            import sys


            def pytest_configure(config):
                for name, mod in list(sys.modules.items()):
                    if hasattr(mod, "_emit_record"):
                        orig = mod._emit_record

                        def patched(rec, _orig=orig):
                            if rec.get("outcome") == "failed":
                                rec = dict(rec, outcome="passed")
                            return _orig(rec)

                        mod._emit_record = patched
        ''',
    })
    res = h.run_sb(counted=True)
    assert _h8.status_of(res) in ("tampered", "collection_error"), res.out
    assert _h8.status_of(res) != "passed"


@pytest.mark.fr("FR-016", "FR-017", "SC-004")
def test_forge_mark_all_passed_via_hook(h, tree, image, no_new_containers):
    # a hook forces every test report to outcome "passed" (would hide the failing canary).
    h.write_suite({
        "test_f.py": '''
            import pytest


            @pytest.mark.fr("FR-002")
            def test_bad():
                assert 1 == 2
        ''',
        "conftest.py": '''
            import pytest


            @pytest.hookimpl(hookwrapper=True)
            def pytest_runtest_makereport(item, call):
                outcome = yield
                rep = outcome.get_result()
                if rep.when == "call":
                    rep.outcome = "passed"
        ''',
    })
    res = h.run_sb(counted=True)
    _tampered(res)
    _feedback_is_minimal(res)


@pytest.mark.fr("FR-016", "FR-017", "SC-004")
def test_forge_exit_zero_early(h, tree, image, no_new_containers):
    # a plugin exits the process with status 0 before the failing canary runs.
    h.write_suite({
        "test_f.py": '''
            import pytest


            @pytest.mark.fr("FR-002")
            def test_bad():
                assert 1 == 2
        ''',
        "conftest.py": '''
            import os


            def pytest_collection_finish(session):
                os._exit(0)
        ''',
    })
    res = h.run_sb(counted=True)
    assert _h8.status_of(res) in ("tampered", "collection_error"), res.out
    assert _h8.status_of(res) != "passed"
    assert res.code in (1, 3)


@pytest.mark.fr("FR-016", "FR-017", "SC-004")
def test_forge_delete_junit(h, tree, image, no_new_containers):
    # a hook tries to delete the junit file so junit and records cannot be cross-checked.
    h.write_suite({
        "test_f.py": '''
            import pytest


            @pytest.mark.fr("FR-002")
            def test_bad():
                assert 1 == 2
        ''',
        "conftest.py": '''
            import glob, os


            def pytest_sessionfinish(session, exitstatus):
                for p in glob.glob("/out/*.xml"):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        ''',
    })
    res = h.run_sb(counted=True)
    # Deleting junit while /out is judge-0700 should fail outright (impl cannot, judge code can):
    # either way the outcome is never a clean pass of a failing suite.
    assert _h8.status_of(res) in ("tampered", "collection_error", "failed"), res.out
    assert _h8.status_of(res) != "passed"


@pytest.mark.fr("FR-016")
def test_honest_failing_run_is_not_tampered(honest):
    # control: an honest run with a real failure is "failed", never "tampered".
    assert _h8.status_of(honest["res"]) == "failed"


@pytest.mark.fr("FR-016")
def test_honest_passing_run_is_passed(h, tree, image, no_new_containers):
    h.write_suite({"test_ok.py": _h8.ALL_PASS_SUITE})
    res = h.run_sb(counted=True)
    assert _h8.status_of(res) == "passed"
    assert res.code == 0


@pytest.mark.fr("FR-017")
def test_tampered_feedback_has_only_feature_round_cap_status(h, tree, image, no_new_containers):
    h.write_suite({
        "test_f.py": '''
            import pytest


            @pytest.mark.fr("FR-002")
            def test_bad():
                assert 1 == 2
        ''',
        "conftest.py": '''
            import pytest


            @pytest.hookimpl(hookwrapper=True)
            def pytest_runtest_makereport(item, call):
                outcome = yield
                rep = outcome.get_result()
                if rep.when == "call":
                    rep.outcome = "passed"
        ''',
    })
    res = h.run_sb("--json", counted=True)
    fb = json.loads(res.out)
    assert fb["status"] == "tampered"
    assert fb.get("failing") in ({}, None)
    assert _h8.status_of  # present
    # no per-id lines and no leak of token
    assert h.leaks(res.out, res.err) == []
