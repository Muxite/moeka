"""Group C: test phase container (no network, read-only root, /work /suite /out), pytest as
judge, timeout, copy-out and cleanup, docker-marker refusal (FR-006..FR-010)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

import _h8
from _h8 import assert_refused, assert_toys_passed, load_report

CHECKS = '''
import os
import socket
import subprocess
import sys
import pytest

RUNNER_SHA = {runner_sha!r}
FORBIDDEN_HOST = {forbidden!r}
IMPL = os.environ.get("HELDOUT_IMPL")


def mounts():
    out = {{}}
    with open("/proc/self/mountinfo") as fh:
        for line in fh:
            left, _, right = line.partition(" - ")
            f = left.split()
            out[f[4]] = dict(root=f[3], opts=f[5].split(","), fstype=right.split()[0])
    return out


@pytest.mark.fr("FR-006")
def test_only_loopback():
    assert sorted(os.listdir("/sys/class/net")) == ["lo"]


@pytest.mark.fr("FR-006")
def test_no_outbound_connection():
    for addr in (("1.1.1.1", 53), ("172.17.0.1", 80), ("8.8.8.8", 443)):
        s = socket.socket()
        s.settimeout(2)
        try:
            with pytest.raises(OSError):
                s.connect(addr)
        finally:
            s.close()


@pytest.mark.fr("FR-006")
def test_root_fs_read_only():
    assert "ro" in mounts()["/"]["opts"]


@pytest.mark.fr("FR-006")
def test_tmp_is_tmpfs():
    m = mounts().get("/tmp")
    assert m is not None and m["fstype"] == "tmpfs"


@pytest.mark.fr("FR-006")
def test_work_owned_by_impl_and_writable():
    assert os.stat("/work").st_uid == 10102
    rc = subprocess.run([IMPL, "sh", "-c", "touch /work/.h8w && rm /work/.h8w"]).returncode
    assert rc == 0


@pytest.mark.fr("FR-006")
def test_suite_dir_judge_0700():
    here = os.path.realpath(__file__)
    assert here.startswith("/suite/")
    st = os.stat("/suite")
    assert st.st_uid == 10101 and stat_mode(st) == 0o700, (st.st_uid, oct(stat_mode(st)))


@pytest.mark.fr("FR-006")
def test_out_dir_judge_0700():
    st = os.stat("/out")
    assert st.st_uid == 10101 and stat_mode(st) == 0o700, (st.st_uid, oct(stat_mode(st)))


def stat_mode(st):
    import stat as _s
    return _s.S_IMODE(st.st_mode)


@pytest.mark.fr("FR-006")
def test_suite_and_out_not_inside_copy_or_tree():
    m = mounts()
    work = m.get("/work", {{}}).get("root")
    for mp in ("/suite", "/out"):
        if mp not in m:
            continue
        root = m[mp]["root"]
        if work and work != "/":
            assert not (root == work or root.startswith(work.rstrip("/") + "/")), mp
        for h in FORBIDDEN_HOST:
            assert not (root == h or root.startswith(h + "/")), (mp, "host path")


@pytest.mark.fr("FR-007")
def test_pytest_runs_as_judge():
    assert os.getuid() == 10101 and os.geteuid() == 10101


@pytest.mark.fr("FR-007")
def test_image_python():
    assert sys.version_info[:2] == (3, 12)
    assert not os.path.realpath(sys.executable).startswith("/work")
    import pytest as _p
    assert not os.path.realpath(_p.__file__).startswith(("/work", "/suite"))


@pytest.mark.fr("FR-007")
def test_pythonpath_copy_then_plugin():
    pp = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    assert pp[0] == "/work", pp
    plug = os.path.join(pp[1], "heldout_run.py")
    import hashlib
    with open(plug, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == RUNNER_SHA


@pytest.mark.fr("FR-007")
def test_records_and_junit_under_out():
    rec = os.environ.get("HELDOUT_RUN_RECORDS", "")
    assert rec.startswith("/out/"), rec
    args = sys.argv
    junit = [a.split("=", 1)[1] for a in args if a.startswith(("--junitxml=", "--junit-xml="))]
    for i, a in enumerate(args[:-1]):
        if a in ("--junitxml", "--junit-xml"):
            junit.append(args[i + 1])
    assert junit and all(j.startswith("/out/") for j in junit), junit


@pytest.mark.fr("FR-007")
def test_plugin_and_flags_in_argv():
    a = sys.argv
    assert "-q" in a
    assert any(x == "no:cacheprovider" or x == "-pno:cacheprovider" for x in a)
    assert any(x in ("heldout_run", "-pheldout_run") for x in a)
    assert any(x.startswith("/suite") for x in a)


@pytest.mark.fr("FR-007")
def test_env_rules():
    assert os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"
    assert os.environ.get("HELDOUT_ROUND") == "1"
    assert os.environ.get("CANARY_KEEP_ME") == "keep-{tok}"
    assert "CANARY_UNSET_ME" not in os.environ


@pytest.mark.fr("FR-007")
def test_cwd_outside_copy_and_suite():
    cwd = os.getcwd()
    for b in ("/work", "/suite"):
        assert not (cwd == b or cwd.startswith(b + "/")), cwd


@pytest.mark.fr("FR-012")
def test_heldout_impl_env_var():
    assert IMPL == "/usr/local/bin/heldout-impl"


@pytest.mark.fr("FR-009")
def test_judge_writes_out_file():
    with open("/out/h8extra-{tok}.txt", "w") as fh:
        fh.write("extra")
    with open("/work/h8judge-{tok}.txt", "w") as fh:
        fh.write("judge was here")
'''

KEYS = ["test_only_loopback", "test_no_outbound_connection", "test_root_fs_read_only",
        "test_tmp_is_tmpfs", "test_work_owned_by_impl_and_writable", "test_suite_dir_judge_0700",
        "test_out_dir_judge_0700", "test_suite_and_out_not_inside_copy_or_tree",
        "test_pytest_runs_as_judge", "test_image_python", "test_pythonpath_copy_then_plugin",
        "test_records_and_junit_under_out", "test_plugin_and_flags_in_argv", "test_env_rules",
        "test_cwd_outside_copy_and_suite", "test_heldout_impl_env_var"]
FR_OF = {k: "FR-006" for k in KEYS[:8]}
FR_OF.update({k: "FR-007" for k in KEYS[8:15]})
FR_OF["test_heldout_impl_env_var"] = "FR-012"


@pytest.fixture(scope="module")
def phase(image):
    hx = _h8.H()
    try:
        hx.make_tree()
        before_tree = _h8.snapshot(hx.tree)
        forbidden = sorted({str(hx.tree), str(hx.root), str(hx.suite), str(hx.home)})
        hx.write_suite({"test_checks.py": CHECKS.format(
            runner_sha=_h8.sha256_file(_h8.RUNNER), forbidden=forbidden, tok=hx.tok)})
        suite_before = _h8.snapshot(hx.suite)
        hx.env["CANARY_KEEP_ME"] = f"keep-{hx.tok}"
        hx.env["CANARY_UNSET_ME"] = "x"
        before = set(_h8.sandbox_containers())
        res = hx.run_sb("--unset", "CANARY_UNSET_ME", counted=True)
        left = set(_h8.sandbox_containers()) - before
        rep = load_report(res.report_dir)
        yield dict(h=hx, res=res, rep=rep, left=left, tree_same=_h8.snapshot(hx.tree) == before_tree,
                   suite_same=_h8.snapshot(hx.suite) == suite_before,
                   leftovers=hx.leftovers(), report_dir=res.report_dir)
    finally:
        hx.close()


def _param():
    return [pytest.param(k, marks=pytest.mark.fr(FR_OF[k]), id=k) for k in KEYS]


@pytest.mark.docker
@pytest.mark.parametrize("key", _param())
def test_in_container_check(phase, key):
    assert_toys_passed(phase["rep"], [key])


@pytest.mark.docker
@pytest.mark.fr("FR-009")
def test_out_copied_to_report_dir(phase):
    tok = phase["h"].tok
    names = [p.name for p in Path(phase["report_dir"]).rglob("*")]
    assert f"h8extra-{tok}.txt" in names, "a file written to /out is not in the report dir"
    for n in ("report.json", "junit.xml", "pytest.log", "feedback.json"):
        assert n in names, n


@pytest.mark.docker
@pytest.mark.fr("FR-009")
def test_report_dir_private_and_owned(phase):
    me = os.getuid()
    for p in [Path(phase["report_dir"]), *Path(phase["report_dir"]).rglob("*")]:
        st = os.lstat(p)
        assert st.st_uid == me, f"{p.name} not owned by the head's uid"
        mode = stat.S_IMODE(st.st_mode)
        assert mode & 0o077 == 0, f"{p.name} mode {oct(mode)}"


@pytest.mark.docker
@pytest.mark.fr("FR-009")
def test_nothing_written_to_worktree(phase):
    assert phase["tree_same"], "the worktree changed"


@pytest.mark.docker
@pytest.mark.fr("FR-009")
def test_scratch_removed(phase):
    assert phase["leftovers"] == [], "scratch directories remain"


@pytest.mark.docker
@pytest.mark.fr("FR-009")
def test_containers_removed(phase):
    assert not phase["left"]


@pytest.mark.docker
@pytest.mark.fr("FR-009", "FR-014")
def test_suite_source_unchanged(phase):
    assert phase["suite_same"], "the suite source changed (canaries must go to the copy only)"


@pytest.mark.docker
@pytest.mark.fr("FR-009")
def test_repo_mode_checkout_unchanged(h, tree, image, no_new_containers):
    h.write_suite({"test_w.py": '''
        import pytest


        @pytest.mark.fr("FR-009")
        def test_write_work():
            with open("/work/h8-written.txt", "w") as fh:
                fh.write("x")
        '''})
    before = _h8.snapshot(h.tree)
    status = h.git("status", "--porcelain")
    args = ["run", "--profile", "generic", "--repo-name", h.repo_name, "--feature", h.feature,
            "--repo", str(h.tree), "--ref", "HEAD", "--suite", str(h.suite), "--sandbox", "docker",
            "--no-count", "--report-dir", str(h.new_report_dir())]
    res = h.run(*args)
    assert res.code == 0, res.out
    assert _h8.snapshot(h.tree) == before
    assert h.git("status", "--porcelain") == status
    assert h.leftovers() == []


# -- FR-008 timeout --------------------------------------------------------------------------------

@pytest.mark.docker
@pytest.mark.fr("FR-008")
def test_timeout_stops_container(h, tree, image, no_new_containers):
    h.write_suite({"test_sleep.py": _h8.SLEEP_SUITE})
    res = h.run_sb("--timeout", "8", counted=True, timeout=400)
    assert _h8.status_of(res) == "timeout"
    assert res.code == 3
    assert res.elapsed < 8 + 15 + 60, f"took {res.elapsed:.0f}s"
    assert h.counted() == 1
    assert h.leftovers() == []


# -- FR-010 docker marker refusal ----------------------------------------------------------------

@pytest.mark.fr("FR-010")
@pytest.mark.parametrize("args", [
    ["--pytest-arg", "-m", "--pytest-arg", "docker"],
    ["--pytest-arg=-m", "--pytest-arg=docker"],
    ["--pytest-arg", "-m", "--pytest-arg", "slow or docker"],
    ["--pytest-arg", "-m", "--pytest-arg", "docker and not slow"],
    ["--pytest-arg", "-m", "--pytest-arg", "(docker)"],
])
def test_docker_marker_refused(h, tree, args):
    h.write_suite({"test_x.py": _h8.ALL_PASS_SUITE})
    res = h.run(*h.std(*args, counted=True, report=False))
    assert_refused(res, "E_SANDBOX_DOCKER", 2)
    assert h.counted() == 0
    assert h.leftovers() == []


@pytest.mark.docker
@pytest.mark.fr("FR-010")
@pytest.mark.parametrize("expr", ["not docker", "not docker and not slow"])
def test_negated_docker_marker_allowed(h, tree, image, expr):
    h.write_suite({"test_x.py": _h8.ALL_PASS_SUITE})
    res = h.run_sb("--pytest-arg", "-m", "--pytest-arg", expr)
    assert res.code == 0 and res.err == ""


@pytest.mark.fr("FR-010", "FR-019")
def test_docker_marker_allowed_unsandboxed(h, tree):
    h.write_suite({"test_x.py": _h8.ALL_PASS_SUITE})
    res = h.run_sb("--pytest-arg", "-m", "--pytest-arg", "docker", sandbox=False)
    assert res.err == ""
    assert res.code in (0, 3)  # nothing selected may be a collection error; never refused
