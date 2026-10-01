"""Group A: mode flag, image, Docker availability, stated limits, unchanged files, Python API
(FR-001, FR-002, FR-003, FR-018, FR-020)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _h8
from _h8 import assert_refused, assert_toys_passed, load_report


IN_CONTAINER_PROBE = '''
import os
import pytest


@pytest.mark.fr("FR-001")
def test_in_container_as_judge():
    assert os.path.exists("/.dockerenv"), "not inside a docker container"
    assert os.getuid() == 10101 and os.geteuid() == 10101, os.getuid()
'''


# -- FR-001 mode ---------------------------------------------------------------------------------

@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("value", ["bubblewrap", "Docker", "", "docker,none"])
def test_unknown_sandbox_value_is_usage_error(h, tree, value):
    h.write_suite({"test_x.py": _h8.ALL_PASS_SUITE})
    res = h.run(*h.std(sandbox=False), "--sandbox", value)
    assert_refused(res, "E_USAGE", 2)
    assert res.out == ""
    assert h.counted() == 0


@pytest.mark.fr("FR-001", "FR-019")
def test_default_is_none_and_unsandboxed(h, tree):
    h.write_suite({"test_us1.py": _h8.US1_SUITE})
    res = h.run_sb(sandbox=False, counted=True)
    assert res.code == 1
    assert res.lines() == _h8.US1_FAIL_LINES
    assert res.err == ""
    rep = load_report(res.report_dir)
    assert rep["meta"].get("sandbox") == "none"


@pytest.mark.fr("FR-001")
def test_explicit_none_equals_default(h, tree):
    h.write_suite({"test_us1.py": _h8.US1_SUITE})
    a = h.run_sb(sandbox=False, counted=False)
    b = h.run(*h.std(sandbox=False, counted=False), "--sandbox", "none")
    assert (a.code, a.out, a.err) == (b.code, b.out, b.err)
    assert load_report(h.last_report)["meta"].get("sandbox") == "none"


@pytest.mark.docker
@pytest.mark.fr("FR-001")
def test_docker_mode_runs_pytest_in_a_container(h, tree, image, no_new_containers):
    h.write_suite({"test_probe.py": IN_CONTAINER_PROBE})
    res = h.run_sb()
    rep = load_report(res.report_dir)
    assert_toys_passed(rep, ["test_in_container_as_judge"])
    assert res.code == 0 and res.err == ""
    assert rep["meta"].get("sandbox") == "docker"


# -- FR-002 image --------------------------------------------------------------------------------

@pytest.mark.fr("FR-002")
def test_python_api_constants(mod):
    assert mod.SANDBOX_JUDGE_UID == 10101
    assert mod.SANDBOX_IMPL_UID == 10102
    text = mod.SANDBOX_DOCKERFILE
    assert isinstance(text, str)
    assert re.search(r"(?im)^\s*FROM\s+\S+", text), "SANDBOX_DOCKERFILE has no FROM line"


@pytest.mark.fr("FR-002")
def test_runner_is_still_one_file_with_embedded_dockerfile(runner_file, mod):
    src = runner_file.read_text()
    assert "SANDBOX_DOCKERFILE" in src
    scripts = runner_file.parent
    for p in scripts.iterdir():
        if p.name.lower().startswith("dockerfile") or p.suffix == ".dockerfile":
            assert "heldout" not in p.read_text(errors="replace").lower(), \
                "a separate sandbox Dockerfile ships next to the runner"


@pytest.mark.docker
@pytest.mark.fr("FR-002")
def test_image_tag_is_sha12_of_dockerfile(image, mod):
    import hashlib
    expect = "heldout-sandbox:" + hashlib.sha256(mod.SANDBOX_DOCKERFILE.encode()).hexdigest()[:12]
    assert image == expect
    assert _h8.image_id(expect) is not None


@pytest.mark.docker
@pytest.mark.fr("FR-002")
def test_report_meta_image_names_default_image(h, tree, image):
    h.write_suite({"test_x.py": _h8.ALL_PASS_SUITE})
    res = h.run_sb()
    assert res.code == 0
    meta = load_report(res.report_dir)["meta"]
    img = str(meta.get("image"))
    assert image in img or img == _h8.image_id(image) or img.split(":")[-1] in \
        (_h8.image_id(image) or ""), "meta.image does not name the sandbox image"


@pytest.mark.docker
@pytest.mark.fr("FR-002")
def test_existing_tag_is_not_rebuilt(h, tree, image):
    before = _h8.docker("image", "inspect", "--format", "{{.Id}} {{.Created}}", image).stdout
    h.write_suite({"test_x.py": _h8.ALL_PASS_SUITE})
    res = h.run_sb()
    assert res.code == 0
    after = _h8.docker("image", "inspect", "--format", "{{.Id}} {{.Created}}", image).stdout
    assert before.strip() and before == after, "image was rebuilt although its tag existed"


@pytest.mark.docker
@pytest.mark.fr("FR-002")
@pytest.mark.parametrize("what,script,expect", [
    ("python312", "python3 -c 'import sys; print(sys.version_info[:2])'", "(3, 12)"),
    ("uv", "uv --version >/dev/null && echo ok", "ok"),
    ("pytest", "python3 -c 'import pytest; print(\"ok\")'", "ok"),
    ("git", "git --version >/dev/null && echo ok", "ok"),
    ("judge", "id -u judge", "10101"),
    ("impl", "id -u impl", "10102"),
    ("launcher", "test -x /usr/local/bin/heldout-impl && echo ok", "ok"),
])
def test_image_contents(image, what, script, expect):
    r = _h8.in_image(image, script)
    assert r.returncode == 0, f"{what}: exit {r.returncode}"
    assert r.stdout.strip().splitlines()[-1:] == [expect], f"{what}: {r.stdout.strip()[-200:]}"


@pytest.mark.docker
@pytest.mark.fr("FR-002")
def test_sandbox_image_override_is_used(h, tree, image, no_new_containers):
    marker = h.token()
    ctx = _h8.mkd(h.base / "imgctx")
    (ctx / "Dockerfile").write_text(
        f"FROM {image}\nUSER 0\nRUN echo {marker} > /etc/h8-override-marker && "
        "chmod 0644 /etc/h8-override-marker\n")
    ref = f"h8override-{h.tok}:t"
    h.track_image(ref)
    r = _h8.docker("build", "-q", "-t", ref, str(ctx), timeout=600)
    assert r.returncode == 0, "harness could not build the override image"
    h.write_suite({"test_ovr.py": f'''
        import pytest


        @pytest.mark.fr("FR-002")
        def test_marker():
            with open("/etc/h8-override-marker") as fh:
                assert fh.read().strip() == "{marker}"
        '''})
    res = h.run_sb("--sandbox-image", ref)
    rep = load_report(res.report_dir)
    assert_toys_passed(rep, ["test_marker"])
    assert res.code == 0
    img = str(rep["meta"].get("image"))
    assert ref in img or img == _h8.image_id(ref), "meta.image does not name the override image"


# -- FR-003 docker unavailable ---------------------------------------------------------------------

def _no_docker_path(h) -> str:
    d = _h8.mkd(h.base / "nodocker")
    for tool in ("git", "sh", "env", "uv", "tar"):
        p = shutil.which(tool)
        if p:
            os.symlink(p, d / tool)
    os.symlink(sys.executable, d / "python3")
    return str(d)


@pytest.mark.fr("FR-003")
def test_docker_missing_is_infra_error(h, tree):
    h.write_suite({"test_x.py": _h8.ALL_PASS_SUITE})
    env = dict(h.env, PATH=_no_docker_path(h))
    res = h.run(*h.std(counted=True, report=False), env=env)
    assert_refused(res, "E_SANDBOX_UNAVAILABLE", 3)
    assert _h8.status_of(res) == "infra_error"
    assert h.counted() == 0


@pytest.mark.fr("FR-003")
def test_daemon_unreachable_is_infra_error(h, tree):
    h.write_suite({"test_x.py": _h8.ALL_PASS_SUITE})
    env = dict(h.env, DOCKER_HOST=f"unix://{h.base}/no-such-docker.sock")
    res = h.run(*h.std(counted=True, report=False), env=env)
    assert_refused(res, "E_SANDBOX_UNAVAILABLE", 3)
    assert _h8.status_of(res) == "infra_error"
    assert h.counted() == 0
    assert h.leftovers() == []


@pytest.mark.fr("FR-003", "FR-010")
def test_error_codes_extended(mod):
    assert {"E_SANDBOX_UNAVAILABLE", "E_SANDBOX_DOCKER"} <= set(mod.ERROR_CODES)
    old = {"E_USAGE", "E_FEATURE", "E_SUITE_MISSING", "E_SUITE_IN_TREE", "E_SUITE_PERMS",
           "E_REPORT_IN_TREE", "E_RUNNER_IN_TREE", "E_MAP", "E_GIT", "E_SYNC", "E_BUSY",
           "E_ROUND_CAP"}
    assert old <= set(mod.ERROR_CODES)


# -- FR-018 stated limits --------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("-\n", "-")).lower()


_LIMIT_KEYS = [
    ("network", r"network"),
    ("host", r"\bhost\b"),
    ("suite", r"\bsuite\b"),
    ("outlives", r"outliv|after the run|persist"),
    ("judge import forges", r"import"),
    ("forge", r"forg|tamper"),
    ("canaries", r"canar"),
    ("partly", r"\bpartly\b|\bpartial"),
    ("low-bandwidth", r"low[- ]bandwidth"),
    ("docker boundary", r"docker"),
    ("escapes", r"escape"),
    ("kernel", r"kernel"),
]


@pytest.mark.fr("FR-018")
@pytest.mark.parametrize("label,rx", _LIMIT_KEYS)
def test_limits_in_module_docstring(mod, label, rx):
    assert re.search(rx, _norm(mod.__doc__ or "")), f"docstring lacks: {label}"


@pytest.mark.fr("FR-018")
@pytest.mark.parametrize("label,rx", _LIMIT_KEYS)
def test_limits_in_help(h, label, rx):
    res = h.run("--help")
    assert res.code == 0
    assert re.search(rx, _norm(res.out)), f"--help lacks: {label}"


# -- FR-020 unchanged files ------------------------------------------------------------------------

@pytest.mark.fr("FR-020")
def test_wrapper_unchanged():
    assert _h8.sha256_file(_h8.WRAPPER) == _h8.WRAPPER_SHA256


@pytest.mark.fr("FR-020")
def test_run_heldout_sh_unchanged():
    assert _h8.sha256_file(_h8.REPO / "scripts" / "run-heldout.sh") == _h8.RUN_HELDOUT_SH_SHA256


@pytest.mark.fr("FR-020")
def test_wrapper_still_runs_the_runner(h):
    res = h.run("--version", wrapper=True)
    assert res.code == 0
    assert res.out.strip() == f"heldout-run {res.out.split()[1]} sha256:" + \
        _h8.sha256_file(_h8.RUNNER)
