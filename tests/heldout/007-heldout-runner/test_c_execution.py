"""Group C: profiles, pytest invocation, timeout, sync, collection errors, lock
(FR-011..016, SC-004)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

import _h7
from _h7 import assert_refused


def _real(p) -> str:
    return os.path.realpath(p)


def _pp(probe) -> list[str]:
    return [_real(x) for x in (probe["env"]["PYTHONPATH"] or "").split(os.pathsep) if x]


def _plugin_at(probe, i: int) -> bool:
    """PYTHONPATH entry i (non-empty entries) held a byte copy of the runner during the run."""
    raw = (probe["env"]["PYTHONPATH"] or "").split(os.pathsep)
    shas = [s for x, s in zip(raw, probe["pp_runner"]) if x]
    return i < len(shas) and shas[i] == _h7.sha256_file(_h7.RUNNER)


# -- FR-011 profiles -----------------------------------------------------------------------------

@pytest.mark.fr("FR-011", "FR-012")
def test_generic_profile_launcher_cwd_pythonpath(h, tree):
    h.write_suite({"test_probe.py": _h7.probe_test()})
    res = h.run_std()
    assert res.code == 0
    p = h.obs("probe")
    copy = _real(p["copy"])
    assert p["executable"] == sys.executable or _real(p["executable"]) == _real(sys.executable)
    pp = _pp(p)
    assert pp[0] == copy, "first PYTHONPATH entry is not the copy"
    assert _plugin_at(p, 1), "second PYTHONPATH entry is not the plugin dir"
    cwd = _real(p["cwd"])
    assert cwd != copy and not cwd.startswith(copy + os.sep), "generic cwd is inside the copy"
    for q in (h.suite, h.tree, h.root):
        assert not cwd.startswith(_real(q) + os.sep) and cwd != _real(q)
    assert p["cwd_list"] == [], "generic cwd is not an empty scratch dir"
    assert Path(_real(h.tmp)) in Path(cwd).parents, "generic cwd is not a scratch dir"
    assert not Path(cwd).exists(), "scratch cwd not removed"


@pytest.mark.fr("FR-011", "FR-012")
def test_moeka_profile_sync_launcher_cwd(h, tree):
    h.fake_uv()
    h.write_suite({"test_probe.py": _h7.probe_test()})
    res = h.run_std(profile="moeka")
    assert res.code == 0, f"exit {res.code}"
    p = h.obs("probe")
    copy = _real(p["copy"])
    log = h.uv_log()
    syncs = [e for e in log if e["argv"][:1] == ["sync"]]
    runs = [e for e in log if e["argv"][:1] == ["run"]]
    assert len(syncs) == 1, "moeka profile did not run exactly one sync"
    a = syncs[0]["argv"]
    assert a[0] == "sync" and _real(a[a.index("--project") + 1]) == copy
    assert a[1:] == ["--project", a[2], "--extra", "dev", "-q"]
    assert len(runs) == 1
    r = runs[0]["argv"]
    assert r[:2] == ["run", "--project"] and _real(r[2]) == copy
    assert r[3:6] == ["--extra", "dev", "pytest"]
    pp = _pp(p)
    assert pp[0] == copy
    assert _plugin_at(p, 1)
    cwd = _real(p["cwd"])
    assert cwd != copy and not cwd.startswith(copy + os.sep)
    assert p["cwd_list"] == []
    assert (h.round_dir(1) / "sync.log").is_file(), "sync.log missing although sync ran"


@pytest.mark.fr("FR-011", "FR-012")
def test_awork_resume_profile_no_sync_cwd_is_copy(h, tree):
    h.fake_uv()
    h.write_suite({"test_probe.py": _h7.probe_test()})
    res = h.run_std(profile="awork-resume")
    assert res.code == 0, f"exit {res.code}"
    p = h.obs("probe")
    copy = _real(p["copy"])
    log = h.uv_log()
    assert not [e for e in log if e["argv"][:1] == ["sync"]], "awork-resume ran a sync step"
    runs = [e for e in log if e["argv"][:1] == ["run"]]
    assert len(runs) == 1
    r = runs[0]["argv"]
    assert r[:2] == ["run", "--project"] and _real(r[2]) == copy
    assert r[3:6] == ["--extra", "dev", "pytest"]
    pp = _pp(p)
    assert pp[0] == _real(Path(copy) / "src") or pp[0] == str(Path(copy) / "src")
    assert pp[1] == copy
    assert _plugin_at(p, 2)
    assert _real(p["cwd"]) == copy, "awork-resume cwd is not the copy"


@pytest.mark.fr("FR-011")
def test_no_sync_overrides_profile(h, tree):
    h.fake_uv()
    h.write_suite({"test_probe.py": _h7.probe_test()})
    res = h.run_std("--no-sync", profile="moeka")
    assert res.code == 0
    assert not [e for e in h.uv_log() if e["argv"][:1] == ["sync"]]
    assert h.obs("probe") is not None


@pytest.mark.fr("FR-011")
def test_unknown_profile_is_usage_error(h, tree):
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    res = h.run_std(profile="nosuch")
    assert_refused(res, "E_USAGE", 2)


@pytest.mark.fr("FR-011", "FR-012")
def test_unset_removes_variables_from_child(h, tree):
    h.env["CANARY_UNSET_ME"] = "secret-" + h.token()
    h.env["CANARY_KEEP_ME"] = "kept"
    h.write_suite({"test_probe.py": _h7.probe_test()})
    res = h.run_std("--unset", "CANARY_UNSET_ME")
    assert res.code == 0
    env = h.obs("probe")["env"]
    assert env["CANARY_UNSET_ME"] is None
    assert env["CANARY_KEEP_ME"] == "kept"


@pytest.mark.fr("FR-011", "FR-012")
def test_pytest_args_appended(h, tree):
    h.write_suite({"test_probe.py": _h7.probe_test()})
    res = h.run_std("--pytest-arg=--tb=short", "--pytest-arg=-rA")
    assert res.code == 0
    argv = h.obs("probe")["argv"]
    assert argv[-2:] == ["--tb=short", "-rA"]


# -- FR-012 pytest invocation and environment ---------------------------------------------------

@pytest.mark.fr("FR-012")
def test_pytest_invocation_arguments(h, tree):
    h.write_suite({"test_probe.py": _h7.probe_test()})
    res = h.run_std()
    assert res.code == 0
    p = h.obs("probe")
    argv = p["argv"][1:]
    assert _real(argv[0]) == _real(p["suite_dir"]), "first pytest argument is not the suite copy"
    assert "-q" in argv
    pairs = list(zip(argv, argv[1:]))
    assert ("-p", "no:cacheprovider") in pairs
    assert ("-p", "heldout_run") in pairs
    junit = [a for a in argv if a.startswith("--junitxml=")]
    assert len(junit) == 1
    assert _real(junit[0].split("=", 1)[1]) == _real(h.round_dir(1) / "junit.xml")


@pytest.mark.fr("FR-012")
def test_child_environment(h, tree):
    inherited = _h7.mkd(h.base / "inherited")
    h.env["PYTHONPATH"] = str(inherited)
    h.write_suite({"test_probe.py": _h7.probe_test()})
    res = h.run_std()
    assert res.code == 0
    p = h.obs("probe")
    env = p["env"]
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["HELDOUT_ROUND"] == "1"
    rec = env["HELDOUT_RUN_RECORDS"]
    assert rec, "HELDOUT_RUN_RECORDS not set"
    rec = _real(rec)
    for q in (p["copy"], h.suite, h.tree):
        assert not rec.startswith(_real(q) + os.sep), "records file inside copy/suite/tree"
    pp = _pp(p)
    assert pp[-1] == _real(inherited), "inherited PYTHONPATH not kept last"
    assert pp.index(_real(inherited)) > 1


@pytest.mark.fr("FR-012")
def test_plugin_dir_holds_copy_of_runner(h, tree):
    h.write_suite({"test_probe.py": _h7.probe_test()})
    res = h.run_std()
    assert res.code == 0
    p = h.obs("probe")
    assert _plugin_at(p, 1), "plugin dir does not hold a byte copy of the runner"
    plugin = Path(_pp(p)[1])
    assert not plugin.exists(), "plugin dir not removed after the run"
    for q in (p["copy"], h.suite, h.tree):
        assert not str(plugin).startswith(_real(q) + os.sep)


@pytest.mark.fr("FR-012")
def test_heldout_round_follows_round_number(h, tree):
    h.write_suite({"test_probe.py": _h7.probe_test()})
    assert h.run_std().code == 0
    assert h.run_std().code == 0
    assert h.obs("probe")["env"]["HELDOUT_ROUND"] == "2"


# -- FR-013 timeout -------------------------------------------------------------------------------

TIMEOUT_SUITE = '''
@pytest.mark.fr("FR-001")
def test_a():
    _mark_run()


@pytest.mark.fr("FR-002")
def test_b():
    import signal, subprocess
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([_sys.executable, "-c",
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(3600)"])
    _obs("hang", {"pid": _os.getpid(), "child": child.pid})
    while True:
        _time.sleep(0.2)


@pytest.mark.fr("FR-002")
def test_c():
    pass
'''


@pytest.mark.slow
@pytest.mark.fr("FR-013", "SC-004")
def test_timeout_ignoring_sigterm_kills_group(h, tree):
    h.write_suite({"test_s.py": TIMEOUT_SUITE})
    res = h.run_std("--timeout", "3")
    assert res.elapsed < 3 + 15 + 8, f"took {res.elapsed:.1f}s"
    assert res.code == 3
    assert res.err == ""
    assert res.lines() == [
        "heldout-run: 001-demo round 1/4: timeout",
        "FR-002: 2/2 failing",
        "total: 2/3 tests failing, 0 skipped; 1/2 ids failing",
    ]
    info = h.obs("hang")
    assert _h7.wait_until(lambda: not _h7.proc_alive(info["pid"]), 10), "pytest survived"
    assert _h7.wait_until(lambda: not _h7.proc_alive(info["child"]), 10), "group child survived"


@pytest.mark.slow
@pytest.mark.fr("FR-013")
def test_timeout_json_and_round_counted(h, tree):
    h.write_suite({"test_s.py": TIMEOUT_SUITE})
    res = h.run_std("--timeout", "2", "--json")
    assert res.code == 3
    fb = res.json()
    assert fb["status"] == "timeout"
    assert fb["failing"] == {"FR-002": {"failing": 2, "total": 2}}
    assert fb["totals"] == {"tests": 3, "failing": 2, "skipped": 0, "ids": 2, "ids_failing": 1}
    assert h.rounds_json()["rounds"][0]["status"] == "timeout"


@pytest.mark.slow
@pytest.mark.fr("FR-013")
def test_timeout_is_wall_clock_from_pytest_start(h, tree):
    h.write_suite({"test_s.py": '''
@pytest.mark.fr("FR-001")
def test_slow_but_ok():
    _time.sleep(4)
'''})
    res = h.run_std("--timeout", "20")
    assert res.code == 0
    res2 = h.run_std("--timeout", "1", "--no-count")
    assert res2.code == 3
    assert res2.lines()[0] == "heldout-run: 001-demo round -/4: timeout"


# -- FR-014 sync failure ---------------------------------------------------------------------------

@pytest.mark.fr("FR-014")
def test_sync_failure_is_infra_error_not_counted(h, tree):
    h.fake_uv(noise=h.token())
    h.env["FAKE_UV_SYNC"] = "fail"
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    res = h.run_std(profile="moeka")
    assert_refused(res, "E_SYNC", 3)
    assert h.runs() == 0, "tests ran after a failed sync"
    assert h.counted() == 0
    assert not h.leaks(res.out, res.err)


@pytest.mark.slow
@pytest.mark.fr("FR-014")
def test_sync_timeout_is_infra_error(h, tree):
    h.fake_uv()
    h.env["FAKE_UV_SYNC"] = "hang"
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    res = h.run_std("--sync-timeout", "2", profile="moeka")
    assert res.elapsed < 2 + 15 + 8
    assert_refused(res, "E_SYNC", 3)
    assert h.counted() == 0
    assert h.runs() == 0


@pytest.mark.fr("FR-014")
def test_sync_failure_json_status(h, tree):
    h.fake_uv()
    h.env["FAKE_UV_SYNC"] = "fail"
    h.write_suite({"test_s.py": _h7.ALL_PASS_SUITE})
    ff = h.base / "fb.json"
    res = h.run_std("--json", "--feedback-file", ff, profile="moeka")
    assert res.code == 3
    if res.out.strip():
        assert res.json()["status"] == "infra_error"


# -- FR-015 collection errors ------------------------------------------------------------------------

COLLECTION_CASES = {
    "import_error": ({"test_good.py": _h7.ALL_PASS_SUITE,
                      "test_bad.py": "import toymod_missing_module_zz\n"}, ()),
    "raise_at_import": ({"test_bad.py": "raise RuntimeError('broken')\n"}, ()),
    "syntax_error": ({"test_bad.py": "def broken(:\n"}, ()),
    "no_tests": ({"README.txt": "no tests here\n"}, ()),
    "usage_error": ({"test_good.py": _h7.ALL_PASS_SUITE}, ("--pytest-arg=--no-such-pytest-flag",)),
    "no_results": ({"test_good.py": _h7.ALL_PASS_SUITE,
                    "conftest.py": "import os\n\ndef pytest_sessionstart(session):\n"
                                   "    os._exit(0)\n"}, ()),
}


@pytest.mark.fr("FR-015")
@pytest.mark.parametrize("case", sorted(COLLECTION_CASES))
def test_collection_error_status(h, tree, case):
    files, extra = COLLECTION_CASES[case]
    h.write_suite(files)
    res = h.run_std(*extra)
    assert res.code == 3, f"exit {res.code}"
    assert res.err == ""
    assert res.lines()[0] == "heldout-run: 001-demo round 1/4: collection_error"
    data = h.rounds_json()
    assert data and len(data["rounds"]) == 1, "collection_error round not counted"
    assert data["rounds"][0]["status"] == "collection_error"


@pytest.mark.fr("FR-015")
def test_missing_code_under_test_is_collection_error(h):
    h.make_tree()
    import shutil
    shutil.rmtree(h.tree / "toymod")
    h.write_suite({"test_s.py": _h7.US1_SUITE})
    res = h.run_std("--json")
    assert res.code == 3
    assert res.json()["status"] == "collection_error"


# -- FR-016 lock ---------------------------------------------------------------------------------------

BLOCK_SUITE = '''
@pytest.mark.fr("FR-001")
def test_block():
    _mark_run()
    _obs("started", {"pid": _os.getpid()})
    _wait_release("release")
'''


@pytest.mark.slow
@pytest.mark.fr("FR-016")
def test_second_concurrent_run_is_busy(h, tree):
    h.write_suite({"test_s.py": BLOCK_SUITE})
    first = h.spawn(*h.std())
    assert _h7.wait_until(lambda: h.obs("started") is not None, 60), "first run never started"
    second = h.run_std()
    assert_refused(second, "E_BUSY", 3)
    assert not h.leaks(second.out, second.err)
    (h.out / "release").write_text("go")
    out, err = first.communicate(timeout=120)
    assert first.returncode == 0
    assert h.runs() == 1
    data = h.rounds_json()
    assert len(data["rounds"]) == 1, "busy run was counted"
    third = h.run_std()
    assert third.lines()[0] == "heldout-run: 001-demo round 2/4: passed"


@pytest.mark.slow
@pytest.mark.fr("FR-016")
def test_busy_also_for_no_count_run(h, tree):
    h.write_suite({"test_s.py": BLOCK_SUITE})
    first = h.spawn(*h.std("--no-count"))
    assert _h7.wait_until(lambda: h.obs("started") is not None, 60)
    second = h.run_std("--no-count")
    assert_refused(second, "E_BUSY", 3)
    (h.out / "release").write_text("go")
    first.communicate(timeout=120)
    assert first.returncode == 0


@pytest.mark.slow
@pytest.mark.fr("FR-016")
def test_other_feature_is_not_blocked(h, tree):
    h.write_suite({"test_s.py": BLOCK_SUITE})
    other = h.root / "demo" / "002-other"
    h.write_suite({"test_o.py": _h7.ALL_PASS_SUITE}, dest=other)
    first = h.spawn(*h.std())
    assert _h7.wait_until(lambda: h.obs("started") is not None, 60)
    res = h.run_std(feature="002-other", suite=other)
    (h.out / "release").write_text("go")
    first.communicate(timeout=120)
    assert res.code == 0, "a different feature was blocked by the lock"
    assert res.lines()[0] == "heldout-run: 002-other round 1/4: passed"
