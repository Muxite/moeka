"""Builders for the 008-heldout-sandbox held-out tests.

Only the spec's Test Harness Contract (007 contract plus the 008 additions) is used: the CLI
(`python3 scripts/heldout_run.py run ... --sandbox docker`), the module `heldout_run` loaded from
its file path (constants), the documented env vars and file locations (private report,
rounds.json), and Docker itself (image tag scheme `heldout-sandbox:<sha12>`).

Every test gets its own temp base holding HOME, HELDOUT_ROOT, TMPDIR, a toy target repo and a toy
hidden suite. Toy suites run INSIDE the sandbox, so they cannot write to host paths; the outer
tests observe them through the private report (`report.json` test outcomes). The real HOME, the
real held-out root, `~/.nanobot` and the live checkout are never touched.
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

JUDGE_UID = 10101
IMPL_UID = 10102
IMPL_LAUNCHER = "/usr/local/bin/heldout-impl"
IMAGE_REPO = "heldout-sandbox"
ABS_PATH_RE = re.compile(r"(?<![\w-])/[A-Za-z_.~]")
# Files that must stay byte-identical (FR-020), at the spec-008 base commit a79edd11.
WRAPPER_SHA256 = "79725ebdfbc83a3c22bdac2aa3c67a9da7c9862de9ac8a6c9d8893537ac1372a"
RUN_HELDOUT_SH_SHA256 = "f50aed0d0694f0cf59ab143c4a6ce87dd5a8a24f541fff26eee26d51445336e7"
# sha256 of `sha256sum *` over tests/heldout/007-heldout-runner/ (the accepted 007 suite).
SUITE007_LISTING_SHA256 = "2c0c5703381de734c55725e21044262a498257a1339d438f4bf533f6c834a531"


def _find_repo() -> Path | None:
    env = os.environ.get("HELDOUT_008_REPO")
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
        "cannot find the tree under test: set PYTHONPATH=<throwaway copy> or HELDOUT_008_REPO"
    )
if REPO == LIVE:
    raise RuntimeError("refusing to run held-out tests against the live checkout")
RUNNER = REPO / "scripts" / "heldout_run.py"
WRAPPER = REPO / "scripts" / "heldout-run"
SUITE_NAME = "008-heldout-sandbox"


def _tmp_base() -> str:
    cand = os.environ.get("HELDOUT_008_TMP") or "/tmp"
    p = Path(cand).resolve()
    for bad in (REPO, LIVE, REAL_HELDOUT):
        if p == bad or bad in p.parents:
            return "/tmp"
    return str(p)


TMP_BASE = _tmp_base()


def _user_base() -> str | None:
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


def sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def mkd(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(p, 0o700)
    return p


def write(p: Path, text: str, mode: int = 0o600) -> Path:
    mkd(p.parent)
    p.write_text(text)
    os.chmod(p, mode)
    return p


def snapshot(root: Path) -> dict[str, tuple]:
    snap: dict[str, tuple] = {}
    root = Path(root)
    if not root.exists():
        return snap
    for dp, dns, fns in os.walk(root):
        for n in sorted(dns + fns):
            p = Path(dp) / n
            rel = str(p.relative_to(root))
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                snap[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                snap[rel] = ("dir", stat.S_IMODE(st.st_mode))
            else:
                snap[rel] = ("file", stat.S_IMODE(st.st_mode),
                             hashlib.sha256(p.read_bytes()).hexdigest())
    return snap


# -- docker ------------------------------------------------------------------------------------

def docker(*args: str, timeout: float = 600, check: bool = False, env=None):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                       env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"harness docker {args[:2]} failed: {r.stderr[-400:]}")
    return r


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return docker("info", "--format", "{{.ServerVersion}}", timeout=30).returncode == 0
    except Exception:
        return False


def image_tag_for(dockerfile_text: str) -> str:
    return f"{IMAGE_REPO}:" + hashlib.sha256(dockerfile_text.encode()).hexdigest()[:12]


def image_id(ref: str) -> str | None:
    r = docker("image", "inspect", "--format", "{{.Id}}", ref, timeout=60)
    return r.stdout.strip() if r.returncode == 0 else None


def sandbox_containers() -> dict[str, str]:
    """All containers (any state) whose image is a heldout-sandbox image or an override test
    image; {id: status}."""
    r = docker("ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Image}}\t{{.Status}}",
               timeout=60)
    out = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and (parts[1].startswith(IMAGE_REPO) or parts[1].startswith("h8")):
            out[parts[0]] = parts[2]
    return out


def in_image(image: str, script: str, user: str = "0", timeout: float = 120):
    """Run a shell snippet in the image (network none), bypassing any entrypoint."""
    return docker("run", "--rm", "--network", "none", "--user", user, "--entrypoint", "/bin/sh",
                  image, "-c", script, timeout=timeout)


# -- toy material --------------------------------------------------------------------------------

TOYMOD = "def add(a, b):\n    return a + b\n"

ONE_FAIL_SUITE = '''
import pytest


@pytest.mark.fr("FR-001")
def test_good():
    assert 1 + 1 == 2


@pytest.mark.fr("FR-002")
def test_bad():
    assert 1 == 2, "bad message"
'''

ALL_PASS_SUITE = '''
import pytest


@pytest.mark.fr("FR-001")
def test_ok_a():
    assert True


@pytest.mark.fr("FR-002")
def test_ok_b():
    assert 2 * 2 == 4
'''

US1_SUITE = '''
import pytest
import toymod


@pytest.mark.fr("FR-001")
def test_one_a():
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

COLLECTION_ERROR_SUITE = '''
import pytest
import toymod_does_not_exist_anywhere


@pytest.mark.fr("FR-001")
def test_never():
    assert True
'''

SLEEP_SUITE = '''
import pytest
import signal
import time


@pytest.mark.fr("FR-001")
def test_fast():
    assert True


@pytest.mark.fr("FR-002")
def test_sleeps():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(900)
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


class H:
    """Per-test harness: temp HOME / HELDOUT_ROOT / TMPDIR, toy repo and toy suite."""

    feature = "001-demo"
    repo_name = "demo"

    def __init__(self) -> None:
        self.tok = new_token()
        self.base = Path(tempfile.mkdtemp(prefix=f"h8{self.tok}-", dir=TMP_BASE)).resolve()
        os.chmod(self.base, 0o700)
        self.tokens: set[str] = {self.tok}
        self.home = mkd(self.base / "home")
        self.root = mkd(self.base / "root")
        self.tmp = mkd(self.base / "tmp")
        self.cwd = mkd(self.base / "cwd")
        self.bin = mkd(self.base / "bin")
        self.reports = mkd(self.base / "reports")
        self.tree = self.base / "tree"
        self.suite = self.root / self.repo_name / self.feature
        mkd(self.suite)
        self.env = self._env()
        self._n = 0
        self._images: list[str] = []
        self._containers: list[str] = []
        self.t_start = time.time()
        write(self.home / ".gitconfig",
              "[user]\n\tname = canary\n\temail = canary@example.invalid\n"
              "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n")

    def _env(self) -> dict[str, str]:
        env = {}
        for k, v in os.environ.items():
            if k.startswith(("PYTEST_", "HELDOUT_", "GIT_", "DOCKER_")) or k in (
                "PYTHONPATH", "PYTHONSTARTUP", "PYTHONDONTWRITEBYTECODE", "PYTHONHOME",
                "VIRTUAL_ENV", "CANARY_OUT", "COV_CORE_SOURCE", "UV_PROJECT_ENVIRONMENT",
            ):
                continue
            env[k] = v
        env.update(
            HOME=str(self.home),
            HELDOUT_ROOT=str(self.root),
            TMPDIR=str(self.tmp),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        )
        if _USER_BASE:
            env["PYTHONUSERBASE"] = _USER_BASE
        return env

    def token(self) -> str:
        t = new_token()
        self.tokens.add(t)
        return t

    # -- toy target repository ---------------------------------------------------------------

    def git(self, *args: str, cwd: Path | None = None) -> str:
        r = subprocess.run(["git", *args], cwd=cwd or self.tree, env=self.env,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError(f"harness git {args} failed: {r.stderr}")
        return r.stdout.strip()

    def make_tree(self, files: dict[str, str] | None = None, path: Path | None = None,
                  toymod: str = TOYMOD) -> Path:
        tree = path or self.tree
        tree.mkdir(mode=0o755, parents=True)
        write(tree / "toymod" / "__init__.py", toymod, 0o644)
        write(tree / "README.txt", "toy repository\n", 0o644)
        for rel, text in (files or {}).items():
            write(tree / rel, textwrap.dedent(text), 0o755 if rel.endswith(".sh") else 0o644)
        for dp, dns, _ in os.walk(tree):
            for n in dns:
                os.chmod(Path(dp) / n, 0o755)
        os.chmod(tree, 0o755)
        self.git("init", "-q", "-b", "main", cwd=tree)
        self.git("add", "-A", cwd=tree)
        self.git("commit", "-q", "-m", "init", cwd=tree)
        return tree

    # -- toy hidden suite ------------------------------------------------------------------------

    def write_suite(self, files: dict[str, str], dest: Path | None = None) -> Path:
        dest = dest or self.suite
        if dest.exists():
            shutil.rmtree(dest)
        mkd(dest)
        for rel, text in files.items():
            write(dest / rel, textwrap.dedent(text))
        os.chmod(dest, 0o700)
        return dest

    # -- running ---------------------------------------------------------------------------------

    def run(self, *args, env: dict | None = None, timeout: float = 900,
            runner: Path | None = None, wrapper: bool = False) -> Result:
        cmd = [str(WRAPPER)] if wrapper else [sys.executable, str(runner or RUNNER)]
        t0 = time.monotonic()
        p = subprocess.Popen(cmd + [str(a) for a in args], cwd=self.cwd, env=env or self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.communicate()
            raise AssertionError(f"runner did not finish within {timeout}s")
        return Result(p.returncode, out, err, time.monotonic() - t0)

    def new_report_dir(self) -> Path:
        self._n += 1
        return self.reports / f"r{self._n:02d}"

    def std(self, *extra, sandbox: bool = True, counted: bool = False, report: bool = True,
            suite: Path | None = None, tree: Path | None = None, profile: str = "generic"):
        args = ["run", "--profile", profile, "--repo-name", self.repo_name,
                "--feature", self.feature, "--worktree", str(tree or self.tree),
                "--suite", str(suite or self.suite)]
        if sandbox:
            args += ["--sandbox", "docker"]
        if not counted:
            args.append("--no-count")
        rd = None
        if report:
            rd = self.new_report_dir()
            args += ["--report-dir", str(rd)]
        self.last_report = rd
        return args + [str(e) for e in extra]

    def run_sb(self, *extra, env=None, timeout: float = 900, runner=None, **kw) -> Result:
        res = self.run(*self.std(*extra, **kw), env=env, timeout=timeout, runner=runner)
        res.report_dir = self.last_report  # type: ignore[attr-defined]
        return res

    # -- private report -------------------------------------------------------------------------

    @property
    def report_root(self) -> Path:
        return self.root / "_reports" / self.repo_name / self.feature

    def rounds_json(self):
        p = self.report_root / "rounds.json"
        return json.loads(p.read_text()) if p.exists() else None

    def counted(self) -> int:
        d = self.rounds_json()
        return len(d["rounds"]) if d else 0

    def leftovers(self) -> list[str]:
        return sorted(os.listdir(self.tmp))

    # -- redaction -------------------------------------------------------------------------------

    def leaks(self, *texts: str) -> list[str]:
        found = []
        for t in texts:
            for tok in self.tokens:
                if tok in t:
                    found.append(f"token {tok[:4]}..")
            if str(self.base) in t:
                found.append("base path")
            m = ABS_PATH_RE.search(t)
            if m:
                found.append(f"absolute path at {m.start()}")
        return found

    # -- docker bookkeeping ------------------------------------------------------------------------

    def track_image(self, ref: str) -> None:
        self._images.append(ref)

    def track_container(self, name: str) -> None:
        self._containers.append(name)

    def close(self) -> None:
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
        for c in self._containers:
            try:
                docker("rm", "-f", "-v", c, timeout=60)
            except Exception:
                pass
        for i in self._images:
            try:
                docker("rmi", "-f", i, timeout=120)
            except Exception:
                pass
        for dp, dns, _fns in os.walk(self.base):
            for n in dns:
                try:
                    os.chmod(Path(dp) / n, 0o700)
                except OSError:
                    pass
        shutil.rmtree(self.base, ignore_errors=True)
        if self.base.exists():
            # files left with a sandbox uid (a runner that failed to clean up): remove as root
            # inside a throwaway container that mounts only this test's temp base.
            img = os.environ.get("_H8_CLEAN_IMAGE") or "alpine:latest"
            try:
                docker("run", "--rm", "--network", "none", "--user", "0", "--entrypoint", "/bin/sh",
                       "-v", f"{self.base}:/x", img, "-c", "rm -rf /x/* /x/.[!.]* 2>/dev/null; true",
                       timeout=120)
            except Exception:
                pass
            shutil.rmtree(self.base, ignore_errors=True)


# -- report parsing ------------------------------------------------------------------------------

def load_report(report_dir) -> dict:
    p = Path(report_dir) / "report.json"
    assert p.is_file(), "report.json missing from the private report dir"
    return json.loads(p.read_text())


def canary_entries(rep: dict) -> list[tuple[str, str]]:
    """Normalise `canaries` to [(name, outcome)]. Accepts a list of objects, an object of
    objects, or an object name -> outcome."""
    c = rep.get("canaries")
    out: list[tuple[str, str]] = []

    def one(name, v):
        if isinstance(v, dict):
            n = v.get("name") or v.get("nodeid") or v.get("test") or name or ""
            o = v.get("outcome") or v.get("recorded") or v.get("result") or ""
            out.append((str(n), str(o)))
        else:
            out.append((str(name), str(v)))

    if isinstance(c, list):
        for v in c:
            one(None, v)
    elif isinstance(c, dict):
        vals = list(c.values())
        if vals and all(isinstance(v, dict) for v in vals):
            for k, v in c.items():
                one(k, v)
        else:
            for k, v in c.items():
                if isinstance(v, (str, type(None))):
                    one(k, v)
    return out


def _canary_names(rep: dict) -> set[str]:
    names = set()
    for n, _ in canary_entries(rep):
        names.add(n)
        names.add(n.rsplit("::", 1)[-1])
    return names


def real_tests(rep: dict) -> list[dict]:
    """report.json tests without the runner's canaries."""
    cn = _canary_names(rep)
    out = []
    for t in rep.get("tests") or []:
        ids = [str(i) for i in t.get("ids") or []]
        if "CANARY" in ids:
            continue
        if t.get("nodeid") in cn or t.get("key") in cn or \
                str(t.get("nodeid", "")).rsplit("::", 1)[-1] in cn:
            continue
        out.append(t)
    return out


def outcomes(rep: dict) -> dict[str, str]:
    """{test key (function name): outcome} for the toy tests."""
    res = {}
    for t in real_tests(rep):
        key = t.get("key") or str(t.get("nodeid", "")).rsplit("::", 1)[-1].split("[", 1)[0]
        res[key] = t.get("outcome")
    return res


def longrepr_of(rep: dict, key: str) -> str:
    for t in real_tests(rep):
        if t.get("key") == key:
            return str(t.get("longrepr") or "")[-1500:]
    return ""


def per_id_counts(rep: dict) -> dict[str, tuple[int, int]]:
    agg: dict[str, list[int]] = {}
    for t in real_tests(rep):
        if t.get("outcome") == "skipped":
            continue
        for i in t.get("ids") or ["UNMAPPED"]:
            a = agg.setdefault(i, [0, 0])
            a[1] += 1
            if t.get("outcome") == "failed":
                a[0] += 1
    return {k: (v[0], v[1]) for k, v in agg.items()}


def status_of(res: Result) -> str:
    first = (res.lines() or [""])[0]
    m = re.match(r"^heldout-run: \S+ round \S+: (\w+)$", first)
    return m.group(1) if m else f"<unparsed:{first[:60]}>"


def err_line(code: str) -> str:
    return f"heldout-run: error {code}"


def assert_refused(res: Result, code: str, exit_code: int = 2) -> None:
    assert res.code == exit_code, f"exit {res.code}, expected {exit_code}"
    assert res.err.rstrip("\n") == err_line(code), "stderr is not exactly the error code line"


def assert_toys_passed(rep: dict, keys) -> None:
    oc = outcomes(rep)
    bad = {k: oc.get(k) for k in keys if oc.get(k) != "passed"}
    detail = "; ".join(f"{k}: {longrepr_of(rep, k)[-300:]}" for k in bad)
    assert not bad, f"in-sandbox checks did not pass: {bad} :: {detail}"


def search_tokens(roots, tokens, exclude=(), since: float | None = None,
                  max_bytes: int = 4 << 20) -> list[str]:
    """Paths under roots (not under any exclude) whose name or bytes contain a token."""
    hits = []
    btoks = [t.encode() for t in tokens]
    excl = [str(Path(e).resolve()) for e in exclude]
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for dp, dns, fns in os.walk(root, onerror=lambda e: None):
            if any(dp == e or dp.startswith(e + os.sep) for e in excl):
                dns[:] = []
                continue
            for n in fns + dns:
                p = os.path.join(dp, n)
                if any(t in n for t in tokens):
                    hits.append(p)
                    continue
                if n in dns:
                    continue
                try:
                    st = os.lstat(p)
                    if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
                        continue
                    if since is not None and st.st_mtime < since and st.st_ctime < since:
                        continue
                    with open(p, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                if any(t in data for t in btoks):
                    hits.append(p)
    return hits
