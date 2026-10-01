"""Group G: feedback parity between sandboxed and unsandboxed runs, performance, and parity on
the accepted suites (SC-001, SC-002, SC-006, FR-019)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import _h8
from _h8 import load_report

REPO = _h8.REPO


def _run_both(h, suite_files, *, tree_files=None, extra=(), counted=False):
    """Run the same toy suite unsandboxed then sandboxed on identical fresh trees; return both
    Results."""
    out = {}
    for mode in ("none", "docker"):
        hx = _h8.H()
        try:
            hx.make_tree(tree_files)
            hx.write_suite(suite_files)
            res = hx.run_sb(*extra, sandbox=(mode == "docker"), counted=counted)
            out[mode] = (res.code, res.out, res.err, _h8.status_of(res))
        finally:
            if mode == "none":
                hx.close()
            else:
                out["_h"] = hx  # keep for timing if needed; closed by caller
                hx.close()
    return out


# -- SC-001 identical feedback -------------------------------------------------------------------

@pytest.mark.docker
@pytest.mark.fr("SC-001", "FR-019")
@pytest.mark.parametrize("name,suite,tree_files", [
    ("all_pass", {"test_x.py": _h8.ALL_PASS_SUITE}, None),
    ("one_failing", {"test_us1.py": _h8.US1_SUITE}, None),
    ("collection_error", {"test_x.py": _h8.COLLECTION_ERROR_SUITE}, None),
])
def test_feedback_identical_sandboxed_vs_not(h, image, no_new_containers, name, suite, tree_files):
    both = _run_both(h, suite, tree_files=tree_files, counted=False)
    none, dock = both["none"], both["docker"]
    assert none[0] == dock[0], f"exit codes differ: {none[0]} vs {dock[0]}"
    assert none[1] == dock[1], f"stdout differs:\n{none[1]}\n---\n{dock[1]}"
    assert none[2] == dock[2], f"stderr differs: {none[2]!r} vs {dock[2]!r}"
    assert none[3] == dock[3]


@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.fr("SC-001")
def test_feedback_identical_on_timeout(h, image, no_new_containers):
    both = {}
    for mode in ("none", "docker"):
        hx = _h8.H()
        try:
            hx.make_tree()
            hx.write_suite({"test_sleep.py": _h8.SLEEP_SUITE})
            res = hx.run_sb("--timeout", "6", sandbox=(mode == "docker"), counted=False,
                            timeout=400)
            both[mode] = (res.code, res.out, res.err)
        finally:
            hx.close()
    assert both["none"] == both["docker"], both


# -- SC-006 performance --------------------------------------------------------------------------

@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.fr("SC-006")
def test_small_suite_overhead_under_60s(h, tree, image, no_new_containers):
    # unsandboxed baseline
    suite = {f"test_{i}.py": '''
        import pytest


        @pytest.mark.fr("FR-00%d")
        def test_a():
            assert True


        @pytest.mark.fr("FR-00%d")
        def test_b():
            assert 1 + 1 == 2
        ''' % (i % 9 + 1, i % 9 + 1) for i in range(8)}  # 16 tests < 20
    hx = _h8.H()
    try:
        hx.make_tree()
        hx.write_suite(suite)
        base = hx.run_sb(sandbox=False).elapsed
    finally:
        hx.close()
    h.write_suite(suite)
    res = h.run_sb()
    assert res.code == 0, res.out
    assert res.elapsed <= base + 60, f"sandboxed {res.elapsed:.1f}s vs base {base:.1f}s + 60"


# -- SC-002 / FR-019 parity on the accepted suites (slow, needs the real tree copy) ---------------

def _archive_copy(dest: Path) -> bool:
    """A committed-state copy of the tree under test via `git archive HEAD` (007 convention)."""
    try:
        p1 = subprocess.run(["git", "-C", str(REPO), "archive", "HEAD"],
                            stdout=subprocess.PIPE, timeout=120)
        if p1.returncode != 0:
            return False
        dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "-x", "-C", str(dest)], input=p1.stdout, timeout=120, check=True)
        return (dest / "scripts" / "heldout_run.py").is_file()
    except Exception:
        return False


ACCEPTED = [
    ("005-multi-instance", "moeka"),
    ("006-rsi-kernel-prerequisites", "moeka"),
]


@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.network
@pytest.mark.fr("SC-002", "FR-019")
@pytest.mark.parametrize("feature,profile", ACCEPTED)
def test_accepted_suite_parity(h, image, feature, profile):
    suite_src = REPO / "tests" / "heldout" / feature
    if not suite_src.is_dir():
        pytest.skip(f"accepted suite {feature} not in the tree under test")
    # copy of the tree (committed state); the suite comes from tests/heldout/<feature>
    copy = _h8.mkd(h.base / "treecopy")
    if not _archive_copy(copy):
        pytest.skip("cannot git-archive the tree under test")
    # a private suite dir with 0700 perms under the held-out root
    suite = h.root / "moeka" / feature
    shutil.copytree(suite_src, suite)
    for p in [suite, *suite.rglob("*")]:
        os.chmod(p, 0o700 if p.is_dir() else 0o600)

    def run(sandbox):
        rd = h.new_report_dir()
        args = ["run", "--profile", profile, "--repo-name", "moeka", "--feature", feature,
                "--worktree", str(copy), "--suite", str(suite), "--report-dir", str(rd),
                "--no-count", "--pytest-arg", "-m", "--pytest-arg", "not docker and not slow"]
        if sandbox:
            args += ["--sandbox", "docker"]
        res = h.run(*args, timeout=5400)
        res.report_dir = rd
        return res

    base = run(False)
    sb = run(True)
    assert base.err == "" and sb.err == "", (base.err, sb.err)
    assert _h8.per_id_counts(load_report(base.report_dir)) == \
        _h8.per_id_counts(load_report(sb.report_dir)), "per-id counts differ sandboxed vs not"
