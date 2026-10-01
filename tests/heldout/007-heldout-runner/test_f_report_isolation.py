"""Group F: private report and isolation checks (FR-027..031, SC-003)."""

from __future__ import annotations

import json
import os
import re
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import _h7
from _h7 import assert_refused

TEST_KEYS = {"nodeid", "key", "ids", "outcome", "when", "duration_s", "longrepr"}

PRINTING_SUITE = '''
@pytest.mark.fr("FR-001")
def test_prints():
    print("PRINTED-{tok}")
    _sys.stderr.write("STDERR-{tok}\\n")


@pytest.mark.fr("FR-002")
def test_fails():
    assert 1 == 2, "MSG-{tok}"
'''


def _modes_ok(root: Path) -> list[str]:
    bad = []
    for dp, dns, fns in os.walk(root):
        for n in dns:
            m = stat.S_IMODE(os.lstat(Path(dp) / n).st_mode)
            if m != 0o700:
                bad.append(f"dir {oct(m)}")
        for n in fns:
            m = stat.S_IMODE(os.lstat(Path(dp) / n).st_mode)
            if m != 0o600:
                bad.append(f"file {n} {oct(m)}")
    m = stat.S_IMODE(os.lstat(root).st_mode)
    if m != 0o700:
        bad.append(f"root {oct(m)}")
    return bad


# -- FR-027 layout and modes ---------------------------------------------------------------------

@pytest.mark.fr("FR-027")
def test_counted_rounds_layout(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std()
    h.run_std()
    assert h.round_dir(1).is_dir()
    assert h.round_dir(2).is_dir()
    assert (h.report_root / "rounds.json").is_file()


@pytest.mark.fr("FR-027")
def test_no_count_run_dir_name(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std("--no-count")
    names = [p.name for p in h.report_root.iterdir() if p.is_dir()]
    assert len(names) == 1
    assert re.fullmatch(r"run-\d{8}T\d{6}Z", names[0]), "no-count dir is not run-<UTC ts>"
    assert not h.round_dir(1).exists()


@pytest.mark.fr("FR-027")
def test_report_modes(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std()
    h.run_std("--no-count")
    assert _modes_ok(h.root / "_reports") == []


@pytest.mark.fr("FR-027", "FR-028")
def test_report_dir_option(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    rd = h.base / "private" / "rep"
    _h7.mkd(rd.parent)
    res = h.run_std("--report-dir", rd)
    assert res.code == 1
    for f in ("report.json", "junit.xml", "pytest.log", "feedback.json"):
        assert (rd / f).is_file(), f
        assert stat.S_IMODE(os.stat(rd / f).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(rd).st_mode) == 0o700
    assert not h.round_dir(1).exists()


@pytest.mark.fr("FR-027", "FR-028")
def test_default_root_from_home(h, tree):
    env = dict(h.env)
    del env["HELDOUT_ROOT"]
    root = h.home / "projects" / ".heldout"
    _h7.mkd(root)
    suite = root / "demo" / "001-demo"
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE}, dest=suite)
    res = h.run(*h.std(suite=suite), env=env)
    assert res.code == 1
    assert (root / "_reports" / "demo" / "001-demo" / "round-01" / "report.json").is_file()
    assert (root / "_reports" / "demo" / "001-demo" / "rounds.json").is_file()


@pytest.mark.fr("FR-027")
def test_heldout_root_option_beats_env(h, tree):
    other = _h7.mkd(h.base / "other-root")
    suite = other / "demo" / "001-demo"
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE}, dest=suite)
    res = h.run_std("--heldout-root", other, suite=suite)
    assert res.code == 1
    assert (other / "_reports" / "demo" / "001-demo" / "round-01").is_dir()
    assert not (h.root / "_reports").exists()


@pytest.mark.fr("FR-018", "FR-027")
def test_default_suite_location(h, tree):
    h.write_suite({"test_s.py": _h7.US1_SUITE})
    args = [a for a in h.std() if a not in ("--suite", str(h.suite))]
    res = h.run(*args)
    assert res.code == 1
    assert res.lines() == _h7.US1_FAIL_LINES


# -- FR-028 contents -----------------------------------------------------------------------------

@pytest.mark.fr("FR-028")
def test_report_contents(h, tree):
    tok = h.token()
    h.write_suite({"test_s.py": PRINTING_SUITE.replace("{tok}", tok)})
    res = h.run_std("--json")
    rd = h.round_dir(1)
    for f in ("report.json", "junit.xml", "pytest.log", "feedback.json"):
        assert (rd / f).is_file(), f
    report = json.loads((rd / "report.json").read_text())
    assert "heldout-report.v1" in json.dumps(report)
    meta = report["meta"]
    blob = json.dumps(meta)
    assert _h7.sha256_file(_h7.RUNNER) in blob, "runner sha256 not in meta"
    assert meta.get("status") == "failed"
    assert "001-demo" in blob and "demo" in blob
    tests = report["tests"]
    assert len(tests) == 2
    for t in tests:
        assert TEST_KEYS <= set(t), "test record keys"
    failing = [t for t in tests if t["outcome"] == "failed"]
    assert len(failing) == 1
    f = failing[0]
    assert f["key"] == "test_fails"
    assert "test_fails" in f["nodeid"]
    assert list(f["ids"]) == ["FR-002"]
    assert f"MSG-{tok}" in (f["longrepr"] or "")
    log = (rd / "pytest.log").read_text()
    assert f"PRINTED-{tok}" in log or f"MSG-{tok}" in log, "pytest output not kept privately"
    assert json.loads((rd / "feedback.json").read_text()) == res.json()
    cases = list(ET.parse(rd / "junit.xml").getroot().iter("testcase"))
    assert len(cases) == 2


@pytest.mark.fr("FR-028", "FR-024")
def test_private_report_gets_what_feedback_hides(h, tree):
    tok = h.token()
    h.write_suite({"test_s.py": PRINTING_SUITE.replace("{tok}", tok)})
    res = h.run_std()
    assert tok not in res.out + res.err
    private = "".join(p.read_text(errors="replace") for p in h.round_dir(1).iterdir()
                      if p.is_file())
    assert tok in private


@pytest.mark.fr("FR-028")
def test_report_suite_sha_changes_with_suite(h, tree):
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE})
    h.run_std()
    h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE + "\n# changed\n"})
    h.run_std()
    m1 = json.loads((h.round_dir(1) / "report.json").read_text())["meta"]
    m2 = json.loads((h.round_dir(2) / "report.json").read_text())["meta"]
    hexes1 = set(re.findall(r"[0-9a-f]{64}", json.dumps(m1)))
    hexes2 = set(re.findall(r"[0-9a-f]{64}", json.dumps(m2)))
    assert _h7.sha256_file(_h7.RUNNER) in hexes1 & hexes2
    assert hexes1 != hexes2, "suite sha256 does not change with the suite"


# -- FR-029 placement --------------------------------------------------------------------------------

def _suite_at(h, path: Path) -> Path:
    return h.write_suite({"test_s.py": _h7.ONE_FAIL_SUITE}, dest=path)


@pytest.mark.fr("FR-029", "SC-003")
def test_suite_inside_worktree_refused(h, tree):
    suite = _suite_at(h, tree / "hidden" / "001-demo")
    res = h.run_std(suite=suite)
    assert_refused(res, "E_SUITE_IN_TREE", 2)
    assert h.runs() == 0 and h.leftovers() == [] and h.counted() == 0


@pytest.mark.fr("FR-029")
def test_suite_inside_repo_ref_refused(h, tree):
    suite = _suite_at(h, tree / "hidden")
    res = h.run("run", "--profile", "generic", "--repo-name", "demo", "--feature", "001-demo",
                "--repo", tree, "--ref", "HEAD", "--suite", suite)
    assert_refused(res, "E_SUITE_IN_TREE", 2)
    assert h.runs() == 0


@pytest.mark.fr("FR-029")
def test_suite_in_other_linked_worktree_refused(h, tree):
    wt = h.base / "linked"
    h.git("worktree", "add", "-q", str(wt), "-b", "side")
    suite = _suite_at(h, wt / "hidden")
    res = h.run_std(suite=suite)
    assert_refused(res, "E_SUITE_IN_TREE", 2)
    res2 = h.run("run", "--profile", "generic", "--repo-name", "demo", "--feature", "001-demo",
                 "--repo", tree, "--ref", "HEAD", "--suite", suite)
    assert_refused(res2, "E_SUITE_IN_TREE", 2)
    assert h.runs() == 0


@pytest.mark.fr("FR-029")
def test_suite_in_main_worktree_when_testing_linked_worktree_refused(h, tree):
    wt = h.base / "linked"
    h.git("worktree", "add", "-q", str(wt), "-b", "side")
    suite = _suite_at(h, tree / "elsewhere")
    res = h.run_std(tree=wt, suite=suite)
    assert_refused(res, "E_SUITE_IN_TREE", 2)
    assert h.runs() == 0


@pytest.mark.fr("FR-029")
def test_suite_through_symlink_into_tree_refused(h, tree):
    real = _suite_at(h, tree / "hidden")
    link = h.base / "suite-link"
    link.symlink_to(real)
    res = h.run_std(suite=link)
    assert_refused(res, "E_SUITE_IN_TREE", 2)


@pytest.mark.fr("FR-029")
def test_suite_under_forbid_under_refused(h, tree):
    forbid = _h7.mkd(h.base / "mounts")
    suite = _suite_at(h, forbid / "s")
    res = h.run_std("--forbid-under", h.base / "unrelated", "--forbid-under", forbid, suite=suite)
    assert_refused(res, "E_SUITE_IN_TREE", 2)
    assert h.runs() == 0 and h.leftovers() == []


@pytest.mark.fr("FR-029")
def test_report_dir_inside_tree_refused(h, tree):
    _suite_at(h, h.suite)
    res = h.run_std("--report-dir", tree / "rep")
    assert_refused(res, "E_REPORT_IN_TREE", 2)
    assert not (tree / "rep").exists()
    assert h.runs() == 0


@pytest.mark.fr("FR-029")
def test_report_dir_in_linked_worktree_refused(h, tree):
    wt = h.base / "linked"
    h.git("worktree", "add", "-q", str(wt), "-b", "side")
    _suite_at(h, h.suite)
    res = h.run_std("--report-dir", wt / "rep")
    assert_refused(res, "E_REPORT_IN_TREE", 2)
    assert not (wt / "rep").exists()


@pytest.mark.fr("FR-029")
def test_report_dir_under_forbid_under_refused(h, tree):
    forbid = _h7.mkd(h.base / "mounts")
    _suite_at(h, h.suite)
    res = h.run_std("--report-dir", forbid / "rep", "--forbid-under", forbid)
    assert_refused(res, "E_REPORT_IN_TREE", 2)


@pytest.mark.fr("FR-029")
def test_heldout_root_inside_tree_refused(h, tree):
    root = _h7.mkd(tree / "dot-heldout")
    suite = _suite_at(h, h.base / "outside-suite")
    res = h.run_std("--heldout-root", root, suite=suite)
    assert res.code == 2
    assert res.err.rstrip("\n") in (_h7.err_line("E_SUITE_IN_TREE"),
                                    _h7.err_line("E_REPORT_IN_TREE"))
    assert not (root / "_reports").exists()
    assert h.runs() == 0


@pytest.mark.fr("FR-029")
def test_suite_outside_everything_accepted(h, tree):
    wt = h.base / "linked"
    h.git("worktree", "add", "-q", str(wt), "-b", "side")
    suite = _suite_at(h, h.base / "outside" / "s")
    res = h.run_std("--forbid-under", h.base / "mounts", suite=suite)
    assert res.code == 1


# -- FR-030 check-isolation ---------------------------------------------------------------------------

@pytest.mark.fr("FR-030")
def test_check_isolation_ok(h, tree):
    _suite_at(h, h.suite)
    res = h.run("check-isolation", "--suite", h.suite, "--repo", tree,
                "--forbid-under", h.base / "mounts")
    assert res.code == 0
    assert res.err == ""
    assert h.leftovers() == []
    assert not (h.root / "_reports").exists()
    assert h.runs() == 0


@pytest.mark.fr("FR-030")
def test_check_isolation_without_repo(h):
    _suite_at(h, h.suite)
    res = h.run("check-isolation", "--suite", h.suite)
    assert res.code == 0


@pytest.mark.fr("FR-030")
def test_check_isolation_suite_in_repo(h, tree):
    suite = _suite_at(h, tree / "hidden")
    res = h.run("check-isolation", "--suite", suite, "--repo", tree)
    assert_refused(res, "E_SUITE_IN_TREE", 2)


@pytest.mark.fr("FR-030")
def test_check_isolation_suite_in_linked_worktree(h, tree):
    wt = h.base / "linked"
    h.git("worktree", "add", "-q", str(wt), "-b", "side")
    suite = _suite_at(h, wt / "hidden")
    res = h.run("check-isolation", "--suite", suite, "--repo", tree)
    assert_refused(res, "E_SUITE_IN_TREE", 2)


@pytest.mark.fr("FR-030")
def test_check_isolation_forbid_under(h, tree):
    forbid = _h7.mkd(h.base / "mounts")
    suite = _suite_at(h, forbid / "s")
    res = h.run("check-isolation", "--suite", suite, "--forbid-under", forbid)
    assert_refused(res, "E_SUITE_IN_TREE", 2)


@pytest.mark.fr("FR-030", "FR-031")
def test_check_isolation_permissions(h, tree):
    _suite_at(h, h.suite)
    os.chmod(h.suite, 0o750)
    res = h.run("check-isolation", "--suite", h.suite)
    assert_refused(res, "E_SUITE_PERMS", 2)
    os.chmod(h.suite, 0o700)
    os.chmod(h.root, 0o711)
    res = h.run("check-isolation", "--suite", h.suite)
    assert_refused(res, "E_SUITE_PERMS", 2)


@pytest.mark.fr("FR-030", "FR-031")
def test_check_isolation_missing_suite(h):
    res = h.run("check-isolation", "--suite", h.base / "nope")
    assert_refused(res, "E_SUITE_MISSING", 2)


# -- FR-031 permissions ---------------------------------------------------------------------------------

@pytest.mark.fr("FR-031")
@pytest.mark.parametrize("mode", [0o755, 0o750, 0o701, 0o710, 0o707])
def test_suite_mode_refused(h, tree, mode):
    _suite_at(h, h.suite)
    os.chmod(h.suite, mode)
    res = h.run_std()
    assert_refused(res, "E_SUITE_PERMS", 2)
    assert h.runs() == 0 and h.leftovers() == []


@pytest.mark.fr("FR-031")
@pytest.mark.parametrize("mode", [0o755, 0o750, 0o705])
def test_heldout_root_mode_refused(h, tree, mode):
    _suite_at(h, h.suite)
    os.chmod(h.root, mode)
    res = h.run_std()
    assert_refused(res, "E_SUITE_PERMS", 2)
    assert h.runs() == 0


@pytest.mark.fr("FR-031")
def test_missing_suite(h, tree):
    res = h.run_std(suite=h.base / "missing-suite")
    assert_refused(res, "E_SUITE_MISSING", 2)
    import shutil
    shutil.rmtree(h.suite)
    args = [a for a in h.std() if a not in ("--suite", str(h.suite))]
    res2 = h.run(*args)
    assert_refused(res2, "E_SUITE_MISSING", 2)
