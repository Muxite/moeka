"""Group B: tree copy, suite placement and cleanup (FR-006..010, SC-003)."""

from __future__ import annotations

import json
import os
import re
import signal
import time
from pathlib import Path

import pytest

import _h7
from _h7 import assert_refused


def _real(p) -> str:
    return os.path.realpath(p)


def _probe_suite(h, levels=3, dest=None):
    return h.write_suite({"test_probe.py": _h7.probe_test(levels)}, dest=dest)


# -- FR-006 --repo/--ref via git archive -----------------------------------------------------

@pytest.mark.fr("FR-006", "FR-008")
def test_repo_ref_copies_committed_state_of_ref(h):
    tree = h.make_tree({"state.txt": "v1\n"})
    h.git("tag", "v1")
    (tree / "state.txt").write_text("v2\n")
    h.git("commit", "-q", "-am", "v2")
    (tree / "state.txt").write_text("dirty\n")
    (tree / "untracked.txt").write_text("u\n")
    _probe_suite(h)
    res = h.run("run", "--profile", "generic", "--repo-name", "demo", "--feature", "001-demo",
                "--repo", tree, "--ref", "v1", "--suite", h.suite)
    assert res.code == 0, f"exit {res.code}"
    p = h.obs("probe")
    assert p["entries"].get("state.txt") == "file:v1\n"
    assert "untracked.txt" not in p["entries"]
    assert not any(Path(k).parts[0] == ".git" or ".git" in Path(k).parts for k in p["entries"])
    assert "toymod/__init__.py" in p["entries"]


@pytest.mark.fr("FR-006", "FR-028")
def test_repo_ref_records_resolved_commit_in_report(h):
    tree = h.make_tree()
    sha = h.git("rev-parse", "HEAD")
    h.git("tag", "accepted")
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    res = h.run("run", "--profile", "generic", "--repo-name", "demo", "--feature", "001-demo",
                "--repo", tree, "--ref", "accepted", "--suite", h.suite)
    assert res.code == 0
    report = json.loads((h.round_dir(1) / "report.json").read_text())
    assert sha in json.dumps(report["meta"]), "resolved commit id not in report meta"


@pytest.mark.fr("FR-006")
def test_bad_ref_is_e_git(h, tree):
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    res = h.run("run", "--profile", "generic", "--repo-name", "demo", "--feature", "001-demo",
                "--repo", tree, "--ref", "no-such-ref-" + h.token(), "--suite", h.suite)
    assert_refused(res, "E_GIT", 2)
    assert h.runs() == 0
    assert h.counted() == 0


@pytest.mark.fr("FR-006")
def test_repo_that_is_not_git_is_e_git(h):
    plain = _h7.mkd(h.base / "plain")
    (plain / "x.txt").write_text("x")
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    res = h.run("run", "--profile", "generic", "--repo-name", "demo", "--feature", "001-demo",
                "--repo", plain, "--ref", "HEAD", "--suite", h.suite)
    assert_refused(res, "E_GIT", 2)
    assert h.runs() == 0


# -- FR-007 --worktree filtered copy ---------------------------------------------------------

@pytest.mark.fr("FR-007", "FR-008")
def test_worktree_copy_contents_and_exclusions(h):
    tree = h.make_tree({"committed.txt": "c\n", "modified.txt": "orig\n"})
    (tree / "modified.txt").write_text("changed\n")
    (tree / "untracked.txt").write_text("new\n")
    (tree / "link_rel").symlink_to("toymod/__init__.py")
    (tree / "dirlink").symlink_to("toymod")
    for rel in (".venv/bin/x", "node_modules/pkg/i.js", "sub/__pycache__/m.cpython.pyc",
                ".pytest_cache/v", ".mypy_cache/c", ".ruff_cache/r", "nested/.git/HEAD",
                "deep/a/node_modules/z.txt"):
        _h7.write(tree / rel, "excluded\n", 0o644)
    _h7.write(tree / "stray.pyc", "x", 0o644)
    _h7.write(tree / "sub" / "keep.py", "KEEP = 1\n", 0o644)
    _h7.write(tree / "sub" / "keep.pyc.txt", "keep\n", 0o644)
    _probe_suite(h)
    res = h.run_std()
    assert res.code == 0, f"exit {res.code}"
    e = h.obs("probe")["entries"]
    assert e.get("committed.txt") == "file:c\n"
    assert e.get("modified.txt") == "file:changed\n"
    assert e.get("untracked.txt") == "file:new\n"
    assert e.get("link_rel") == "link:toymod/__init__.py", "file symlink not preserved"
    assert e.get("dirlink") == "link:toymod", "dir symlink not preserved"
    assert e.get("sub/keep.py") == "file:KEEP = 1\n"
    assert "sub/keep.pyc.txt" in e
    excluded = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                "node_modules"}
    for rel in e:
        parts = Path(rel).parts
        assert not excluded & set(parts), "excluded component copied"
        assert not rel.endswith(".pyc"), ".pyc copied"


@pytest.mark.fr("FR-008")
def test_copy_is_under_tmpdir_by_default_and_outside_everything(h, tree):
    _probe_suite(h)
    res = h.run_std()
    assert res.code == 0
    copy = Path(_real(h.obs("probe")["copy"]))
    assert Path(_real(h.tmp)) in copy.parents
    for p in (h.tree, h.suite, h.root):
        q = Path(_real(p))
        assert copy != q and q not in copy.parents


@pytest.mark.fr("FR-008", "FR-010")
def test_scratch_option_places_copy(h, tree):
    scratch = _h7.mkd(h.base / "scratch")
    _probe_suite(h)
    res = h.run_std("--scratch", scratch)
    assert res.code == 0
    copy = Path(_real(h.obs("probe")["copy"]))
    assert Path(_real(scratch)) in copy.parents
    assert os.listdir(scratch) == [], "scratch not cleaned"
    assert h.leftovers() == []


@pytest.mark.fr("FR-008", "SC-003")
def test_scratch_inside_tree_never_puts_copy_in_tree(h, tree):
    _probe_suite(h)
    _h7.mkd(tree / "scratch-here")
    before = _h7.snapshot(tree)
    res = h.run_std("--scratch", tree / "scratch-here")
    if res.code == 0:
        copy = Path(_real(h.obs("probe")["copy"]))
        assert Path(_real(tree)) not in copy.parents
    else:
        assert res.code in (2, 3), "neither ran outside the tree nor refused"
        assert h.obs("probe") is None
        assert len(res.err.splitlines()) == 1
    assert _h7.snapshot(tree) == before


# -- FR-009 suite placement ---------------------------------------------------------------------

@pytest.mark.fr("FR-009")
def test_suite_copied_to_tests_heldout_feature(h, tree):
    _probe_suite(h)
    res = h.run_std()
    assert res.code == 0
    p = h.obs("probe")
    assert _real(p["suite_dir"]) == _real(Path(p["copy"]) / "tests" / "heldout" / "001-demo")


@pytest.mark.fr("FR-009")
def test_existing_tests_heldout_feature_in_tree_is_replaced(h):
    stale = '''
import pytest

@pytest.mark.fr("FR-099")
def test_stale():
    assert False
'''
    h.make_tree({"tests/heldout/001-demo/test_stale.py": stale,
                 "tests/heldout/001-demo/conftest.py": "raise RuntimeError('stale')\n",
                 "tests/heldout/other/keep.txt": "keep\n"})
    _probe_suite(h)
    res = h.run_std()
    assert res.code == 0, f"exit {res.code}"
    assert "FR-099" not in res.out
    e = h.obs("probe")["entries"]
    assert "tests/heldout/001-demo/test_stale.py" not in e
    assert "tests/heldout/001-demo/conftest.py" not in e
    assert "tests/heldout/001-demo/test_probe.py" in e
    assert "tests/heldout/other/keep.txt" in e


@pytest.mark.fr("FR-009")
def test_suite_dest_option(h, tree):
    _probe_suite(h, levels=2)
    res = h.run_std("--suite-dest", "custom/place")
    assert res.code == 0
    p = h.obs("probe")
    assert _real(p["suite_dir"]) == _real(Path(p["copy"]) / "custom" / "place")
    assert not any(k.startswith("tests/heldout/001-demo") for k in p["entries"])


@pytest.mark.fr("FR-009", "SC-003")
@pytest.mark.parametrize("kind", ["pass", "fail", "collection_error"])
def test_suite_source_and_tree_unchanged(h, tree, kind):
    files = {
        "pass": {"test_s.py": _h7.ALL_PASS_SUITE},
        "fail": {"test_s.py": _h7.US1_SUITE, "SPEC-MAP.json": "{}",
                 "conftest.py": "import pytest\n\n@pytest.fixture\ndef fx():\n    return 1\n"},
        "collection_error": {"test_s.py": "import no_such_module_zz\n"},
    }[kind]
    h.write_suite(files)
    (tree / "dirty.txt").write_text("dirty\n")
    s_before = _h7.snapshot(h.suite)
    t_before = _h7.snapshot(tree)
    res = h.run_std()
    assert res.code in (0, 1, 3)
    assert _h7.snapshot(h.suite) == s_before, "suite source modified"
    assert _h7.snapshot(tree) == t_before, "tree under test modified"
    assert h.leftovers() == []


# -- FR-010 cleanup ------------------------------------------------------------------------------

@pytest.mark.fr("FR-010", "SC-003")
@pytest.mark.parametrize("kind", ["pass", "fail", "collection_error", "refusal"])
def test_scratch_removed_after_run(h, tree, kind):
    files = {
        "pass": {"test_s.py": _h7.ALL_PASS_SUITE},
        "fail": {"test_s.py": _h7.ONE_FAIL_SUITE},
        "collection_error": {"test_s.py": "raise ImportError('boom')\n"},
        "refusal": {"test_s.py": _h7.ONE_FAIL_SUITE},
    }[kind]
    h.write_suite(files)
    scratch = _h7.mkd(h.base / "scratch")
    if kind == "refusal":
        os.chmod(h.suite, 0o755)
    res = h.run_std("--scratch", scratch)
    assert res.code in {"pass": (0,), "fail": (1,), "collection_error": (3,),
                        "refusal": (2,)}[kind]
    assert os.listdir(scratch) == []
    assert h.leftovers() == []


@pytest.mark.slow
@pytest.mark.fr("FR-010", "FR-013", "SC-003")
def test_scratch_removed_after_timeout(h, tree):
    h.write_suite({"test_s.py": '''
@pytest.mark.fr("FR-001")
def test_hang():
    _mark_run()
    _time.sleep(3600)
'''})
    res = h.run_std("--timeout", "2")
    assert res.code == 3
    assert h.leftovers() == []


@pytest.mark.fr("FR-010", "FR-014")
def test_scratch_removed_after_sync_failure(h, tree):
    h.fake_uv()
    h.env["FAKE_UV_SYNC"] = "fail"
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    res = h.run_std(profile="moeka")
    assert res.code == 3
    assert h.leftovers() == []


HANG_SUITE = '''
@pytest.mark.fr("FR-001")
def test_hang():
    import subprocess
    child = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(3600)"])
    _obs("hang", {"pid": _os.getpid(), "child": child.pid})
    _time.sleep(3600)
'''


@pytest.mark.slow
@pytest.mark.fr("FR-010", "SC-003")
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_cleanup_on_signal(h, tree, sig):
    h.write_suite({"test_s.py": HANG_SUITE})
    scratch = _h7.mkd(h.base / "scratch")
    t_before = _h7.snapshot(tree)
    p = h.spawn(*h.std("--scratch", scratch, "--timeout", "300"))
    assert _h7.wait_until(lambda: h.obs("hang") is not None, 60), "suite never started"
    info = h.obs("hang")
    os.kill(p.pid, sig)
    try:
        out, err = p.communicate(timeout=40)
    except Exception:
        pytest.fail("runner did not exit after the signal")
    assert _h7.wait_until(lambda: not _h7.proc_alive(info["pid"]), 15), "pytest left running"
    assert _h7.wait_until(lambda: not _h7.proc_alive(info["child"]), 15), "test child left"
    assert _h7.wait_until(lambda: os.listdir(scratch) == [], 10), "scratch left behind"
    assert h.leftovers() == []
    assert _h7.snapshot(tree) == t_before
    assert not h.leaks(out, err)


@pytest.mark.fr("FR-010")
def test_keep_keeps_copy_and_reports_path_privately(h, tree):
    scratch = _h7.mkd(h.base / "scratch")
    _probe_suite(h)
    res = h.run_std("--scratch", scratch, "--keep", "--json")
    assert res.code == 0
    copy = Path(h.obs("probe")["copy"])
    assert copy.is_dir(), "--keep did not keep the copy"
    assert (copy / "toymod" / "__init__.py").is_file()
    assert str(copy) not in res.out and str(copy) not in res.err
    assert not h.leaks(res.out, res.err)
    private = ""
    for f in h.round_dir(1).rglob("*"):
        if f.is_file():
            private += f.read_text(errors="replace")
    assert str(copy) in private or _real(copy) in private, "kept copy path not in private report"
