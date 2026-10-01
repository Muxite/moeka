"""Group B: build phase (sync step in a container as impl, network, no suite; limits)
(FR-004, FR-005)."""

from __future__ import annotations

import pytest

import _h8
from _h8 import assert_refused, assert_toys_passed, load_report

pytestmark = pytest.mark.docker

# The toy tree's sync step: records what it sees into the copy (/work/_facts); the suite then
# checks those facts from inside the test container and reports through pass/fail.
SYNC_SH = r'''#!/bin/sh
F="$1/_facts"
mkdir -p "$F"
id -u > "$F/uid"
id -g > "$F/gid"
printf '%s' "$1" > "$F/arg"
ls /sys/class/net > "$F/net" 2>/dev/null
cat /proc/self/mountinfo > "$F/mountinfo"
grep -E '^(Cap|NoNewPrivs)' /proc/self/status > "$F/status"
cat /sys/fs/cgroup/memory.max > "$F/memory.max" 2>/dev/null
cat /sys/fs/cgroup/cpu.max > "$F/cpu.max" 2>/dev/null
cat /sys/fs/cgroup/pids.max > "$F/pids.max" 2>/dev/null
touch "$1/_sync_wrote_here" && echo ok > "$F/work_writable"
test -e /.dockerenv && echo yes > "$F/dockerenv"
chmod -R a+rX "$F" "$1/_sync_wrote_here" 2>/dev/null
exit 0
'''

SYNC_CMD = "sh {copy}/h8sync.sh {copy}"

_CHECKS = '''
import os
import pytest

F = "/work/_facts"
ALLOWED_BIND = {{"/", "/work", "/etc/resolv.conf", "/etc/hostname", "/etc/hosts"}}
VIRTUAL = {{"proc", "sysfs", "tmpfs", "devpts", "mqueue", "cgroup", "cgroup2", "overlay",
           "devtmpfs", "securityfs", "fusectl", "debugfs", "tracefs", "binfmt_misc"}}
FORBIDDEN_HOST = {forbidden!r}


def fact(name):
    with open(os.path.join(F, name)) as fh:
        return fh.read().strip()


def caps():
    out = {{}}
    for line in fact("status").splitlines():
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


@pytest.mark.fr("FR-004")
def test_sync_ran_in_container():
    assert fact("dockerenv") == "yes"


@pytest.mark.fr("FR-004")
def test_sync_uid_is_impl():
    assert fact("uid") == "10102"


@pytest.mark.fr("FR-004")
def test_sync_copy_placeholder_is_work():
    assert fact("arg") == "/work"


@pytest.mark.fr("FR-004")
def test_sync_work_is_writable():
    assert fact("work_writable") == "ok"


@pytest.mark.fr("FR-004")
def test_sync_has_bridge_network():
    ifaces = set(fact("net").split())
    assert ifaces - {{"lo"}}, ifaces


@pytest.mark.fr("FR-004")
def test_sync_no_host_paths_mounted():
    bad = []
    for line in fact("mountinfo").splitlines():
        left, _, right = line.partition(" - ")
        f = left.split()
        root, mnt = f[3], f[4]
        fstype = right.split()[0] if right else ""
        if any(root == h or root.startswith(h + "/") for h in FORBIDDEN_HOST):
            bad.append((mnt, "forbidden source"))
        elif fstype in VIRTUAL or mnt in ALLOWED_BIND:
            continue
        else:
            bad.append((mnt, fstype))
    assert not bad, bad


@pytest.mark.fr("FR-005")
def test_sync_caps_dropped():
    c = caps()
    for k in ("CapEff", "CapPrm", "CapBnd", "CapAmb"):
        assert int(c.get(k, "0"), 16) == 0, (k, c.get(k))


@pytest.mark.fr("FR-005")
def test_sync_no_new_privs():
    assert caps().get("NoNewPrivs") == "1"


@pytest.mark.fr("FR-005")
def test_sync_memory_limit():
    assert fact("memory.max") == "{mem}"


@pytest.mark.fr("FR-005")
def test_sync_cpu_limit():
    assert fact("cpu.max").split() == ["{cpu_quota}", "100000"]


@pytest.mark.fr("FR-005")
def test_sync_pids_limit():
    v = fact("pids.max")
    assert v != "max" and int(v) > 0


@pytest.mark.fr("FR-006")
def test_phase_limits_same_as_build():
    def rd(p):
        with open(p) as fh:
            return fh.read().strip()
    assert rd("/sys/fs/cgroup/memory.max") == "{mem}"
    assert rd("/sys/fs/cgroup/cpu.max").split() == ["{cpu_quota}", "100000"]
    assert rd("/sys/fs/cgroup/pids.max") != "max"
'''

ALL_KEYS = ["test_sync_ran_in_container", "test_sync_uid_is_impl",
            "test_sync_copy_placeholder_is_work", "test_sync_work_is_writable",
            "test_sync_has_bridge_network", "test_sync_no_host_paths_mounted",
            "test_sync_caps_dropped", "test_sync_no_new_privs", "test_sync_memory_limit",
            "test_sync_cpu_limit", "test_sync_pids_limit", "test_phase_limits_same_as_build"]


def _suite(h, mem="4294967296", cpu_quota="200000"):
    forbidden = sorted({str(h.suite), str(h.root), str(h.reports), str(h.home),
                        str(h.suite.resolve()), str(h.root.resolve())})
    h.write_suite({"test_facts.py": _CHECKS.format(forbidden=forbidden, mem=mem,
                                                  cpu_quota=cpu_quota)})


def _run_checks(h, keys, *extra, **kw):
    h.make_tree({"h8sync.sh": SYNC_SH})
    res = h.run_sb("--sync-cmd", SYNC_CMD, *extra, **kw)
    rep = load_report(res.report_dir)
    return res, rep


@pytest.fixture(scope="module")
def facts_report(image):
    hx = _h8.H()
    try:
        _suite(hx)
        before = set(_h8.sandbox_containers())
        res, rep = _run_checks(hx, [])
        left = set(_h8.sandbox_containers()) - before
        yield res, rep, left
    finally:
        hx.close()


@pytest.mark.fr("FR-004", "FR-005")
@pytest.mark.parametrize("key", ALL_KEYS[:-1])
def test_sync_step_facts_default_limits(facts_report, key):
    res, rep, _left = facts_report
    assert_toys_passed(rep, [key])


@pytest.mark.fr("FR-005")
def test_build_and_test_containers_removed(facts_report):
    assert not facts_report[2], "containers left after the run"


@pytest.mark.fr("FR-005", "FR-006")
def test_custom_limits_apply_to_both_phases(h, image, no_new_containers):
    _suite(h, mem=str(512 * 1024 * 1024), cpu_quota="100000")
    res, rep = _run_checks(h, [], "--sandbox-memory", "512m", "--sandbox-cpus", "1")
    assert_toys_passed(rep, ["test_sync_memory_limit", "test_sync_cpu_limit",
                             "test_phase_limits_same_as_build"])


@pytest.mark.fr("FR-006", "FR-005")
def test_test_phase_default_limits(h, image, no_new_containers):
    _suite(h)
    res, rep = _run_checks(h, [])
    assert_toys_passed(rep, ["test_phase_limits_same_as_build"])


@pytest.mark.fr("FR-004")
def test_generic_has_no_sync_step_without_sync_cmd(h, image, no_new_containers):
    h.make_tree({"h8sync.sh": SYNC_SH})
    h.write_suite({"test_nosync.py": '''
        import os
        import pytest


        @pytest.mark.fr("FR-004")
        def test_no_facts():
            assert not os.path.exists("/work/_facts")
        '''})
    res = h.run_sb()
    rep = load_report(res.report_dir)
    assert_toys_passed(rep, ["test_no_facts"])
    assert not (res.report_dir / "sync.log").exists()


@pytest.mark.fr("FR-004", "FR-009")
def test_sync_writes_stay_in_copy_not_worktree(h, image, no_new_containers):
    _suite(h)
    h.make_tree({"h8sync.sh": SYNC_SH})
    before = _h8.snapshot(h.tree)
    res = h.run_sb("--sync-cmd", SYNC_CMD)
    assert res.code in (0, 1)
    assert _h8.snapshot(h.tree) == before, "sync step changed the worktree"
    assert h.leftovers() == []


@pytest.mark.fr("FR-004")
def test_failing_sync_is_infra_error(h, image, no_new_containers):
    h.make_tree({"h8sync.sh": "#!/bin/sh\nexit 7\n"})
    h.write_suite({"test_x.py": _h8.ALL_PASS_SUITE})
    res = h.run(*h.std("--sync-cmd", SYNC_CMD, counted=True, report=False))
    assert_refused(res, "E_SYNC", 3)
    assert _h8.status_of(res) == "infra_error"
    assert h.counted() == 0
    assert h.leftovers() == []


@pytest.mark.fr("FR-005")
def test_sync_timeout_kills_container(h, image, no_new_containers):
    h.make_tree({"h8sync.sh": "#!/bin/sh\ntrap '' TERM\nsleep 900\n"})
    h.write_suite({"test_x.py": _h8.ALL_PASS_SUITE})
    res = h.run(*h.std("--sync-cmd", SYNC_CMD, "--sync-timeout", "5", counted=True,
                       report=False), timeout=300)
    assert res.code == 3
    assert _h8.status_of(res) == "infra_error"
    assert res.elapsed < 5 + 15 + 60, f"sync timeout took {res.elapsed:.0f}s"
    assert h.counted() == 0
