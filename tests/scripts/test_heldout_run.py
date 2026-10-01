"""Spec 007: ``scripts/heldout_run.py`` (held-out runner) on toy repos and suites in temp dirs.

Every test sets HOME, HELDOUT_ROOT and TMPDIR to temp directories and uses ``--profile generic``
(the sync-failure test uses the moeka profile with a fake ``uv`` on PATH).
"""

from __future__ import annotations

import ast
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import site
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "heldout_run.py"
WRAPPER = REPO_ROOT / "scripts" / "heldout-run"
FEATURE = "001-demo"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _load():
    spec = importlib.util.spec_from_file_location("heldout_run_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hr = _load()

ABS_PATH_RX = re.compile(r"(?<![0-9A-Za-z-])/[A-Za-z0-9._-]+")


# -- fixture builders -------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c",
         "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, text=True, env=env,
    ).stdout


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
        (self.repo / "README.md").write_text("demo\n")
        _git(self.repo, "init", "-q")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")

    def suite(self, files: dict[str, str], *, repo_name: str = "demo", feature: str = FEATURE,
              path: Path | None = None) -> Path:
        d = path or (self.root / repo_name / feature)
        d.mkdir(parents=True, exist_ok=True)
        for p in [d, *d.parents]:
            if p == self.root.parent:
                break
            p.chmod(0o700)
        for name, body in files.items():
            (d / name).write_text(textwrap.dedent(body))
        return d

    def env(self, **extra: str) -> dict[str, str]:
        e = dict(os.environ)
        e.update(HOME=str(self.home), HELDOUT_ROOT=str(self.root), TMPDIR=str(self.tmpdir))
        e.pop("PYTEST_ADDOPTS", None)
        # HOME moves, so keep a user-site pytest importable (host runs; Docker uses a venv).
        if site.USER_BASE:
            e.setdefault("PYTHONUSERBASE", site.USER_BASE)
        e.update(extra)
        return e

    def run(self, *args: str, env: dict | None = None, timeout: float = 120,
            runner: Path = RUNNER) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(runner), *args],
            capture_output=True, text=True, env=env or self.env(), timeout=timeout,
        )

    def run_wt(self, *extra: str, suite: Path | None = None, **kw) -> subprocess.CompletedProcess:
        args = ["run", "--profile", "generic", "--repo-name", "demo", "--feature", FEATURE,
                "--worktree", str(self.repo)]
        if suite is not None:
            args += ["--suite", str(suite)]
        return self.run(*args, *extra, **kw)

    @property
    def report_root(self) -> Path:
        return self.root / "_reports" / "demo" / FEATURE


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


@pytest.fixture
def h(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _tree_state(root: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if ".git" in p.parts[len(root.parts):]:
            continue
        out[rel] = os.readlink(p).encode() if p.is_symlink() else (p.read_bytes() if p.is_file() else b"<d>")
    return out


def _report(h: Env, name: str = "round-01") -> dict:
    return json.loads((h.report_root / name / "report.json").read_text())


# -- FR-001 / FR-002 packaging -------------------------------------------------------------------


def test_module_imports_are_stdlib_only():
    tree = ast.parse(RUNNER.read_text())
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        for n in names:
            assert n in sys.stdlib_module_names or n == "__future__", n
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                assert m.split(".")[0] not in {"moeka", "nanobot", "awr", "pytest"}, m


def test_version_line_and_wrapper(h):
    digest = hashlib.sha256(RUNNER.read_bytes()).hexdigest()
    p = subprocess.run([str(WRAPPER), "--version"], capture_output=True, text=True, env=h.env())
    assert p.returncode == 0 and p.stderr == ""
    assert p.stdout == f"heldout-run {hr.VERSION} sha256:{digest}\n"
    assert os.access(WRAPPER, os.X_OK)


def test_help_states_the_limit(h):
    p = h.run("--help")
    assert p.returncode == 0
    assert "not a sandbox" in p.stdout
    assert "not a sandbox" in (hr.__doc__ or "")
    p = h.run("run", "--help")
    assert "not a sandbox" in p.stdout


def test_usage_error_never_echoes_values(h):
    p = h.run("run", "--repo-name", "demo", "--feature", FEATURE, "--bogus-zzqx", "secretval")
    assert p.returncode == 2
    assert p.stderr == "heldout-run: error E_USAGE\n" and p.stdout == ""
    p = h.run("run", "--repo-name", "demo", "--feature", FEATURE, "--timeout", "notanumberqq")
    assert p.stderr == "heldout-run: error E_USAGE\n"


@pytest.mark.parametrize("feature,repo", [("1-demo", "demo"), ("001-Demo", "demo"),
                                          ("001-demo", "Demo"), ("001-demo", "_x")])
def test_bad_names_are_refused(h, feature, repo):
    h.suite(PASS_SUITE)
    p = h.run("run", "--profile", "generic", "--repo-name", repo, "--feature", feature,
              "--worktree", str(h.repo))
    assert (p.returncode, p.stderr, p.stdout) == (2, "heldout-run: error E_FEATURE\n", "")


# -- US1: run and feedback ----------------------------------------------------------------------


def test_scenario_failing_suite_exact_feedback(h):
    h.suite(DEMO_SUITE)
    p = h.run_wt()
    assert p.returncode == 1, p
    assert p.stdout == (
        "heldout-run: 001-demo round 1/4: failed\n"
        "FR-002: 2/4 failing\n"
        "total: 2/6 tests failing, 0 skipped; 1/2 ids failing\n"
    )
    assert p.stderr == ""


def test_all_passing_suite(h):
    h.suite(PASS_SUITE)
    p = h.run_wt()
    assert p.returncode == 0
    assert p.stdout == ("heldout-run: 001-demo round 1/4: passed\n"
                        "total: 0/2 tests failing, 0 skipped; 0/2 ids failing\n")


def test_repo_ref_mode_uses_committed_tree_and_records_commit(h):
    h.suite({"test_v.py": """
        import pytest, demo, os
        @pytest.mark.fr("FR-001")
        def test_committed_value():
            assert demo.value() == 1
        @pytest.mark.fr("FR-002")
        def test_no_git():
            here = os.path.dirname(demo.__file__)
            assert not os.path.exists(os.path.join(here, ".git"))
            assert not os.path.exists(os.path.join(here, "untracked.txt"))
    """})
    commit = _git(h.repo, "rev-parse", "HEAD").strip()
    (h.repo / "demo.py").write_text("def value():\n    return 2\n")  # uncommitted edit
    (h.repo / "untracked.txt").write_text("x")
    p = h.run("run", "--profile", "generic", "--repo-name", "demo", "--feature", FEATURE,
              "--repo", str(h.repo), "--ref", "HEAD")
    assert p.returncode == 0, p.stdout
    meta = _report(h)["meta"]
    assert meta["tree"]["commit"] == commit and meta["commit"] == commit


def test_bad_ref_is_e_git(h):
    h.suite(PASS_SUITE)
    p = h.run("run", "--profile", "generic", "--repo-name", "demo", "--feature", FEATURE,
              "--repo", str(h.repo), "--ref", "no-such-ref")
    assert (p.returncode, p.stderr, p.stdout) == (2, "heldout-run: error E_GIT\n", "")
    assert list(h.tmpdir.iterdir()) == []


def test_worktree_copy_contents_and_exclusions(h):
    (h.repo / "demo.py").write_text("def value():\n    return 3\n")
    (h.repo / "new.txt").write_text("untracked")
    (h.repo / "link.txt").symlink_to("README.md")
    for d in (".venv", "node_modules", "pkg/__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"):
        (h.repo / d).mkdir(parents=True)
        (h.repo / d / "junk").write_text("j")
    (h.repo / "pkg" / "stale.pyc").write_bytes(b"x")
    h.suite({"test_c.py": """
        import os, pytest, demo
        ROOT = os.path.dirname(demo.__file__)
        @pytest.mark.fr("FR-007")
        def test_copy():
            assert demo.value() == 3
            assert open(os.path.join(ROOT, "new.txt")).read() == "untracked"
            assert os.path.islink(os.path.join(ROOT, "link.txt"))
            assert os.readlink(os.path.join(ROOT, "link.txt")) == "README.md"
            for d in (".git", ".venv", "node_modules", "pkg/__pycache__", ".pytest_cache",
                      ".mypy_cache", ".ruff_cache", "pkg/stale.pyc"):
                assert not os.path.lexists(os.path.join(ROOT, d)), d
    """})
    before = _tree_state(h.repo)
    p = h.run_wt()
    assert p.returncode == 0, p.stdout
    assert _tree_state(h.repo) == before


def test_existing_heldout_dir_in_tree_is_replaced_and_suite_dest(h):
    stale = h.repo / "tests" / "heldout" / FEATURE
    stale.mkdir(parents=True)
    (stale / "test_stale.py").write_text("def test_stale():\n    assert False\n")
    h.suite(PASS_SUITE)
    p = h.run_wt()
    assert p.returncode == 0 and "total: 0/2" in p.stdout
    # pytest is pointed at the dest only, so the stale tree copy is not collected.
    p = h.run_wt("--suite-dest", "elsewhere/suite", "--no-count", "--keep")
    assert p.returncode == 0 and "total: 0/2" in p.stdout
    kept = Path(json.loads(next(h.report_root.glob("run-*/report.json")).read_text())["meta"][
        "kept_copy"])
    assert (kept / "elsewhere" / "suite" / "test_ok.py").exists()
    shutil.rmtree(kept)


def test_suite_dest_outside_copy_is_refused(h):
    h.suite(PASS_SUITE)
    p = h.run_wt("--suite-dest", "../escape")
    assert p.returncode == 2 and p.stderr == "heldout-run: error E_USAGE\n"


def test_child_environment(h):
    h.suite({"test_env.py": """
        import os, pytest
        @pytest.mark.fr("FR-012")
        def test_env():
            assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
            assert os.environ["HELDOUT_ROUND"] == "1"
            assert os.environ.get("HELDOUT_RUN_RECORDS")
            assert "DROP_ME_QQ" not in os.environ
            assert os.environ["KEEP_ME_QQ"] == "1"
            parts = os.environ["PYTHONPATH"].split(os.pathsep)
            assert parts[-1] == "/inherited/entry"
            assert os.path.isfile(os.path.join(parts[1], "heldout_run.py"))
            assert os.path.isfile(os.path.join(parts[0], "demo.py"))
            assert not os.path.exists(os.path.join(os.getcwd(), "demo.py"))  # scratch cwd
    """})
    p = h.run_wt("--unset", "DROP_ME_QQ", env=h.env(DROP_ME_QQ="1", KEEP_ME_QQ="1",
                                                     PYTHONPATH="/inherited/entry"))
    assert p.returncode == 0, p.stdout
    cmd = _report(h)["meta"]["pytest_command"]
    assert cmd[1:3] == ["-m", "pytest"] and cmd[4:9] == ["-q", "-p", "no:cacheprovider", "-p",
                                                         "heldout_run"]
    assert cmd[9].startswith("--junitxml=") and cmd[9].endswith("round-01/junit.xml")


def test_pytest_arg_values_are_appended(h):
    h.suite(DEMO_SUITE)
    p = h.run_wt("--pytest-arg", "-k", "--pytest-arg", "test_one or test_two")
    assert p.returncode == 0, p.stdout
    assert "total: 0/2 tests failing" in p.stdout


# -- mapping -----------------------------------------------------------------------------------


UNMARKED = {
    "test_u.py": """
        import pytest
        def test_alpha():
            assert True
        @pytest.mark.parametrize("v", [1, 2])
        def test_beta(v):
            assert v == 1
        def test_gamma():
            assert False
        class TestThing:
            def test_delta(self):
                assert False
    """,
}


def test_spec_map_json_mode(h):
    files = dict(UNMARKED)
    files["SPEC-MAP.json"] = json.dumps({"map": {"test_alpha": ["FR-001"], "test_beta": ["FR-002"],
                                                 "test_gamma": ["SC-001"], "test_delta": ["FR-002"]}})
    h.suite(files)
    p = h.run_wt()
    assert p.returncode == 1
    assert p.stdout.splitlines()[1:] == ["FR-002: 2/3 failing", "SC-001: 1/1 failing",
                                         "total: 3/5 tests failing, 0 skipped; 2/3 ids failing"]


def test_fr_report_mode_is_never_executed_and_matches_fr_report(h, tmp_path):
    sentinel = tmp_path / "EXECUTED"
    files = dict(UNMARKED)
    files["fr_report.py"] = f"""
        import sys, xml.etree.ElementTree as ET
        from collections import Counter
        open({str(sentinel)!r}, "w").write("x")
        MAP: dict[str, list[str]] = {{
            "test_alpha": ["FR-001"],
            "test_beta": ["FR-002", "FR-003a"],
            "test_gamma": ["FR-003a"],
        }}

        def main(path):
            root = ET.parse(path).getroot()
            total, failing = Counter(), Counter()
            for tc in root.iter("testcase"):
                ids = MAP.get(tc.get("name", "").split("[", 1)[0])
                if ids is None:
                    continue
                bad = any(tc.find(t) is not None for t in ("failure", "error"))
                for i in ids:
                    total[i] += 1
                    failing[i] += bad
            for i in sorted(total):
                if failing[i]:
                    print(f"{{i}}: {{failing[i]}} failing of {{total[i]}}")

        if __name__ == "__main__":
            main(sys.argv[1])
    """
    suite = h.suite(files)
    p = h.run_wt()
    assert p.returncode == 1
    assert not sentinel.exists()
    lines = p.stdout.splitlines()
    assert lines[1:4] == ["FR-002: 1/2 failing", "FR-003a: 2/3 failing", "UNMAPPED: 1/1 failing"]
    junit = h.report_root / "round-01" / "junit.xml"
    ref = subprocess.run([sys.executable, str(suite / "fr_report.py"), str(junit)],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    sentinel.unlink()
    assert ref == ["FR-002: 1 failing of 2", "FR-003a: 2 failing of 3"]


def test_markers_and_map_are_unioned(h):
    h.suite({
        "test_m.py": """
            import pytest
            @pytest.mark.fr("FR-001")
            def test_x():
                assert False
        """,
        "SPEC-MAP.json": json.dumps({"test_x": ["FR-009"]}),
    })
    p = h.run_wt()
    assert p.stdout.splitlines()[1:3] == ["FR-001: 1/1 failing", "FR-009: 1/1 failing"]


def test_map_flag_overrides_and_bad_map_is_e_map(h, tmp_path):
    h.suite({**UNMARKED, "SPEC-MAP.json": json.dumps({"test_gamma": ["FR-001"]})})
    alt = tmp_path / "alt.json"
    alt.write_text(json.dumps({"test_gamma": ["NFR-004"]}))
    p = h.run_wt("--map", str(alt), "--no-count")
    assert "NFR-004: 1/1 failing" in p.stdout and "FR-001" not in p.stdout
    alt.write_text("{not json")
    p = h.run_wt("--map", str(alt))
    assert (p.returncode, p.stderr, p.stdout) == (2, "heldout-run: error E_MAP\n", "")
    (h.root / "demo" / FEATURE / "SPEC-MAP.json").write_text(json.dumps({"test_x": 5}))
    p = h.run_wt()
    assert (p.returncode, p.stderr) == (2, "heldout-run: error E_MAP\n")


def test_invalid_ids_unmapped_ordering_and_multi_ids(h):
    h.suite({"test_o.py": """
        import pytest
        @pytest.mark.fr("SC-002", "FR-010")
        def test_a():
            assert False
        @pytest.mark.fr("covers secret free text qqzz")
        def test_b():
            assert False
        def test_c():
            assert False
        @pytest.mark.fr("NFR-001")
        @pytest.mark.fr("FR-002b")
        def test_d():
            assert False
        @pytest.mark.fr("FR-002a")
        def test_e():
            assert False
        @pytest.mark.fr("FR-002")
        def test_f():
            assert False
    """})
    p = h.run_wt()
    assert p.stdout.splitlines()[1:] == [
        "FR-002: 1/1 failing", "FR-002a: 1/1 failing", "FR-002b: 1/1 failing",
        "FR-010: 1/1 failing", "NFR-001: 1/1 failing", "SC-002: 1/1 failing",
        "INVALID_ID: 1/1 failing", "UNMAPPED: 1/1 failing",
        "total: 6/6 tests failing, 0 skipped; 8/8 ids failing",
    ]
    assert "qqzz" not in p.stdout


def test_outcome_rules(h):
    h.suite({"test_r.py": """
        import pytest
        @pytest.fixture
        def bad_setup():
            raise RuntimeError("boom")
        @pytest.fixture
        def bad_teardown():
            yield
            raise RuntimeError("boom")
        @pytest.mark.fr("FR-001")
        def test_skip():
            pytest.skip("no")
        @pytest.mark.fr("FR-001")
        @pytest.mark.xfail(reason="known")
        def test_xfail():
            assert False
        @pytest.mark.fr("FR-002")
        @pytest.mark.xfail(strict=True, reason="x")
        def test_xpass_strict():
            assert True
        @pytest.mark.fr("FR-003")
        def test_setup_error(bad_setup):
            pass
        @pytest.mark.fr("FR-004")
        def test_teardown_error(bad_teardown):
            pass
        @pytest.mark.fr("FR-005")
        def test_plain():
            pass
    """})
    p = h.run_wt()
    assert p.returncode == 1
    assert p.stdout.splitlines()[1:] == [
        "FR-002: 1/1 failing", "FR-003: 1/1 failing", "FR-004: 1/1 failing",
        "total: 3/5 tests failing, 1 skipped; 3/5 ids failing",
    ]


def test_json_feedback_and_feedback_file(h, tmp_path):
    h.suite(DEMO_SUITE)
    ff = tmp_path / "fb.json"
    p = h.run_wt("--json", "--feedback-file", str(ff))
    obj = json.loads(p.stdout)
    assert obj == json.loads(ff.read_text())
    assert set(obj) == {"schema", "feature", "round", "round_cap", "status", "cap_reached",
                        "failing", "totals"}
    assert obj["schema"] == "heldout-feedback.v1" and obj["round"] == 1 and obj["round_cap"] == 4
    assert obj["failing"] == {"FR-002": {"failing": 2, "total": 4}}
    assert obj["totals"] == {"tests": 6, "failing": 2, "skipped": 0, "ids": 2, "ids_failing": 1}
    assert obj["cap_reached"] is False


# -- statuses ------------------------------------------------------------------------------------


HANG_SUITE = {"test_h.py": """
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


def test_timeout_ignoring_sigterm(h):
    h.suite(HANG_SUITE)
    t0 = time.monotonic()
    p = h.run_wt("--timeout", "3", timeout=60)
    assert time.monotonic() - t0 < 3 + 15
    assert p.returncode == 3 and p.stderr == ""
    assert p.stdout.splitlines() == ["heldout-run: 001-demo round 1/4: timeout",
                                     "FR-013: 2/2 failing",
                                     "total: 2/3 tests failing, 0 skipped; 1/2 ids failing"]
    assert list(h.tmpdir.iterdir()) == []
    rounds = json.loads((h.report_root / "rounds.json").read_text())
    assert [r["status"] for r in rounds["rounds"]] == ["timeout"]


def test_collection_error_counts_a_round(h):
    h.suite({"test_bad.py": "import no_such_module_qq\n\ndef test_x():\n    pass\n"})
    p = h.run_wt()
    assert p.returncode == 3 and p.stderr == ""
    assert p.stdout.splitlines()[0] == "heldout-run: 001-demo round 1/4: collection_error"
    rounds = json.loads((h.report_root / "rounds.json").read_text())
    assert len(rounds["rounds"]) == 1


def test_sync_failure_is_infra_error_not_counted(h, tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uv").write_text("#!/bin/sh\necho 'sync secret qqsync' >&2\nexit 1\n")
    (fake / "uv").chmod(0o755)
    h.suite(PASS_SUITE)
    env = h.env(PATH=f"{fake}:{os.environ['PATH']}")
    p = h.run("run", "--profile", "moeka", "--repo-name", "demo", "--feature", FEATURE,
              "--worktree", str(h.repo), env=env)
    assert p.returncode == 3 and p.stderr == "heldout-run: error E_SYNC\n"
    assert p.stdout.splitlines()[0] == "heldout-run: 001-demo round -/4: infra_error"
    assert "qqsync" not in p.stdout + p.stderr
    assert not (h.report_root / "rounds.json").exists()
    runs = [d.name for d in h.report_root.iterdir() if d.name.startswith("run-")]
    assert len(runs) == 1
    assert "qqsync" in (h.report_root / runs[0] / "sync.log").read_text()
    assert list(h.tmpdir.iterdir()) == []


def test_concurrent_run_is_busy(h):
    h.suite(PASS_SUITE)
    h.report_root.mkdir(parents=True)
    fd = os.open(h.report_root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        p = h.run_wt()
    finally:
        os.close(fd)
    assert p.returncode == 3 and p.stderr == "heldout-run: error E_BUSY\n"
    assert "infra_error" in p.stdout.splitlines()[0]
    assert not (h.report_root / "rounds.json").exists()


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_signal_cleans_scratch(h, sig):
    h.suite(HANG_SUITE)
    proc = subprocess.Popen([sys.executable, str(RUNNER), "run", "--profile", "generic",
                             "--repo-name", "demo", "--feature", FEATURE, "--worktree",
                             str(h.repo)], env=h.env(), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        recs = list(h.tmpdir.glob("heldout-run-*/records.jsonl"))
        if recs and 'test_hang", "when": "setup"' in recs[0].read_text():
            break
        time.sleep(0.2)
    time.sleep(1)
    proc.send_signal(sig)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 3
    assert list(h.tmpdir.iterdir()) == []
    assert err == "heldout-run: error E_SYNC\n"
    assert out.splitlines()[0] == "heldout-run: 001-demo round -/4: infra_error"
    _assert_clean(out, err)


# -- US2: redaction and isolation ----------------------------------------------------------------


CANARY = "cnry7f3a"
CANARY_SUITE = {
    f"test_{CANARY}_file.py": f"""
        import pytest, os
        SECRET = "{CANARY}_literal"

        @pytest.fixture
        def {CANARY}_fixture():
            return "{CANARY}_fixture_value"

        @pytest.mark.fr("FR-001", "{CANARY} marker free text")
        @pytest.mark.parametrize("v", ["{CANARY}_param"], ids=["{CANARY}_pid"])
        def test_{CANARY}_name(v, {CANARY}_fixture):
            print("{CANARY}_printed", os.getcwd())
            import sys; print("{CANARY}_stderr", file=sys.stderr)
            mode = os.environ.get("CANARY_MODE", "fail")
            if mode == "hang":
                import time
                time.sleep(600)
            assert mode == "pass", "{CANARY}_assertion " + SECRET + {CANARY}_fixture
    """,
}


def _assert_clean(*texts: str) -> None:
    for t in texts:
        assert CANARY not in t
        assert ABS_PATH_RX.search(t) is None, t


@pytest.mark.parametrize("mode", ["pass", "fail", "hang", "collect", "sync", "busy", "refuse"])
def test_canary_redaction_in_every_outcome(h, tmp_path, mode):
    suite = h.suite(CANARY_SUITE)
    ff = tmp_path / "fb.json"
    extra = ["--feedback-file", str(ff)]
    env = h.env(CANARY_MODE=mode)
    args = ["run", "--profile", "generic", "--repo-name", "demo", "--feature", FEATURE,
            "--worktree", str(h.repo)]
    if mode == "hang":
        extra += ["--timeout", "2"]
    if mode == "collect":
        (suite / f"test_{CANARY}_broken.py").write_text(f"raise ImportError('{CANARY}_import')\n")
    if mode == "sync":
        fake = tmp_path / "fakebin"
        fake.mkdir()
        (fake / "uv").write_text(f"#!/bin/sh\necho {CANARY}_sync\nexit 1\n")
        (fake / "uv").chmod(0o755)
        env["PATH"] = f"{fake}:{env['PATH']}"
        args[2] = "moeka"
    lock_fd = None
    if mode == "busy":
        h.report_root.mkdir(parents=True)
        lock_fd = os.open(h.report_root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    if mode == "refuse":
        suite.chmod(0o755)
    try:
        p = h.run(*args, *extra, env=env, timeout=90)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
    texts = [p.stdout, p.stderr]
    if ff.exists():
        texts.append(ff.read_text())
    _assert_clean(*texts)
    for json_mode in (["--json"],):
        if mode in ("pass", "fail"):
            q = h.run(*args, "--no-count", *json_mode, env=env)
            _assert_clean(q.stdout, q.stderr)


def test_suite_inside_worktree_is_refused_before_copy(h):
    suite = h.suite(PASS_SUITE, path=h.repo / "hidden")
    suite.chmod(0o755)  # placement is reported before permissions
    p = h.run_wt(suite=suite)
    assert (p.returncode, p.stderr, p.stdout) == (2, "heldout-run: error E_SUITE_IN_TREE\n", "")
    assert list(h.tmpdir.iterdir()) == []


def test_suite_inside_other_worktree_or_forbidden(h, tmp_path):
    other = tmp_path / "other-wt"
    _git(h.repo, "worktree", "add", "-q", str(other), "-b", "other")
    suite = h.suite(PASS_SUITE, path=other / "x")
    p = h.run_wt(suite=suite)
    assert p.stderr == "heldout-run: error E_SUITE_IN_TREE\n" and p.returncode == 2
    p = h.run("run", "--profile", "generic", "--repo-name", "demo", "--feature", FEATURE,
              "--repo", str(h.repo), "--ref", "HEAD", "--suite", str(suite))
    assert p.stderr == "heldout-run: error E_SUITE_IN_TREE\n"
    good = h.suite(PASS_SUITE)
    p = h.run_wt("--forbid-under", str(h.root))
    assert p.stderr == "heldout-run: error E_SUITE_IN_TREE\n"
    p = h.run_wt("--report-dir", str(h.repo / "rep"), suite=good)
    assert p.stderr == "heldout-run: error E_REPORT_IN_TREE\n" and p.returncode == 2
    assert not (h.repo / "rep").exists()


def test_runner_inside_worktree_is_refused(h):
    h.suite(PASS_SUITE)
    inner = h.repo / "scripts"
    inner.mkdir()
    shutil.copy(RUNNER, inner / "heldout_run.py")
    p = h.run_wt(runner=inner / "heldout_run.py")
    assert (p.returncode, p.stderr) == (2, "heldout-run: error E_RUNNER_IN_TREE\n")


def test_permissions_and_missing_suite(h):
    suite = h.suite(PASS_SUITE)
    suite.chmod(0o750)
    p = h.run_wt()
    assert (p.returncode, p.stderr) == (2, "heldout-run: error E_SUITE_PERMS\n")
    suite.chmod(0o700)
    h.root.chmod(0o711)
    p = h.run_wt()
    assert p.stderr == "heldout-run: error E_SUITE_PERMS\n"
    h.root.chmod(0o700)
    p = h.run_wt(suite=h.tmp / "nope")
    assert (p.returncode, p.stderr) == (2, "heldout-run: error E_SUITE_MISSING\n")


def test_check_isolation(h, tmp_path):
    suite = h.suite(PASS_SUITE)
    p = h.run("check-isolation", "--suite", str(suite), "--repo", str(h.repo))
    assert p.returncode == 0 and p.stderr == ""
    p = h.run("check-isolation", "--suite", str(suite), "--forbid-under", str(h.root))
    assert (p.returncode, p.stderr) == (2, "heldout-run: error E_SUITE_IN_TREE\n")
    inside = h.suite(PASS_SUITE, path=h.repo / "s")
    p = h.run("check-isolation", "--suite", str(inside), "--repo", str(h.repo))
    assert (p.returncode, p.stderr) == (2, "heldout-run: error E_SUITE_IN_TREE\n")
    suite.chmod(0o755)
    p = h.run("check-isolation", "--suite", str(suite))
    assert (p.returncode, p.stderr) == (2, "heldout-run: error E_SUITE_PERMS\n")


@pytest.mark.parametrize("mode", ["pass", "fail", "hang"])
def test_source_and_suite_unchanged_scratch_removed(h, tmp_path, mode):
    suite = h.suite(CANARY_SUITE)
    scratch = tmp_path / "own-scratch"
    scratch.mkdir()
    before_suite, before_repo = _tree_state(suite), _tree_state(h.repo)
    p = h.run_wt("--scratch", str(scratch), "--timeout", "3", env=h.env(CANARY_MODE=mode))
    assert p.returncode in (0, 1, 3)
    assert _tree_state(suite) == before_suite and _tree_state(h.repo) == before_repo
    assert list(scratch.iterdir()) == [] and list(h.tmpdir.iterdir()) == []


def test_keep_keeps_copy_and_records_it_privately(h):
    h.suite(PASS_SUITE)
    p = h.run_wt("--keep")
    assert p.returncode == 0
    kept = Path(_report(h)["meta"]["kept_copy"])
    assert (kept / "demo.py").exists() and not (kept / ".git").exists()
    assert (kept / "tests" / "heldout" / FEATURE / "test_ok.py").exists()
    assert str(kept) not in p.stdout
    shutil.rmtree(kept)


# -- private report ------------------------------------------------------------------------------


def test_report_layout_and_modes(h):
    h.suite(DEMO_SUITE)
    h.run_wt()
    d = h.report_root / "round-01"
    names = {p.name for p in d.iterdir()}
    assert {"report.json", "junit.xml", "pytest.log", "feedback.json"} <= names
    assert d.stat().st_mode & 0o777 == 0o700
    for p in d.iterdir():
        assert p.stat().st_mode & 0o777 == 0o600, p.name
    rep = json.loads((d / "report.json").read_text())
    assert rep["schema"] == "heldout-report.v1"
    meta = rep["meta"]
    assert meta["runner_sha256"] == hashlib.sha256(RUNNER.read_bytes()).hexdigest()
    assert meta["status"] == "failed" and meta["pytest_exit_code"] == 1 and meta["round"] == 1
    assert len(meta["suite_sha256"]) == 64 and meta["tree"]["mode"] == "worktree"
    t = {x["nodeid"].split("::")[-1]: x for x in rep["tests"]}
    assert t["test_three[3]"]["outcome"] == "failed" and t["test_three[3]"]["key"] == "test_three"
    assert "assert 3 <= 2" in t["test_three[3]"]["longrepr"]
    assert json.loads((d / "feedback.json").read_text())["failing"] == {
        "FR-002": {"failing": 2, "total": 4}}
    h.run_wt("--no-count")
    runs = [p for p in h.report_root.iterdir() if re.fullmatch(r"run-\d{8}T\d{6}Z", p.name)]
    assert len(runs) == 1


# -- US4: rounds ---------------------------------------------------------------------------------


def test_round_cap_escalation_and_refusal(h):
    h.suite(DEMO_SUITE)
    for n in range(1, 4):
        p = h.run_wt()
        assert p.stdout.startswith(f"heldout-run: 001-demo round {n}/4: failed\n")
        assert "escalate" not in p.stdout
    p = h.run_wt("--no-count")
    assert p.stdout.startswith("heldout-run: 001-demo round -/4: failed\n")
    p = h.run_wt()
    assert p.stdout.splitlines()[-1] == "round cap reached: escalate to owner"
    p = h.run_wt("--json")
    assert (p.returncode, p.stderr, p.stdout) == (4, "heldout-run: error E_ROUND_CAP\n", "")
    p = h.run_wt("--no-count")
    assert p.returncode == 1
    p = h.run("rounds", "--repo-name", "demo", "--feature", FEATURE)
    assert p.stdout.splitlines() == ["round 1: failed FR-002"] * 0 + [
        f"round {n}: failed FR-002" for n in range(1, 5)]
    p = h.run("rounds", "--repo-name", "demo", "--feature", FEATURE, "--reset")
    assert p.returncode == 2 and p.stderr == "heldout-run: error E_USAGE\n"
    p = h.run("rounds", "--repo-name", "demo", "--feature", FEATURE, "--reset", "--reason",
              "tester-bug")
    assert p.returncode == 0
    doc = json.loads((h.report_root / "rounds.json").read_text())
    assert doc["schema"] == "heldout-rounds.v1" and doc["rounds"] == []
    assert doc["resets"][0]["reason"] == "tester-bug" and len(doc["resets"][0]["rounds"]) == 4
    p = h.run_wt()
    assert p.stdout.startswith("heldout-run: 001-demo round 1/4: failed\n")


def test_cap_flag_and_passing_at_cap(h):
    h.suite(PASS_SUITE)
    p = h.run_wt("--cap", "1")
    assert p.returncode == 0 and "escalate" not in p.stdout
    p = h.run_wt("--cap", "1")
    assert p.returncode == 4


# -- US3: triage ---------------------------------------------------------------------------------


SPEC_MD = """\
# Spec

- **FR-001**: The thing MUST work.
- **FR-002 (held-out, exact)**: Values MUST be at most two.
  Continuation line of FR-002.

  | a | b |
  |---|---|
  | 1 | 2 |

  - nested bullet of FR-002
- **FR-003**: Next requirement.

Paragraph after.

## Heading
"""


def test_triage_text_and_json(h, tmp_path):
    h.suite(DEMO_SUITE)
    h.run_wt()
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC_MD)
    p = h.run("triage", "--repo-name", "demo", "--feature", FEATURE, "--id", "FR-002",
              "--spec", str(spec))
    assert p.returncode == 0
    lines = p.stdout.splitlines()
    assert lines[0] == "PRIVATE TRIAGE: not for the implementer"
    headers = [ln for ln in lines if ln.startswith("== ")]
    assert headers == [f"== FR-002: tests/heldout/{FEATURE}/test_demo.py::test_three[{x}]"
                       for x in (3, 4)] or len(headers) == 2
    assert "E       assert 3 <= 2" in p.stdout
    assert "  - nested bullet of FR-002" in p.stdout and "FR-003" not in p.stdout
    q = h.run("triage", "--repo-name", "demo", "--feature", FEATURE, "--round", "1",
              "--spec", str(spec), "--json")
    obj = json.loads(q.stdout)
    assert set(obj) == {"schema", "entries"} and obj["schema"] == "heldout-triage.v1"
    data = obj["entries"]
    assert len(data) == 2 and set(data[0]) == {"id", "nodeid", "assertion", "spec"}
    assert data[0]["spec"] == hr.extract_requirement(SPEC_MD, "FR-002")
    assert len(json.loads((h.report_root / "rounds.json").read_text())["rounds"]) == 1
    p = h.run("triage", "--repo-name", "demo", "--feature", FEATURE, "--id", "FR-001",
              "--spec", str(spec))
    assert p.stdout.splitlines() == ["PRIVATE TRIAGE: not for the implementer"]


def test_extract_requirement_grammar():
    text = hr.extract_requirement(SPEC_MD, "FR-002")
    assert text.splitlines()[0] == "- **FR-002 (held-out, exact)**: Values MUST be at most two."
    assert text.splitlines()[-1] == "  - nested bullet of FR-002"
    assert "| 1 | 2 |" in text
    assert hr.extract_requirement(SPEC_MD, "FR-001") == "- **FR-001**: The thing MUST work."
    assert hr.extract_requirement(SPEC_MD, "FR-003") == "- **FR-003**: Next requirement."
    assert hr.extract_requirement(SPEC_MD, "FR-00") is None
    assert hr.extract_requirement(SPEC_MD, "FR-009") is None
    assert hr.extract_requirement("- **FR-0011**: x\n", "FR-001") is None


# -- Python API ----------------------------------------------------------------------------------


def test_api_summarize_and_format():
    recs = [
        {"nodeid": "a", "key": "a", "ids": ["FR-001"], "outcome": "passed"},
        {"nodeid": "b", "key": "b", "ids": ["FR-001", "SC-001"], "outcome": "failed"},
        {"nodeid": "c", "key": "c", "ids": [], "outcome": "skipped"},
        {"nodeid": "d", "key": "d", "ids": ["bogus"], "outcome": "failed"},
    ]
    fb = hr.summarize(recs, feature=FEATURE, round=4, cap=4, status="failed")
    assert fb["failing"] == {"FR-001": {"failing": 1, "total": 2},
                             "SC-001": {"failing": 1, "total": 1},
                             "INVALID_ID": {"failing": 1, "total": 1}}
    assert fb["totals"] == {"tests": 3, "failing": 2, "skipped": 1, "ids": 3, "ids_failing": 3}
    assert fb["cap_reached"] is True
    assert hr.format_feedback(fb).splitlines() == [
        "heldout-run: 001-demo round 4/4: failed", "FR-001: 1/2 failing", "SC-001: 1/1 failing",
        "INVALID_ID: 1/1 failing", "total: 2/3 tests failing, 1 skipped; 3/3 ids failing",
        "round cap reached: escalate to owner"]
    assert hr.FEEDBACK_SCHEMA == "heldout-feedback.v1"
    assert re.match(hr.ID_PATTERN, "NFR-123a") and not re.match(hr.ID_PATTERN, "FR-12")
    assert hr.ID_PATTERN.match("SC-001")
    assert "E_ROUND_CAP" in hr.ERROR_CODES and len(hr.ERROR_CODES) == 12


def test_api_load_id_map(tmp_path):
    j = tmp_path / "m.json"
    j.write_text(json.dumps({"t": ["FR-001", "FR-002"]}))
    assert hr.load_id_map(j) == {"t": ("FR-001", "FR-002")}
    py = tmp_path / "fr_report.py"
    py.write_text("import os\nos.system('false')\nMAP = {'t': ['FR-003']}\n")
    assert hr.load_id_map(py) == {"t": ("FR-003",)}
    py.write_text("MAP = {'t': [f()]}\n")
    with pytest.raises(hr.HeldoutError):
        hr.load_id_map(py)


def test_main_in_process_version(capsys):
    assert hr.main(["--version"]) == 0
    assert capsys.readouterr().out.startswith(f"heldout-run {hr.VERSION} sha256:")


@pytest.mark.parametrize("body", [
    "[]", '"x"', "null", '{"t": "FR-001"}', '{"t": [1]}', '{"t": {"a": 1}}', '{"t": null}',
    '{"map": []}', '{"map": "x"}', '{"map": null}', '{"map": ["FR-001"]}',
    '{"map": {"t": "FR-001"}}', '{"map": {"t": [1]}}', '{"map": {"t": null}}',
    '{"map": {"map": {"t": ["FR-001"]}}}', '{"map": {"t": ["FR-001"]}, "extra": 1}',
])
def test_malformed_json_maps_are_e_map(tmp_path, body):
    m = tmp_path / "m.json"
    m.write_text(body)
    with pytest.raises(hr.HeldoutError) as ei:
        hr.load_id_map(m)
    assert ei.value.code == "E_MAP"


@pytest.mark.parametrize("body", [
    "MAP = ['FR-001']\n", "MAP = {'t': 'FR-001'}\n", "MAP = {'t': [1]}\n", "MAP = {1: ['FR-001']}\n",
    "MAP = {'t': {'FR-001'}}\n", "X = 1\n", "MAP = {\n", "MAP: dict\n",
])
def test_malformed_py_maps_are_e_map(tmp_path, body):
    m = tmp_path / "fr_report.py"
    m.write_text(body)
    with pytest.raises(hr.HeldoutError):
        hr.load_id_map(m)


def test_unreadable_map_is_e_map(tmp_path):
    with pytest.raises(hr.HeldoutError):
        hr.load_id_map(tmp_path / "missing.json")
    d = tmp_path / "dir.json"
    d.mkdir()
    with pytest.raises(hr.HeldoutError):
        hr.load_id_map(d)
    b = tmp_path / "bin.json"
    b.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(hr.HeldoutError):
        hr.load_id_map(b)


def test_wrapped_json_map_is_accepted(tmp_path):
    m = tmp_path / "m.json"
    m.write_text('{"map": {"t": ["FR-001", "FR-001", "SC-002"]}}')
    assert hr.load_id_map(m) == {"t": ("FR-001", "SC-002")}


def test_malformed_wrapped_spec_map_refuses_run(h):
    h.suite({**UNMARKED, "SPEC-MAP.json": '{"map": ["FR-001"]}'})
    p = h.run_wt()
    assert (p.returncode, p.stderr, p.stdout) == (2, "heldout-run: error E_MAP\n", "")
