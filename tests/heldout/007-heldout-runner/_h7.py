"""Builders for the 007-heldout-runner held-out tests.

Everything here goes through the spec's Test Harness Contract only: the CLI
(`python3 scripts/heldout_run.py ...` or the `scripts/heldout-run` wrapper), the module
`heldout_run` loaded from its file path, the documented environment variables and the
documented file locations (private report, rounds.json).

Every test gets its own temporary base directory holding HOME, HELDOUT_ROOT, TMPDIR, the
canary target repository (a tiny git repo) and the canary hidden suite. The real HOME, the
real held-out root and the live checkout are never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

LIVE = Path("/home/muk/projects/moeka").resolve()
REAL_HELDOUT = Path("/home/muk/projects/.heldout").resolve()

ERROR_CODES = {
    "E_USAGE", "E_FEATURE", "E_SUITE_MISSING", "E_SUITE_IN_TREE", "E_SUITE_PERMS",
    "E_REPORT_IN_TREE", "E_RUNNER_IN_TREE", "E_MAP", "E_GIT", "E_SYNC", "E_BUSY",
    "E_ROUND_CAP",
}
ESCALATION = "round cap reached: escalate to owner"
# "/" followed by a path segment, not part of a count such as "2/6" or "-/4".
ABS_PATH_RE = re.compile(r"(?<![\w-])/[A-Za-z_.~]")
# Known base of the tree under test before the feature (consolidate/new-main c5340d24).
RUN_HELDOUT_SH_SHA256 = "f50aed0d0694f0cf59ab143c4a6ce87dd5a8a24f541fff26eee26d51445336e7"
AWORK_RUN_HELDOUT_SH_SHA256 = "fd398be4ad3e22820ecccc47fec3344e6da07f1b826c60e8b9b79199b8b335b5"


def _find_repo() -> Path | None:
    env = os.environ.get("HELDOUT_007_REPO")
    cands = [Path(env)] if env else [Path(p) for p in sys.path if p]
    for c in cands:
        try:
            c = c.resolve()
        except OSError:
            continue
        if (c / "scripts").is_dir() and (
            (c / "nanobot").is_dir() or (c / "scripts" / "heldout_run.py").exists()
        ):
            return c
    return None


REPO = _find_repo()
if REPO is None:
    raise RuntimeError(
        "cannot find the tree under test: set PYTHONPATH=<throwaway copy> or HELDOUT_007_REPO"
    )
if REPO == LIVE:
    raise RuntimeError("refusing to run held-out tests against the live checkout")
RUNNER = REPO / "scripts" / "heldout_run.py"
WRAPPER = REPO / "scripts" / "heldout-run"


def _tmp_base() -> str:
    cand = os.environ.get("HELDOUT_007_TMP") or "/tmp"
    p = Path(cand).resolve()
    for bad in (REPO, LIVE, REAL_HELDOUT):
        if p == bad or bad in p.parents:
            return "/tmp"
    return str(p)


TMP_BASE = _tmp_base()


def _user_base() -> str | None:
    """When pytest comes from the user site, keep it reachable under a temp HOME (read only)."""
    import site

    import pytest as _pytest
    try:
        usp = site.getusersitepackages()
    except Exception:
        return None
    if usp and os.path.realpath(_pytest.__file__).startswith(os.path.realpath(usp) + os.sep):
        return site.getuserbase()
    return None


_USER_BASE = _user_base()


def new_token() -> str:
    return "zq" + secrets.token_hex(6)


def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def mkd(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    q = p
    os.chmod(q, 0o700)
    return p


def write(p: Path, text: str, mode: int = 0o600) -> Path:
    mkd(p.parent)
    p.write_text(text)
    os.chmod(p, mode)
    return p


# Prepended to every inner canary test module: observation side channel through CANARY_OUT
# (an environment variable the runner passes through to pytest unchanged).
PRELUDE = '''\
import json as _json
import os as _os
import sys as _sys
import time as _time
from pathlib import Path as _Path

import pytest


def _obs(name, data):
    out = _os.environ.get("CANARY_OUT")
    if out:
        (_Path(out) / (name + ".json")).write_text(_json.dumps(data))


def _mark_run(tag="runs"):
    out = _os.environ.get("CANARY_OUT")
    if out:
        with open(_os.path.join(out, tag + ".log"), "a") as fh:
            fh.write("x\\n")


def _wait_release(name, limit=90.0):
    out = _os.environ.get("CANARY_OUT")
    end = _time.monotonic() + limit
    while out and not _os.path.exists(_os.path.join(out, name)) and _time.monotonic() < end:
        _time.sleep(0.05)

'''


def probe_test(levels: int = 3, fr: str = "FR-001", name: str = "test_probe") -> str:
    """An inner test that records where it runs and what it sees."""
    return f'''
@pytest.mark.fr("{fr}")
def {name}():
    _mark_run()
    here = _Path(__file__).resolve()
    copy = here.parents[{levels}]
    entries = {{}}
    for dp, dns, fns in _os.walk(copy):
        for n in dns + fns:
            p = _Path(dp) / n
            rel = str(p.relative_to(copy))
            if _os.path.islink(p):
                entries[rel] = "link:" + _os.readlink(p)
            elif p.is_dir():
                entries[rel] = "dir"
            else:
                entries[rel] = "file:" + p.read_bytes()[:200].decode("utf-8", "replace")
    import hashlib as _hl
    pp_runner = []
    for entry in (_os.environ.get("PYTHONPATH") or "").split(_os.pathsep):
        f = _Path(entry) / "heldout_run.py" if entry else None
        pp_runner.append(_hl.sha256(f.read_bytes()).hexdigest() if f and f.is_file() else None)
    keys = ("PYTHONPATH", "HELDOUT_RUN_RECORDS", "HELDOUT_ROUND", "PYTHONDONTWRITEBYTECODE",
            "CANARY_UNSET_ME", "CANARY_KEEP_ME", "HOME", "TMPDIR")
    _obs("probe", dict(
        file=str(here), suite_dir=str(here.parent), copy=str(copy), cwd=_os.getcwd(),
        cwd_list=sorted(_os.listdir(_os.getcwd())), argv=list(_sys.argv),
        executable=_sys.executable, pid=_os.getpid(), pgid=_os.getpgid(0),
        env={{k: _os.environ.get(k) for k in keys}}, entries=entries, pp_runner=pp_runner,
    ))
'''


TOYMOD = "def add(a, b):\n    return a + b\n"

# The US1 example: FR-001 2 tests all pass; FR-002 4 tests, 2 fail.
US1_SUITE = '''
import toymod


@pytest.mark.fr("FR-001")
def test_one_a():
    _mark_run()
    assert toymod.add(1, 2) == 3


@pytest.mark.fr("FR-001")
def test_one_b():
    assert toymod.add(2, 2) == 4


@pytest.mark.fr("FR-002")
def test_two_a():
    assert toymod.add(0, 0) == 0


@pytest.mark.fr("FR-002")
def test_two_b():
    assert toymod.add(-1, 1) == 0


@pytest.mark.fr("FR-002")
def test_two_c():
    assert toymod.add(1, 1) == 3, "two_c message"


@pytest.mark.fr("FR-002")
def test_two_d():
    assert toymod.add(5, 5) == 11, "two_d message"
'''

US1_FAIL_LINES = [
    "heldout-run: 001-demo round 1/4: failed",
    "FR-002: 2/4 failing",
    "total: 2/6 tests failing, 0 skipped; 1/2 ids failing",
]

ALL_PASS_SUITE = '''
import toymod


@pytest.mark.fr("FR-001")
def test_ok_a():
    _mark_run()
    assert toymod.add(1, 2) == 3


@pytest.mark.fr("FR-002")
def test_ok_b():
    assert toymod.add(2, 2) == 4
'''

ONE_FAIL_SUITE = '''
@pytest.mark.fr("FR-001")
def test_good():
    _mark_run()


@pytest.mark.fr("FR-002")
def test_bad():
    assert 1 == 2, "bad message"
'''


@dataclass
class Result:
    code: int
    out: str
    err: str
    elapsed: float

    def lines(self) -> list[str]:
        return self.out.splitlines()

    def json(self):
        return json.loads(self.out)


def proc_alive(pid: int) -> bool:
    try:
        st = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    state = st.rsplit(")", 1)[-1].split()[0]
    return state not in ("Z", "X")


def wait_until(pred, limit: float, step: float = 0.05) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def snapshot(root: Path, exclude_git: bool = True) -> dict[str, tuple]:
    """File set and bytes (and symlink targets, modes) of a directory tree."""
    snap: dict[str, tuple] = {}
    root = Path(root)
    if not root.exists():
        return snap
    for dp, dns, fns in os.walk(root):
        if exclude_git and ".git" in dns and Path(dp) == root:
            dns.remove(".git")
        for n in sorted(dns + fns):
            p = Path(dp) / n
            rel = str(p.relative_to(root))
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                snap[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                snap[rel] = ("dir", stat.S_IMODE(st.st_mode))
            else:
                snap[rel] = ("file", stat.S_IMODE(st.st_mode), hashlib.sha256(p.read_bytes()).hexdigest())
    return snap


class H:
    """Per-test harness: temp HOME / HELDOUT_ROOT / TMPDIR, canary repo and suite."""

    feature = "001-demo"
    repo_name = "demo"

    def __init__(self) -> None:
        self.tok = new_token()
        self.base = Path(tempfile.mkdtemp(prefix=f"h7{self.tok}-", dir=TMP_BASE)).resolve()
        os.chmod(self.base, 0o700)
        self.tokens: set[str] = {self.tok}
        self.home = mkd(self.base / "home")
        self.root = mkd(self.base / "root")
        self.tmp = mkd(self.base / "tmp")
        self.out = mkd(self.base / "out")
        self.cwd = mkd(self.base / "cwd")
        self.bin = mkd(self.base / "bin")
        self.tree = self.base / "tree"
        self.suite = self.root / self.repo_name / self.feature
        mkd(self.suite)
        self.env = self._env()
        self._popen: list[subprocess.Popen] = []
        write(self.home / ".gitconfig",
              "[user]\n\tname = canary\n\temail = canary@example.invalid\n"
              "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n")

    # -- environment --------------------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = {}
        for k, v in os.environ.items():
            if k.startswith(("PYTEST_", "HELDOUT_", "GIT_")) or k in (
                "PYTHONPATH", "PYTHONSTARTUP", "PYTHONDONTWRITEBYTECODE", "PYTHONHOME",
                "VIRTUAL_ENV", "CANARY_OUT", "COV_CORE_SOURCE",
            ):
                continue
            env[k] = v
        env.update(
            HOME=str(self.home),
            HELDOUT_ROOT=str(self.root),
            TMPDIR=str(self.tmp),
            CANARY_OUT=str(self.out),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            # third-party pytest plugins (benchmark, xdist, ...) must not perturb inner runs
            PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        )
        if _USER_BASE:
            env["PYTHONUSERBASE"] = _USER_BASE
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY",
                  "all_proxy"):
            env[k] = "http://127.0.0.1:9"
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
        return env

    def token(self) -> str:
        t = new_token()
        self.tokens.add(t)
        return t

    # -- canary target repository --------------------------------------------------------

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        r = subprocess.run(["git", *args], cwd=cwd or self.tree, env=self.env,
                           capture_output=True, text=True, timeout=60)
        if check and r.returncode != 0:
            raise AssertionError(f"harness git {args} failed: {r.stderr}")
        return r.stdout.strip()

    def make_tree(self, files: dict[str, str] | None = None, path: Path | None = None) -> Path:
        tree = path or self.tree
        tree.mkdir(mode=0o700, parents=True)
        write(tree / "toymod" / "__init__.py", TOYMOD, 0o644)
        write(tree / "README.txt", "toy repository\n", 0o644)
        for rel, text in (files or {}).items():
            write(tree / rel, text, 0o644)
        self.git("init", "-q", "-b", "main", cwd=tree)
        self.git("add", "-A", cwd=tree)
        self.git("commit", "-q", "-m", "init", cwd=tree)
        return tree

    # -- canary hidden suite --------------------------------------------------------------

    def write_suite(self, files: dict[str, str], dest: Path | None = None,
                    clean: bool = True, prelude: bool = True) -> Path:
        dest = dest or self.suite
        if clean and dest.exists():
            shutil.rmtree(dest)
        mkd(dest)
        for rel, text in files.items():
            body = textwrap.dedent(text)
            if prelude and rel.endswith(".py") and Path(rel).name.startswith("test_"):
                body = PRELUDE + body
            write(dest / rel, body)
        os.chmod(dest, 0o700)
        return dest

    # -- running the runner --------------------------------------------------------------

    def cmd(self, wrapper: bool = False, runner: Path | None = None) -> list[str]:
        if wrapper:
            return [str(WRAPPER)]
        return [sys.executable, str(runner or RUNNER)]

    def run(self, *args, env: dict | None = None, timeout: float = 240,
            wrapper: bool = False, runner: Path | None = None) -> Result:
        t0 = time.monotonic()
        p = subprocess.Popen(self.cmd(wrapper, runner) + [str(a) for a in args], cwd=self.cwd,
                             env=env or self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            out, err = p.communicate()
            raise AssertionError(f"runner did not finish within {timeout}s")
        return Result(p.returncode, out, err, time.monotonic() - t0)

    def spawn(self, *args, env: dict | None = None) -> subprocess.Popen:
        p = subprocess.Popen(self.cmd() + [str(a) for a in args], cwd=self.cwd,
                             env=env or self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
        self._popen.append(p)
        return p

    def std(self, *extra, suite: Path | None = None, tree: Path | None = None,
            feature: str | None = None, repo_name: str | None = None,
            profile: str = "generic") -> list[str]:
        return ["run", "--profile", profile, "--repo-name", repo_name or self.repo_name,
                "--feature", feature or self.feature, "--worktree", str(tree or self.tree),
                "--suite", str(suite or self.suite), *[str(e) for e in extra]]

    def run_std(self, *extra, **kw) -> Result:
        env = kw.pop("env", None)
        timeout = kw.pop("timeout", 240)
        return self.run(*self.std(*extra, **kw), env=env, timeout=timeout)

    # -- observations ---------------------------------------------------------------------

    def obs(self, name: str):
        p = self.out / f"{name}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def runs(self, tag: str = "runs") -> int:
        p = self.out / f"{tag}.log"
        return len(p.read_text().splitlines()) if p.exists() else 0

    @property
    def report_root(self) -> Path:
        return self.root / "_reports" / self.repo_name / self.feature

    def round_dir(self, n: int) -> Path:
        return self.report_root / f"round-{n:02d}"

    def rounds_json(self):
        p = self.report_root / "rounds.json"
        return json.loads(p.read_text()) if p.exists() else None

    def counted(self) -> int:
        data = self.rounds_json()
        return len(data["rounds"]) if data else 0

    def leftovers(self) -> list[str]:
        return sorted(os.listdir(self.tmp))

    # -- fakes ----------------------------------------------------------------------------

    def fake_uv(self, noise: str = "") -> Path:
        """A fake `uv` first on PATH: logs argv, `sync` obeys FAKE_UV_SYNC, `run` execs pytest."""
        script = f'''#!{sys.executable}
import json, os, sys, time
out = os.environ.get("CANARY_OUT", "")
if out:
    with open(os.path.join(out, "uv.log"), "a") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
args = sys.argv[1:]
if args[:1] == ["sync"]:
    print({noise!r} + " sync stdout " + os.getcwd())
    sys.stderr.write({noise!r} + " sync stderr\\n")
    mode = os.environ.get("FAKE_UV_SYNC", "ok")
    if mode == "fail":
        sys.exit(1)
    if mode == "hang":
        time.sleep(600)
    sys.exit(0)
if args[:1] == ["run"]:
    rest = args[1:]
    i = rest.index("pytest")
    os.execv(sys.executable, [sys.executable, "-m", "pytest", *rest[i + 1:]])
sys.exit(97)
'''
        write(self.bin / "uv", script, 0o700)
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}"
        return self.bin / "uv"

    def uv_log(self) -> list[dict]:
        p = self.out / "uv.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def python3_shim(self) -> None:
        write(self.bin / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', 0o700)
        self.env["PATH"] = f"{self.bin}{os.pathsep}{self.env.get('PATH', '')}"

    # -- redaction ------------------------------------------------------------------------

    def leaks(self, *texts: str) -> list[str]:
        found = []
        for t in texts:
            for tok in self.tokens:
                if tok in t:
                    found.append(f"canary token {tok[:4]}..")
            if str(self.base) in t or TMP_BASE + "/" in t:
                found.append("base path")
            m = ABS_PATH_RE.search(t)
            if m:
                found.append(f"absolute path at {m.start()}")
        return found

    def channel(self, res: Result, *files: Path) -> list[str]:
        texts = [res.out, res.err]
        for f in files:
            if Path(f).exists():
                texts.append(Path(f).read_text(errors="replace"))
        return self.leaks(*texts)

    # -- teardown -------------------------------------------------------------------------

    def reap(self) -> None:
        needle = str(self.base).encode()
        me = os.getpid()
        for d in Path("/proc").iterdir():
            if not d.name.isdigit() or int(d.name) == me:
                continue
            try:
                blob = (d / "environ").read_bytes() + (d / "cmdline").read_bytes()
            except OSError:
                continue
            if needle in blob:
                try:
                    os.kill(int(d.name), signal.SIGKILL)
                except OSError:
                    pass
        for p in self._popen:
            try:
                p.kill()
            except OSError:
                pass
            try:
                p.communicate(timeout=5)
            except Exception:
                pass

    def close(self) -> None:
        self.reap()
        for dp, dns, _fns in os.walk(self.base):
            for n in dns:
                try:
                    os.chmod(Path(dp) / n, 0o700)
                except OSError:
                    pass
        shutil.rmtree(self.base, ignore_errors=True)


def err_line(code: str) -> str:
    return f"heldout-run: error {code}"


def assert_refused(res: Result, code: str, exit_code: int = 2) -> None:
    assert res.code == exit_code, f"exit {res.code}, expected {exit_code}"
    assert res.err.rstrip("\n") == err_line(code), "stderr is not exactly the error code line"
    assert "\n" not in res.err.rstrip("\n")
