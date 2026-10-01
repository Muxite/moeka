"""Shared harness for the 005-multi-instance held-out tests.

Safety rules enforced here (spec Test Harness Contract):
- HOME is a fresh temporary directory for the whole session and again per test.
- The systemd user bus is unreachable (no XDG_RUNTIME_DIR, bogus DBUS address), and
  guard executables named systemctl/loginctl/sudo/nanobot/journalctl sit first on PATH,
  so an unrouted call can never reach the real systemd or start a real gateway.
- The tests refuse to run against the live checkout.
"""

from __future__ import annotations

import fcntl
import getpass
import importlib.util
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent

# -- session-wide isolation (before anything imports nanobot) ----------------------------

_SESSION_HOME = Path(tempfile.mkdtemp(prefix="mhs", dir="/tmp"))
_SCRUB = (
    "MOEKA_WORKSPACE", "MOEKA_CONFIG", "MOEKA_STATE", "NANOBOT_HOME", "MOEKA_REPO_ENV",
    "XDG_RUNTIME_DIR", "MOEKA_NANOBOT_BIN", "MOEKA_SYSTEMCTL", "MOEKA_LOGINCTL",
    "MOEKA_STOP_TIMEOUT_S", "TELEGRAM_TOKEN", "DISCORD_TOKEN", "MOEKA_TOKEN_ISSUE_SECRET",
    "MOEKA_OLLAMA_API_BASE", "MOEKA_GATEWAY_PORT", "MOEKA_WS_PORT",
)
os.environ["HOME"] = str(_SESSION_HOME)
for _v in _SCRUB:
    os.environ.pop(_v, None)
os.environ["MOEKA_RUN_DIR"] = str(_SESSION_HOME / "rundir")
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/heldout-bus"
# Offline: any accidental outbound HTTP(S) goes to a dead local proxy and fails fast.
DEAD_PROXY = "http://127.0.0.1:9"
LOCAL_NO_PROXY = "127.0.0.1,localhost,::1"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ[_k] = DEAD_PROXY
os.environ["NO_PROXY"] = os.environ["no_proxy"] = LOCAL_NO_PROXY

_spec = importlib.util.find_spec("nanobot")
if _spec is None or _spec.origin is None:
    raise RuntimeError("nanobot is not importable; set PYTHONPATH=<implementer tree copy>")
REPO = Path(os.environ.get("MOEKA_HELDOUT_REPO") or Path(_spec.origin).resolve().parents[1])
LIVE = Path("/home/muk/projects/moeka").resolve()
if REPO.resolve() == LIVE:
    raise RuntimeError("refusing to run held-out tests against the live checkout")

MOEKA_SH = REPO / "bin" / "moeka.sh"
INSTALL_SH = REPO / "scripts" / "install-service.sh"
THROWAWAY = not (REPO / ".git").exists()


# -- requirement-id reporting (ids and counts only) ----------------------------------------

_FR_OF: dict[str, tuple[str, ...]] = {}
_RESULTS: dict[str, str] = {}


def pytest_collection_modifyitems(config, items):
    for item in items:
        ids: list[str] = []
        for mark in item.iter_markers("fr"):
            ids.extend(mark.args)
        _FR_OF[item.nodeid] = tuple(dict.fromkeys(ids)) or ("UNMAPPED",)
        if item.get_closest_marker("docker") is not None and shutil.which("docker") is None:
            item.add_marker(pytest.mark.skip(reason="docker not available"))


def pytest_runtest_logreport(report):
    prev = _RESULTS.get(report.nodeid)
    if report.when == "call" or (report.when in ("setup", "teardown") and report.outcome != "passed"):
        outcome = report.outcome
        if report.when != "call" and report.outcome == "failed":
            outcome = "error"
        if prev in ("failed", "error"):
            return
        _RESULTS[report.nodeid] = outcome


def _fr_summary() -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for nodeid, outcome in _RESULTS.items():
        for fr in _FR_OF.get(nodeid, ("UNMAPPED",)):
            summary[fr][outcome] += 1
            summary[fr]["total"] += 1
    return {k: dict(v) for k, v in sorted(summary.items())}


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    summary = _fr_summary()
    tr = terminalreporter
    tr.section("held-out requirement summary (ids and counts only)")
    failing = 0
    for fr, counts in summary.items():
        bad = counts.get("failed", 0) + counts.get("error", 0)
        failing += bool(bad)
        tr.write_line(
            f"{fr:8s} total={counts.get('total', 0):3d} passed={counts.get('passed', 0):3d} "
            f"failed={counts.get('failed', 0):3d} error={counts.get('error', 0):3d} "
            f"skipped={counts.get('skipped', 0):3d}"
        )
    tr.write_line(f"requirement ids with >=1 failing test: {failing}")
    out = os.environ.get("HELDOUT_REPORT")
    if out:
        Path(out).write_text(json.dumps(summary, indent=2) + "\n")


# -- small utilities -----------------------------------------------------------------------


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pid_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


def cmdline(pid: int) -> list[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1]  # type: ignore[return-value]
    except OSError:
        return []


def wait_for(pred, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = pred()
        if value:
            return value
        if time.monotonic() > deadline:
            return value
        time.sleep(interval)


def flock_held(path: Path) -> bool:
    """True when another open file description holds an exclusive flock on *path*."""
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def tree(*roots: Path, skip_sidecars: bool = False) -> dict[str, tuple]:
    """Snapshot of every path under *roots*: dirs and (size, mtime_ns, sha-ish) of files."""
    snap: dict[str, tuple] = {}
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            snap[dirpath] = ("d",)
            for name in filenames:
                if skip_sidecars and name.endswith(("-wal", "-shm", "-journal")):
                    continue
                p = os.path.join(dirpath, name)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if stat.S_ISSOCK(st.st_mode):
                    snap[p] = ("s",)
                    continue
                snap[p] = (st.st_size, st.st_mtime_ns)
    return snap


def write_config(root: Path, *, gateway: int | None = None, api: int | None = None,
                 ws_socket: bool = True, ws_port: int | None = None,
                 workspace: str | None = None, extra: dict | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    ws: dict[str, Any] = {"enabled": True}
    if ws_socket:
        ws["unixSocketPath"] = str(root / "run" / "websocket.sock")
    else:
        ws["host"] = "127.0.0.1"
        ws["port"] = ws_port or free_port()
    cfg: dict[str, Any] = {
        "agents": {"defaults": {"workspace": workspace if workspace is not None else str(root)}},
        "gateway": {"host": "127.0.0.1", "port": gateway or free_port()},
        "api": {"host": "127.0.0.1", "port": api or free_port()},
        "channels": {"websocket": ws},
    }
    if extra:
        for k, v in extra.items():
            cfg.setdefault(k, {}).update(v) if isinstance(v, dict) else cfg.__setitem__(k, v)
    path = root / "config.json"
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    return path


# -- fake executables ------------------------------------------------------------------------

FAKE_NANOBOT = r'''#!/usr/bin/env python3
import json, os, signal, sys, time
rec = {"argv": sys.argv[1:], "pid": os.getpid(), "ppid": os.getppid(),
       "env": {k: v for k, v in os.environ.items() if k.startswith(("HT_", "MOEKA_"))}}
log = os.environ.get("FAKE_LOG_DIR")
if log:
    p = os.path.join(log, "nanobot-%d-%d.json" % (os.getpid(), time.monotonic_ns()))
    with open(p + ".tmp", "w") as fh:
        json.dump(rec, fh)
    os.replace(p + ".tmp", p)
if sys.argv[1:2] == ["gateway"]:
    if os.environ.get("FAKE_IGNORE_TERM") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    while True:
        time.sleep(0.2)
if "--version" in sys.argv:
    print("moeka 0.0.0-fake")
sys.exit(0)
'''

FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_SYSTEMCTL_DIR"]
with open(os.path.join(d, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
quiet = "--quiet" in sys.argv or "-q" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("-")]
def norm(u):
    return u[:-8] if u.endswith(".service") else u
def mark(kind, unit, on):
    p = os.path.join(d, kind, norm(unit))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if on:
        open(p, "w").close()
    elif os.path.exists(p):
        os.remove(p)
def has(kind, unit):
    return os.path.exists(os.path.join(d, kind, norm(unit)))
if not args:
    sys.exit(0)
verb, units = args[0], args[1:]
now = "--now" in sys.argv
if verb == "is-active":
    act = bool(units) and all(has("active", u) for u in units)
    if not quiet:
        print("active" if act else "inactive")
    sys.exit(0 if act else 3)
if verb == "is-enabled":
    en = bool(units) and all(has("enabled", u) for u in units)
    if not quiet:
        print("enabled" if en else "disabled")
    sys.exit(0 if en else 1)
if verb == "status":
    act = bool(units) and all(has("active", u) for u in units)
    print("fake status: " + ("active" if act else "inactive"))
    sys.exit(0 if act else 3)
if verb == "show":
    act = bool(units) and all(has("active", u) for u in units)
    print("ActiveState=" + ("active" if act else "inactive"))
    print("MainPID=0")
    sys.exit(0)
if verb in ("start", "restart"):
    for u in units: mark("active", u, True)
if verb == "stop":
    for u in units: mark("active", u, False)
if verb == "enable":
    for u in units:
        mark("enabled", u, True)
        if now: mark("active", u, True)
if verb == "disable":
    for u in units:
        mark("enabled", u, False)
        if now: mark("active", u, False)
sys.exit(0)
'''

FAKE_LOGINCTL = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_SYSTEMCTL_DIR"]
with open(os.path.join(d, "loginctl.jsonl"), "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:2] == ["show-user"]:
    print("Linger=no")
sys.exit(0)
'''

GUARD = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ.get("FAKE_SYSTEMCTL_DIR") or "/tmp"
with open(os.path.join(d, "unrouted.jsonl"), "a") as fh:
    fh.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + "\n")
if os.path.basename(sys.argv[0]) == "journalctl":
    print("FAKE-JOURNAL-OUTPUT")
    sys.exit(0)
sys.exit(1)
'''


def _write_exec(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


class Harness:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.root = home  # alias
        self.aux = Path(tempfile.mkdtemp(prefix="mha", dir="/tmp"))
        self.bin = self.aux / "fakebin"
        self.guard = self.aux / "guard"
        self.logs = self.aux / "gwlog"
        self.sysd = self.aux / "sysd"
        self.run_dir = self.aux / "rundir"
        self.out = self.aux / "out"
        for d in (self.bin, self.guard, self.logs, self.sysd, self.out):
            d.mkdir(parents=True)
        self.nanobot = _write_exec(self.bin / "nanobot", FAKE_NANOBOT)
        self.systemctl = _write_exec(self.bin / "fake-systemctl", FAKE_SYSTEMCTL)
        self.loginctl = _write_exec(self.bin / "fake-loginctl", FAKE_LOGINCTL)
        for name in ("systemctl", "loginctl", "sudo", "nanobot", "journalctl", "pkill",
                     "killall", "pgrep", "ps"):
            _write_exec(self.guard / name, GUARD)
        self._popens: list[subprocess.Popen] = []
        self._pids: set[int] = set()
        self._n = 0

    # environment ---------------------------------------------------------------------
    def env(self, **extra: str | None) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.guard}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "LANG": "C.UTF-8",
            "USER": getpass.getuser(),
            "LOGNAME": getpass.getuser(),
            "MOEKA_NANOBOT_BIN": str(self.nanobot),
            "MOEKA_SYSTEMCTL": str(self.systemctl),
            "MOEKA_LOGINCTL": str(self.loginctl),
            "MOEKA_RUN_DIR": str(self.run_dir),
            "MOEKA_STOP_TIMEOUT_S": "3",
            "FAKE_LOG_DIR": str(self.logs),
            "FAKE_SYSTEMCTL_DIR": str(self.sysd),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/heldout-bus",
            "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY,
            "http_proxy": DEAD_PROXY, "https_proxy": DEAD_PROXY,
            "NO_PROXY": LOCAL_NO_PROXY, "no_proxy": LOCAL_NO_PROXY,
            "PYTHONPATH": os.environ.get("PYTHONPATH", str(REPO)),
        }
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return env

    # running moeka.sh -----------------------------------------------------------------
    def _files(self):
        self._n += 1
        return self.out / f"o{self._n}", self.out / f"e{self._n}"

    def moeka(self, *args: str, env: dict[str, str] | None = None, timeout: float = 40,
              script: Path = MOEKA_SH, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return self.run(["bash", str(script), *args], env=env, timeout=timeout, cwd=cwd)

    def install(self, *args: str, env: dict[str, str] | None = None, timeout: float = 40):
        return self.run(["bash", str(INSTALL_SH), *args], env=env, timeout=timeout)

    def run(self, argv: list[str], *, env=None, timeout: float = 40, cwd: Path | None = None):
        out, err = self._files()
        with open(out, "wb") as fo, open(err, "wb") as fe:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe,
                env=env or self.env(), cwd=str(cwd or self.aux), start_new_session=True,
            )
            self._popens.append(proc)
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._killpg(proc)
                rc = -999
        return subprocess.CompletedProcess(
            argv, rc, out.read_text(errors="replace"), err.read_text(errors="replace"),
        )

    def popen(self, argv: list[str], *, env=None, cwd: Path | None = None) -> subprocess.Popen:
        out, err = self._files()
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=open(out, "wb"), stderr=open(err, "wb"),
            env=env or self.env(), cwd=str(cwd or self.aux), start_new_session=True,
        )
        proc.out_path, proc.err_path = out, err  # type: ignore[attr-defined]
        self._popens.append(proc)
        return proc

    def nanobot_cli(self, *args: str, env=None, timeout: float = 60, cwd: Path | None = None):
        e = env or self.env()
        return self.run([sys.executable, "-m", "nanobot", *args], env=e, timeout=timeout, cwd=cwd)

    def python(self, *args: str, env=None) -> subprocess.Popen:
        return self.popen([sys.executable, str(HERE / "workers.py"), *args], env=env)

    def worker(self, *args: str, timeout: float = 120, env=None) -> subprocess.CompletedProcess:
        return self.run([sys.executable, str(HERE / "workers.py"), *args], env=env, timeout=timeout)

    # fakes -----------------------------------------------------------------------------
    def systemctl_calls(self) -> list[list[str]]:
        p = self.sysd / "calls.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def clear_systemctl_calls(self) -> None:
        """Forget recorded systemctl calls (setup noise); later asserts see only new calls."""
        (self.sysd / "calls.jsonl").unlink(missing_ok=True)

    def loginctl_calls(self) -> list[list[str]]:
        p = self.sysd / "loginctl.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def unrouted(self) -> list[list[str]]:
        p = self.sysd / "unrouted.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def set_active(self, unit: str, on: bool = True) -> None:
        unit = unit[:-8] if unit.endswith(".service") else unit
        p = self.sysd / "active" / unit
        p.parent.mkdir(exist_ok=True)
        if on:
            p.touch()
        elif p.exists():
            p.unlink()

    def gateway_records(self, config: Path | None = None) -> list[dict]:
        recs = []
        for p in sorted(self.logs.glob("nanobot-*.json")):
            try:
                rec = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if rec.get("argv", [None])[0] != "gateway":
                continue
            if config is not None and str(config) not in rec["argv"]:
                continue
            recs.append(rec)
        for r in recs:
            self._pids.add(int(r["pid"]))
        return recs

    def wait_gateway(self, config: Path, timeout: float = 15) -> dict | None:
        def live():
            for r in self.gateway_records(config):
                if pid_alive(int(r["pid"])):
                    return r
            return None
        return wait_for(live, timeout)

    def track(self, pid: int) -> None:
        self._pids.add(pid)

    # teardown ------------------------------------------------------------------------------
    @staticmethod
    def _killpg(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def _kill_strays(self) -> None:
        """SIGKILL any process whose cmdline mentions this test's HOME or aux dir."""
        marks = (str(self.home).encode(), str(self.aux).encode())
        me = os.getpid()
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == me:
                continue
            try:
                raw = Path(f"/proc/{entry}/cmdline").read_bytes()
            except OSError:
                continue
            try:
                # setproctitle may hide the argv; the environment still names our dirs.
                envb = Path(f"/proc/{entry}/environ").read_bytes()
            except OSError:
                envb = b""
            env_hit = any(b"HOME=" + m + b"\0" in envb or b"=" + m + b"/" in envb
                          for m in marks)
            if any(m in raw for m in marks) or env_hit:
                try:
                    os.kill(int(entry), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def cleanup(self) -> None:
        self.gateway_records()
        for proc in self._popens:
            if proc.poll() is None:
                self._killpg(proc)
        for pid in self._pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self._kill_strays()
        for base in (self.home, self.aux):
            for dirpath, dirnames, _ in os.walk(base):
                for d in dirnames:
                    try:
                        os.chmod(os.path.join(dirpath, d), 0o700)
                    except OSError:
                        pass
            shutil.rmtree(base, ignore_errors=True)


def _reset_config_path() -> None:
    try:
        from nanobot.config import loader
    except Exception:
        return
    reset = getattr(loader, "reset_config_path", None)
    if callable(reset):
        try:
            reset()
            return
        except Exception:
            pass
    try:
        loader._current_config_path = None  # type: ignore[attr-defined]
    except Exception:
        pass


@pytest.fixture(autouse=True)
def h(monkeypatch) -> Harness:
    home = Path(tempfile.mkdtemp(prefix="mh", dir="/tmp"))
    harness = Harness(home)
    monkeypatch.setenv("HOME", str(home))
    for v in _SCRUB:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("MOEKA_RUN_DIR", str(harness.run_dir))
    monkeypatch.setenv("FAKE_SYSTEMCTL_DIR", str(harness.sysd))
    _reset_config_path()
    try:
        yield harness
    finally:
        _reset_config_path()
        harness.cleanup()


@pytest.fixture
def repo_env_files():
    """Write repo-level .env/keys.env into the (throwaway) implementer tree copy."""
    if not THROWAWAY:
        pytest.skip("repo-level env files can only be written in a throwaway copy (no .git)")
    created: list[Path] = []

    def write(name: str, text: str) -> Path:
        p = REPO / name
        if p.exists() and p not in created:
            pytest.skip(f"{p} already exists in the copy; refusing to overwrite")
        p.write_text(text)
        created.append(p)
        return p

    try:
        yield write
    finally:
        for p in created:
            p.unlink(missing_ok=True)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SESSION_HOME, ignore_errors=True)
