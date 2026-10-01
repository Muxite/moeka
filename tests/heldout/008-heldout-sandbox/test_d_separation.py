"""Group D: implementation processes vs the judge: heldout-impl, unreadable suite/out/records/
plugin, isolation flag (FR-011, FR-012, FR-013, SC-005, Story 4)."""

from __future__ import annotations

import pytest

import _h8
from _h8 import assert_toys_passed, load_report

CHECKS = '''
import os
import subprocess
import pytest

IMPL = os.environ.get("HELDOUT_IMPL") or "/usr/local/bin/heldout-impl"
RECORDS = os.environ.get("HELDOUT_RUN_RECORDS", "")


def impl(*args, **kw):
    kw.setdefault("capture_output", True)
    return subprocess.run([IMPL, *args], **kw)


def plugin_dir():
    for p in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if p and os.path.isfile(os.path.join(p, "heldout_run.py")) and p != "/work":
            return p
    raise AssertionError("no plugin dir on PYTHONPATH")


def capfields(text):
    out = {{}}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


# -- FR-012 / SC-005 heldout-impl --------------------------------------------------------------

@pytest.mark.fr("FR-012", "SC-005")
def test_impl_uid_differs_from_judge():
    r = impl("id", "-u")
    assert r.returncode == 0
    assert r.stdout.decode().strip() == "10102"
    assert int(r.stdout) != os.getuid()


@pytest.mark.fr("FR-012")
def test_impl_gid_and_no_supplementary_groups():
    un = impl("id", "-un").stdout.decode().strip()
    gn = impl("id", "-gn").stdout.decode().strip()
    g = impl("id", "-g").stdout.decode().strip()
    groups = impl("id", "-G").stdout.decode().split()
    assert un == "impl" and gn == "impl"
    assert groups == [g], groups


@pytest.mark.fr("FR-012")
def test_impl_child_has_no_capabilities():
    st = capfields(impl("grep", "-E", "^Cap", "/proc/self/status").stdout.decode())
    for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert int(st[k], 16) == 0, (k, st.get(k))


@pytest.mark.fr("FR-012")
def test_judge_process_tree_holds_no_capabilities():
    with open("/proc/self/status") as fh:
        st = capfields(fh.read())
    for k in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
        assert int(st[k], 16) == 0, (k, st.get(k))
    with open("/proc/%d/status" % os.getppid()) as fh:
        pst = capfields(fh.read())
    if int(pst.get("Uid", "0").split()[0]) == 10101:
        for k in ("CapPrm", "CapEff", "CapAmb"):
            assert int(pst[k], 16) == 0, ("parent", k)


@pytest.mark.fr("FR-012")
def test_impl_stdin_stdout_passthrough():
    r = impl("cat", input=b"h8-stdin-{tok}")
    assert r.returncode == 0 and r.stdout == b"h8-stdin-{tok}"


@pytest.mark.fr("FR-012")
def test_impl_stderr_passthrough():
    r = impl("sh", "-c", "echo out; echo err-{tok} >&2")
    assert r.stdout == b"out\\n" and r.stderr == b"err-{tok}\\n"


@pytest.mark.fr("FR-012")
def test_impl_exit_status():
    assert impl("sh", "-c", "exit 7").returncode == 7
    assert impl("sh", "-c", "exit 0").returncode == 0


@pytest.mark.fr("FR-012")
def test_impl_working_directory():
    d = "/tmp/h8cwd-{tok}"
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o755)
    r = impl("pwd", cwd=d)
    assert r.stdout.decode().strip() == d


@pytest.mark.fr("FR-012")
def test_impl_environment_minus_records():
    env = dict(os.environ, H8_PASS="pass-{tok}")
    assert "HELDOUT_RUN_RECORDS" in env
    r = impl("sh", "-c", "printf '%s|%s' \\"$H8_PASS\\" \\"${{HELDOUT_RUN_RECORDS-UNSET}}\\"", env=env)
    assert r.stdout.decode() == "pass-{tok}|UNSET"


@pytest.mark.fr("FR-012")
def test_impl_runs_implementation_code():
    r = impl("python3", "-c", "import toymod, os; print(toymod.add(2, 3), os.getuid())",
             env=dict(os.environ, PYTHONPATH="/work"))
    assert r.stdout.decode().split() == ["5", "10102"]


# -- FR-011 / SC-005 judge files unreadable for impl ---------------------------------------------

@pytest.mark.fr("FR-011", "SC-005")
def test_impl_cannot_read_suite_file():
    r = impl("cat", os.path.realpath(__file__))
    assert r.returncode != 0 and b"FR-011" not in r.stdout


@pytest.mark.fr("FR-011")
def test_impl_cannot_list_or_write_suite():
    assert impl("ls", "/suite").returncode != 0
    assert impl("sh", "-c", "echo x > /suite/h8x.py").returncode != 0
    assert impl("sh", "-c", "echo x > %s/h8x.py" % os.path.dirname(__file__)).returncode != 0


@pytest.mark.fr("FR-011")
def test_impl_cannot_read_or_write_out():
    assert impl("ls", "/out").returncode != 0
    assert impl("sh", "-c", "echo x > /out/h8x").returncode != 0


@pytest.mark.fr("FR-011")
def test_impl_cannot_read_or_append_records():
    assert RECORDS
    assert impl("cat", RECORDS).returncode != 0
    assert impl("sh", "-c", "echo '{{}}' >> " + RECORDS).returncode != 0
    assert impl("test", "-r", RECORDS).returncode != 0


@pytest.mark.fr("FR-011")
def test_impl_cannot_read_or_write_plugin_dir():
    p = plugin_dir()
    assert impl("cat", os.path.join(p, "heldout_run.py")).returncode != 0
    assert impl("ls", p).returncode != 0
    assert impl("sh", "-c", "echo x > %s/h8x.py" % p).returncode != 0
'''

KEYS = {
    "test_impl_uid_differs_from_judge": ("FR-012", "SC-005"),
    "test_impl_gid_and_no_supplementary_groups": ("FR-012",),
    "test_impl_child_has_no_capabilities": ("FR-012",),
    "test_judge_process_tree_holds_no_capabilities": ("FR-012",),
    "test_impl_stdin_stdout_passthrough": ("FR-012",),
    "test_impl_stderr_passthrough": ("FR-012",),
    "test_impl_exit_status": ("FR-012",),
    "test_impl_working_directory": ("FR-012",),
    "test_impl_environment_minus_records": ("FR-012",),
    "test_impl_runs_implementation_code": ("FR-012",),
    "test_impl_cannot_read_suite_file": ("FR-011", "SC-005"),
    "test_impl_cannot_list_or_write_suite": ("FR-011",),
    "test_impl_cannot_read_or_write_out": ("FR-011",),
    "test_impl_cannot_read_or_append_records": ("FR-011",),
    "test_impl_cannot_read_or_write_plugin_dir": ("FR-011",),
}


@pytest.fixture(scope="module")
def sep(image):
    hx = _h8.H()
    try:
        hx.make_tree()
        hx.write_suite({"test_sep.py": CHECKS.format(tok=hx.tok)})
        res = hx.run_sb()
        yield dict(res=res, rep=load_report(res.report_dir))
    finally:
        hx.close()


@pytest.mark.docker
@pytest.mark.parametrize("key", [pytest.param(k, marks=pytest.mark.fr(*v), id=k)
                                 for k, v in KEYS.items()])
def test_in_container_separation(sep, key):
    assert_toys_passed(sep["rep"], [key])


@pytest.mark.docker
@pytest.mark.fr("FR-012")
def test_heldout_impl_unset_without_sandbox(h, tree):
    h.write_suite({"test_env.py": '''
        import os
        import pytest


        @pytest.mark.fr("FR-012")
        def test_unset():
            assert "HELDOUT_IMPL" not in os.environ
        '''})
    h.env.pop("HELDOUT_IMPL", None)
    res = h.run_sb(sandbox=False)
    assert res.code == 0
    assert_toys_passed(load_report(res.report_dir), ["test_unset"])


# -- FR-013 isolation flag -----------------------------------------------------------------------

@pytest.mark.docker
@pytest.mark.fr("FR-013")
def test_isolation_partial_when_judge_imports_work_module(h, tree, image):
    h.write_suite({"test_imp.py": '''
        import pytest
        import toymod


        @pytest.mark.fr("FR-001")
        def test_add():
            assert toymod.add(1, 2) == 3
        '''})
    res = h.run_sb()
    assert res.code == 0
    assert load_report(res.report_dir)["meta"].get("isolation") == "partial"


@pytest.mark.docker
@pytest.mark.fr("FR-013")
def test_isolation_full_when_impl_only_runs_as_process(h, tree, image):
    h.write_suite({"test_proc.py": '''
        import os
        import subprocess
        import pytest


        @pytest.mark.fr("FR-001")
        def test_add_via_process():
            r = subprocess.run([os.environ["HELDOUT_IMPL"], "python3", "-c",
                                "import toymod; print(toymod.add(1, 2))"],
                               capture_output=True, env=dict(os.environ, PYTHONPATH="/work"))
            assert r.stdout.strip() == b"3"
        '''})
    res = h.run_sb()
    assert res.code == 0
    assert load_report(res.report_dir)["meta"].get("isolation") == "full"


@pytest.mark.docker
@pytest.mark.fr("FR-013")
def test_isolation_partial_for_late_import(h, tree, image):
    h.write_suite({"test_late.py": '''
        import pytest


        @pytest.mark.fr("FR-001")
        def test_late():
            import importlib
            m = importlib.import_module("toymod")
            assert m.add(2, 2) == 4
        '''})
    res = h.run_sb()
    assert res.code == 0
    assert load_report(res.report_dir)["meta"].get("isolation") == "partial"
