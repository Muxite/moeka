#!/usr/bin/env python3
"""heldout-run: run a hidden (held-out) pytest suite against a throwaway copy of a tree.

The head agent runs this from its own checkout. It copies the tree under test (a git ref or a
working tree) into a scratch directory, places the suite inside the copy, runs pytest under a
wall-clock timeout in its own process group, maps every test to requirement ids (``fr`` markers,
``SPEC-MAP.json`` or the ``MAP`` literal of an ``fr_report.py``) and prints only redacted feedback:
ids with failing counts and totals. Full details go to a private report under the held-out root.

The same file is the pytest plugin loaded in the child with ``-p heldout_run``; it records
per-test results as JSON lines in ``$HELDOUT_RUN_RECORDS``.

Subcommands: ``run``, ``triage``, ``rounds``, ``check-isolation``; ``--version``.

STATED LIMITS.

Without ``--sandbox`` (``--sandbox none``, the default) this runner is not a sandbox and claims
no adversarial isolation. The suite runs in the same process as the implementer's code, which
can read the suite copy, write outside the copy, or tamper with the recorded results, and the
feedback counts are themselves a low-bandwidth channel. The runner prevents accidental exposure
(suite placement, redaction, cleanup), not a hostile implementer.

With ``--sandbox docker`` (spec 008) the sync step and pytest run in Docker containers built from
the embedded ``SANDBOX_DOCKERFILE``. Sandboxed runs isolate the network, the host and the suite
from the tree's processes and from anything that outlives the run: the sync step sees only the
copy (uid ``impl``, no suite, no host path); pytest runs as uid ``judge`` with no network, a
read-only root, and the suite, ``/out``, the records and the plugin readable by ``judge`` only;
every container, volume and scratch directory is removed when the run ends. Code imported into
the judge process (a suite that imports implementation modules, a ``sitecustomize.py`` or a
shadowing module on ``PYTHONPATH``, the tree's own ``.venv`` under the uv profiles) can still read
the suite and forge results within one run; two random canary tests, a collect-only count and a
junit/records cross-check detect that only partly (status ``tampered``; ``meta.isolation`` records
whether tree code was imported). The per-id counts remain a low-bandwidth channel. Docker itself
is the boundary: no claim is made against kernel or Docker escapes. The sync container uses the
default bridge network, so it can also reach services the host exposes on that bridge.

Judge/impl separation (FR-011, FR-012). The test container's PID 1 is ``heldout-init``, a small
root process holding only CAP_SETUID, CAP_SETGID, CAP_SETPCAP and CAP_KILL (everything else is
dropped, including DAC override). It runs the collect pass and the main pass as uid ``judge`` with
empty bounding, ambient and permitted sets, so pytest's process tree holds no capability. It also
serves ``/usr/local/bin/heldout-impl``: a judge process (checked with SO_PEERCRED; impl callers
are refused) sends argv, environment, working directory and its stdin/stdout/stderr descriptors
over a unix socket, and the broker forks, drops to uid/gid ``impl`` (no supplementary groups,
empty capability sets) and execs; the client relays the exit status or signal, and killing the
client kills the command. When impl cannot enter the caller's working directory (pytest's
``tmp_path`` is judge-only, mode 0700), the command starts in ``/``; a suite that wants the
implementation to use a temp directory must make it accessible (for example ``chmod 0o777``). Why a broker: the required ``no-new-privileges`` flag disables setuid
bits and file capabilities, so a setuid helper or a capability-carrying ``setpriv`` cannot work,
and keeping one privileged process outside pytest's tree is the simplest mechanism that leaves
pytest, its children and the impl processes with nothing to escalate with.

Standard library only; it never imports moeka, nanobot or awr.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import fcntl
import fnmatch
import hashlib
import io
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import warnings
import xml.etree.ElementTree as ElementTree

VERSION = "1.0.0"

FEEDBACK_SCHEMA = "heldout-feedback.v1"
REPORT_SCHEMA = "heldout-report.v1"
ROUNDS_SCHEMA = "heldout-rounds.v1"
TRIAGE_SCHEMA = "heldout-triage.v1"

_ID_RX = re.compile(r"^(FR|NFR|SC)-[0-9]{3}[a-z]?$")


class _IdPattern(str):
    """The requirement-id regex source; also usable like a compiled pattern."""

    def match(self, string, *args):  # noqa: D102
        return _ID_RX.match(string, *args)

    def fullmatch(self, string, *args):  # noqa: D102
        return _ID_RX.fullmatch(string, *args)

    def search(self, string, *args):  # noqa: D102
        return _ID_RX.search(string, *args)

    @property
    def pattern(self) -> str:  # noqa: D102
        return str(self)


ID_PATTERN = _IdPattern(_ID_RX.pattern)
FEATURE_RX = re.compile(r"^[0-9]{3}-[a-z0-9][a-z0-9-]*$")
REPO_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

INVALID_ID = "INVALID_ID"
UNMAPPED = "UNMAPPED"

ERROR_CODES = frozenset(
    {
        "E_USAGE",
        "E_FEATURE",
        "E_SUITE_MISSING",
        "E_SUITE_IN_TREE",
        "E_SUITE_PERMS",
        "E_REPORT_IN_TREE",
        "E_RUNNER_IN_TREE",
        "E_MAP",
        "E_GIT",
        "E_SYNC",
        "E_BUSY",
        "E_ROUND_CAP",
        "E_SANDBOX_UNAVAILABLE",
        "E_SANDBOX_DOCKER",
    }
)
_EXIT_OF_CODE = {"E_SYNC": 3, "E_BUSY": 3, "E_ROUND_CAP": 4, "E_SANDBOX_UNAVAILABLE": 3}

STATUSES = ("passed", "failed", "timeout", "collection_error", "infra_error", "tampered")
COUNTED_STATUSES = frozenset({"passed", "failed", "timeout", "collection_error", "tampered"})
_EXIT_OF_STATUS = {"passed": 0, "failed": 1, "timeout": 3, "collection_error": 3, "infra_error": 3,
                   "tampered": 1}

DEFAULT_CAP = 4
DEFAULT_SYNC_TIMEOUT = 900.0
KILL_GRACE_S = 10.0
RECORDS_ENV = "HELDOUT_RUN_RECORDS"
ROUND_ENV = "HELDOUT_ROUND"

EXCLUDED_NAMES = frozenset(
    {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
)

# Profile defaults (FR-011). "{copy}" is replaced by the copy's path; "@scratch" means a new
# empty scratch directory.
PROFILES = {
    "moeka": {
        "sync": ["uv", "sync", "--project", "{copy}", "--extra", "dev", "-q"],
        "launcher": ["uv", "run", "--project", "{copy}", "--extra", "dev", "pytest"],
        "pythonpath": ["{copy}"],
        "cwd": "@scratch",
        "timeout": 5400.0,
    },
    "awork-resume": {
        "sync": None,
        "launcher": ["uv", "run", "--project", "{copy}", "--extra", "dev", "pytest"],
        "pythonpath": ["{copy}/src", "{copy}"],
        "cwd": "{copy}",
        "timeout": 1800.0,
    },
    "generic": {
        "sync": None,
        "launcher": ["@python", "-m", "pytest"],
        "pythonpath": ["{copy}"],
        "cwd": "@scratch",
        "timeout": 600.0,
    },
}

LIMIT_TEXT = (
    "Stated limits. Without --sandbox, heldout-run is not a sandbox and claims no adversarial\n"
    "isolation: the suite runs in the same process as the implementer's code, which can read the\n"
    "suite copy, write outside the copy or tamper with results; the feedback counts are a\n"
    "low-bandwidth channel. It prevents accidental exposure (placement, redaction, cleanup), not\n"
    "a hostile implementer.\n"
    "With --sandbox docker, sandboxed runs isolate the network, the host and the suite from the\n"
    "tree's processes and from anything that outlives the run (sync as uid impl without the\n"
    "suite; pytest as uid judge, no network, read-only root; heldout-impl runs implementation\n"
    "processes as uid impl). Code imported into the judge process can still forge results within\n"
    "one run, which the canaries and count checks detect only partly (status tampered); the\n"
    "counts are a low-bandwidth channel; Docker itself is the boundary (no claim against kernel\n"
    "or Docker escapes)."
)


# ---------------------------------------------------------------------------------------------
# sandbox (spec 008): constants, the embedded image and its two in-image programs

SANDBOX_MODES = ("none", "docker")
SANDBOX_JUDGE_UID = 10101
SANDBOX_IMPL_UID = 10102
SANDBOX_IMPL_PATH = "/usr/local/bin/heldout-impl"
IMPL_ENV = "HELDOUT_IMPL"
SANDBOX_DEFAULT_MEMORY = "4g"
SANDBOX_DEFAULT_CPUS = 2.0
SANDBOX_PIDS_LIMIT = 4096
SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CANARY_LABEL = "CANARY"
_SANDBOX_LABEL = "heldout-run.id"
_SANDBOX_CWD = "/tmp/heldout-cwd"
_SANDBOX_HOME = "/tmp/heldout-home"
_SANDBOX_PLUGIN = "/out/plugin"
# Files copied from /out to the private report directory (FR-009), each capped.
_OUT_FILES = ("junit.xml", "pytest.log", "records.jsonl", "collect.jsonl", "collect.log",
              "collect-junit.xml")
_OUT_CAP = 256 << 20
# In-container launchers (FR-007: syncing disabled) and sync steps (FR-004). awork-resume has
# no sync step of its own (its ``uv run`` syncs), so sandboxed it syncs like moeka.
_UV_SYNC = ["uv", "sync", "--project", "{copy}", "--extra", "dev", "-q"]
_UV_RUN = ["uv", "run", "--project", "/work", "--extra", "dev", "--no-sync", "pytest"]
SANDBOX_PROFILES = {
    "moeka": {"sync": _UV_SYNC, "launcher": _UV_RUN},
    "awork-resume": {"sync": _UV_SYNC, "launcher": _UV_RUN},
    "generic": {"sync": None, "launcher": ["python3", "-m", "pytest"]},
}
# Host-bound environment names never passed into a container (paths, sockets, sessions).
_HOST_ONLY_ENV = frozenset({
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "PWD", "OLDPWD", "PYTHONPATH", "PYTHONHOME",
    "PYTHONUSERBASE", "PYTHONSTARTUP", "PYTHONEXECUTABLE", "PYTHONPYCACHEPREFIX", "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "SHELL", "USER", "LOGNAME", "MAIL", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "XDG_DATA_HOME", "XDG_STATE_HOME", "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "DISPLAY",
    "WAYLAND_DISPLAY", "UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR", "UV_PYTHON",
    "UV_PYTHON_INSTALL_DIR", "UV_TOOL_DIR", "HELDOUT_ROOT", IMPL_ENV, RECORDS_ENV, ROUND_ENV,
})

_SANDBOX_INIT = r'''#!/usr/local/bin/python3 -I
"""heldout-init: PID 1 of a heldout-run test container (the only process holding capabilities).

It runs as root with only CAP_SETUID, CAP_SETGID, CAP_SETPCAP and CAP_KILL (no DAC override).
It reads one JSON config from stdin, runs each step (the collect pass, then the main pass) as the
judge user with an empty bounding set, and meanwhile serves /run/heldout/broker.sock: a judge
process (checked with SO_PEERCRED) passes its stdin/stdout/stderr, argv, environment and working
directory, and the broker starts that command as the impl user, never as anyone else. Step exit
codes go to stdout as JSON lines; nothing a step or an impl process writes reaches stdout.
"""
import ctypes
import json
import os
import selectors
import signal
import socket
import struct
import sys
import time

SOCK = "/run/heldout/broker.sock"
_LIBC = ctypes.CDLL(None, use_errno=True)
_UL = ctypes.c_ulong
PR_CAPBSET_DROP, PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, PR_SET_NO_NEW_PRIVS = 24, 47, 4, 38
FORWARDED = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1,
             signal.SIGUSR2, signal.SIGKILL, signal.SIGCONT, signal.SIGSTOP}
_TERM = []


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def note(text):
    sys.stderr.write("heldout-init: " + text + "\n")
    sys.stderr.flush()


def drop_to(uid):
    """Become uid/gid ``uid`` for good: no groups, empty bounding/ambient/permitted sets."""
    with open("/proc/sys/kernel/cap_last_cap") as fh:
        last = int(fh.read())
    for cap in range(last + 1):
        _LIBC.prctl(PR_CAPBSET_DROP, _UL(cap), _UL(0), _UL(0), _UL(0))
    _LIBC.prctl(PR_CAP_AMBIENT, _UL(PR_CAP_AMBIENT_CLEAR_ALL), _UL(0), _UL(0), _UL(0))
    _LIBC.prctl(PR_SET_NO_NEW_PRIVS, _UL(1), _UL(0), _UL(0), _UL(0))
    os.setgroups([])
    os.setresgid(uid, uid, uid)
    os.setresuid(uid, uid, uid)
    if os.getresuid() != (uid, uid, uid) or os.getresgid() != (uid, uid, uid) or os.getgroups():
        os._exit(125)


def child_defaults():
    signal.set_wakeup_fd(-1)
    for sig in (signal.SIGPIPE, signal.SIGXFSZ, signal.SIGTERM, signal.SIGINT, signal.SIGCHLD,
                signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_SETMASK, [])
    os.setsid()


def spawn_step(step, judge):
    pid = os.fork()
    if pid:
        return pid
    try:
        child_defaults()
        drop_to(judge)
        os.umask(0o022)
        for path, mode in step.get("mkdirs", []):
            os.makedirs(path, exist_ok=True)
            os.chmod(path, mode)
        log = os.open(step["log"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        null = os.open("/dev/null", os.O_RDONLY)
        os.dup2(null, 0)
        os.dup2(log, 1)
        os.dup2(log, 2)
        os.closerange(3, 1 << 16)
        os.chdir(step["cwd"])
        argv = step["argv"]
        os.execvpe(argv[0], argv, step["env"])
    except BaseException as exc:  # noqa: BLE001
        try:
            os.write(2, ("heldout-init: step failed to start: %s\n" % type(exc).__name__).encode())
        finally:
            os._exit(127)


def spawn_impl(req, fds, impl):
    pid = os.fork()
    if pid:
        return pid
    try:
        child_defaults()
        for i in range(3):
            os.dup2(fds[i], i)
        os.closerange(3, 1 << 16)
        drop_to(impl)
        try:
            os.chdir(req["cwd"])
        except OSError:
            os.chdir("/")
        argv = [str(a) for a in req["argv"]]
        env = {str(k): str(v) for k, v in req["env"].items() if k != "HELDOUT_RUN_RECORDS"}
        os.execvpe(argv[0], argv, env)
    except FileNotFoundError:
        os.write(2, b"heldout-impl: command not found\n")
        os._exit(127)
    except PermissionError:
        os.write(2, b"heldout-impl: permission denied\n")
        os._exit(126)
    except BaseException:  # noqa: BLE001
        os.write(2, b"heldout-impl: cannot start command\n")
        os._exit(126)


def other_pids():
    me = os.getpid()
    out = []
    for name in os.listdir("/proc"):
        if name.isdigit() and int(name) not in (1, me):
            out.append(int(name))
    return out


class Broker:
    def __init__(self, cfg):
        self.judge = int(cfg["judge_uid"])
        self.impl = int(cfg["impl_uid"])
        self.sel = selectors.DefaultSelector()
        self.children = {}  # pid -> conn state
        self.exited = {}  # pid -> exit code (non-broker children)
        rfd, wfd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.wake_r = rfd
        signal.set_wakeup_fd(wfd)
        for sig in (signal.SIGCHLD, signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, self._on_signal)
        self.sel.register(rfd, selectors.EVENT_READ, ("wake", None))
        try:
            os.unlink(SOCK)
        except FileNotFoundError:
            pass
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(SOCK)
        os.chmod(SOCK, 0o666)
        self.srv.listen(64)
        self.srv.setblocking(False)
        self.sel.register(self.srv, selectors.EVENT_READ, ("listen", None))

    @staticmethod
    def _on_signal(signum, frame):
        if signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            _TERM.append(signum)

    def reap(self):
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            code = os.waitstatus_to_exitcode(status)
            st = self.children.pop(pid, None)
            if st is not None:
                self._finish(st, code)
            else:
                self.exited[pid] = code

    def _finish(self, st, code):
        msg = {"signal": -code} if code < 0 else {"exit": code}
        conn = st["conn"]
        try:
            conn.setblocking(True)
            conn.settimeout(5)
            conn.sendall((json.dumps(msg) + "\n").encode())
        except OSError:
            pass
        self._close(conn)

    def _close(self, conn):
        try:
            self.sel.unregister(conn)
        except (KeyError, ValueError):
            pass
        conn.close()

    def accept(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        try:
            cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", cred)
        except OSError:
            conn.close()
            return
        if uid != self.judge:
            conn.close()
            return
        conn.setblocking(False)
        st = {"conn": conn, "buf": b"", "fds": None, "pid": None}
        self.sel.register(conn, selectors.EVENT_READ, ("conn", st))

    def readable(self, st):
        conn = st["conn"]
        try:
            if st["fds"] is None:
                data, fds, _flags, _addr = socket.recv_fds(conn, 1 << 16, 3)
                st["fds"] = list(fds)
            else:
                data = conn.recv(1 << 16)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._client_gone(st)
            return
        st["buf"] += data
        if len(st["buf"]) > (8 << 20):
            self._client_gone(st)
            return
        while b"\n" in st["buf"]:
            line, st["buf"] = st["buf"].split(b"\n", 1)
            line = line.lstrip(b"\0")
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._client_gone(st)
                return
            if st["pid"] is None:
                self._start(st, msg)
            elif isinstance(msg.get("signal"), int) and msg["signal"] in FORWARDED:
                try:
                    os.kill(st["pid"], msg["signal"])
                except OSError:
                    pass

    def _start(self, st, msg):
        fds = st["fds"] or []
        ok = (isinstance(msg, dict) and len(fds) == 3 and isinstance(msg.get("argv"), list)
              and msg["argv"] and isinstance(msg.get("env"), dict)
              and isinstance(msg.get("cwd"), str))
        if not ok:
            for fd in fds:
                os.close(fd)
            self._close(st["conn"])
            return
        pid = spawn_impl(msg, fds, self.impl)
        for fd in fds:
            os.close(fd)
        st["fds"] = []
        st["pid"] = pid
        self.children[pid] = st

    def _client_gone(self, st):
        for fd in st["fds"] or []:
            try:
                os.close(fd)
            except OSError:
                pass
        st["fds"] = []
        pid = st["pid"]
        if pid is not None and pid in self.children:
            for target in (-pid, pid):
                try:
                    os.kill(target, signal.SIGKILL)
                except OSError:
                    pass
            # the reaper still finds it; nobody to tell
            st["conn_closed"] = True
        self._close(st["conn"])

    def poll(self, timeout):
        for key, _ in self.sel.select(timeout):
            kind, st = key.data
            if kind == "wake":
                try:
                    while os.read(self.wake_r, 4096):
                        pass
                except OSError:
                    pass
            elif kind == "listen":
                self.accept()
            else:
                self.readable(st)
        self.reap()

    def kill_all(self):
        for _ in range(50):
            pids = other_pids()
            if not pids:
                break
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.02)
            self.reap()
        for st in list(self.children.values()):
            self._close(st["conn"])
        self.children.clear()


def main():
    cfg = json.loads(sys.stdin.buffer.read() or b"{}")
    broker = Broker(cfg)
    judge = broker.judge
    for step in cfg.get("steps", []):
        name = step["name"]
        emit({"event": "start", "step": name, "t": time.time()})
        pid = spawn_step(step, judge)
        while pid not in broker.exited and not _TERM:
            broker.poll(0.5)
        if _TERM:
            broker.kill_all()
            emit({"event": "terminated", "step": name})
            return 143
        emit({"event": "exit", "step": name, "rc": broker.exited.pop(pid)})
        broker.kill_all()
    emit({"event": "done"})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        note("failed: %s" % type(exc).__name__)
        sys.exit(70)
'''

_SANDBOX_CLIENT = r'''#!/usr/local/bin/python3 -I
"""heldout-impl CMD [ARGS...]: run CMD as the impl user (uid 10102) via the heldout-init broker.

CMD gets this process's stdin, stdout, stderr, working directory (``/`` when impl cannot enter
it) and environment minus HELDOUT_RUN_RECORDS; the exit status (or killing signal) is CMD's.
This client holds no privilege: the broker (PID 1) does the switch and accepts judge callers only.
"""
import json
import os
import signal
import socket
import sys

SOCK = "/run/heldout/broker.sock"


def main(argv):
    if not argv:
        sys.stderr.write("usage: heldout-impl CMD [ARGS...]\n")
        return 2
    env = {k: v for k, v in os.environ.items() if k != "HELDOUT_RUN_RECORDS"}
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = "/"
    fds = []
    for i in range(3):
        try:
            os.fstat(i)
            fds.append(i)
        except OSError:
            fds.append(os.open("/dev/null", os.O_RDWR))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(SOCK)
        socket.send_fds(sock, [b"\0"], fds)
        sock.sendall(json.dumps({"argv": argv, "env": env, "cwd": cwd}).encode() + b"\n")
    except OSError:
        sys.stderr.write("heldout-impl: broker unavailable\n")
        return 125

    def forward(signum, frame):
        try:
            sock.sendall(json.dumps({"signal": signum}).encode() + b"\n")
        except OSError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1,
                signal.SIGUSR2):
        signal.signal(sig, forward)
    buf = b""
    while b"\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except InterruptedError:
            continue
        if not chunk:
            sys.stderr.write("heldout-impl: broker closed the connection\n")
            return 125
        buf += chunk
    res = json.loads(buf.split(b"\n", 1)[0])
    if "signal" in res:
        sig = int(res["signal"])
        signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)
        return 128 + sig
    return int(res.get("exit", 125))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

SANDBOX_DOCKERFILE = (
    """# heldout-run sandbox image (spec 008). Written and built by scripts/heldout_run.py.
FROM python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_ROOT_USER_ACTION=ignore
RUN apt-get update \\
 && apt-get install -y --no-install-recommends git ca-certificates \\
 && rm -rf /var/lib/apt/lists/*
RUN pip install "pytest==9.0.2" "uv==0.11.7"
RUN groupadd -g 10101 judge \\
 && useradd -u 10101 -g 10101 -M -d /tmp/heldout-home -s /usr/sbin/nologin judge \\
 && groupadd -g 10102 impl \\
 && useradd -u 10102 -g 10102 -m -d /home/impl -s /bin/sh impl \\
 && mkdir -p /work /suite /out /run/heldout \\
 && chown impl:judge /work && chmod 2775 /work \\
 && chown judge:judge /suite /out && chmod 0700 /suite /out
COPY <<"HELDOUT_EOF" /usr/local/sbin/heldout-init
"""
    + _SANDBOX_INIT
    + """HELDOUT_EOF
COPY <<"HELDOUT_EOF" /usr/local/bin/heldout-impl
"""
    + _SANDBOX_CLIENT
    + """HELDOUT_EOF
RUN chmod 0755 /usr/local/sbin/heldout-init /usr/local/bin/heldout-impl
"""
)


def sandbox_image() -> str:
    """The default sandbox image tag: ``heldout-sandbox:<first 12 hex of sha256(Dockerfile)>``."""
    return "heldout-sandbox:" + hashlib.sha256(SANDBOX_DOCKERFILE.encode("utf-8")).hexdigest()[:12]


class HeldoutError(Exception):
    """A refusal or infra failure carrying one of ERROR_CODES."""

    def __init__(self, code: str):
        assert code in ERROR_CODES, code
        super().__init__(code)
        self.code = code

    @property
    def exit_code(self) -> int:
        return _EXIT_OF_CODE.get(self.code, 2)


class _Interrupted(BaseException):
    pass


# ---------------------------------------------------------------------------------------------
# id mapping and feedback (pure helpers)


def _is_id(value) -> bool:
    return isinstance(value, str) and _ID_RX.match(value) is not None


def _normalise_ids(value) -> tuple[str, ...]:
    # Documented shape: a list of id strings (a tuple is accepted from a .py literal).
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return tuple(dict.fromkeys(value))
    raise HeldoutError("E_MAP")


def _validate_map(obj) -> dict[str, tuple[str, ...]]:
    if not isinstance(obj, dict):
        raise HeldoutError("E_MAP")
    out: dict[str, tuple[str, ...]] = {}
    for key, value in obj.items():
        if not isinstance(key, str):
            raise HeldoutError("E_MAP")
        out[key] = _normalise_ids(value)
    return out


def _map_from_py(text: str):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        raise HeldoutError("E_MAP") from None
    node = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "MAP" for t in stmt.targets):
                node = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == "MAP" and stmt.value:
                node = stmt.value
    if node is None:
        raise HeldoutError("E_MAP")
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        raise HeldoutError("E_MAP") from None


def load_id_map(path) -> dict[str, tuple[str, ...]]:
    """Read an id map: JSON ``{key: [ids]}`` (optionally under ``"map"``) or a ``.py`` MAP literal.

    A ``.py`` file is parsed, never imported or executed. Errors raise ``HeldoutError("E_MAP")``.
    """
    path = os.fspath(path)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise HeldoutError("E_MAP") from None
    if path.endswith(".py"):
        obj = _map_from_py(text)
    else:
        try:
            obj = json.loads(text)
        except ValueError:
            raise HeldoutError("E_MAP") from None
        if isinstance(obj, dict) and "map" in obj:
            # Wrapped form {"map": {...}}: the wrapper holds only the map, and it must be an object.
            if set(obj) != {"map"} or not isinstance(obj["map"], dict):
                raise HeldoutError("E_MAP")
            obj = obj["map"]
    return _validate_map(obj)


def _labels(ids) -> set[str]:
    if isinstance(ids, str):
        ids = (ids,)
    labels: set[str] = set()
    for value in ids or ():
        labels.add(value if _is_id(value) else INVALID_ID)
    return labels or {UNMAPPED}


_PREFIX_ORDER = {"FR": 0, "NFR": 1, "SC": 2, INVALID_ID: 3, UNMAPPED: 4}


def _id_sort_key(label: str):
    m = re.match(r"^(FR|NFR|SC)-([0-9]{3})([a-z]?)$", label)
    if m:
        return (_PREFIX_ORDER[m.group(1)], int(m.group(2)), m.group(3))
    return (_PREFIX_ORDER.get(label, 5), 0, label)


def _safe_label(label) -> bool:
    return _is_id(label) or label in (INVALID_ID, UNMAPPED)


def summarize(records, *, feature, round, cap, status) -> dict:  # noqa: A002
    """Build the ``heldout-feedback.v1`` object from test records (ids and counts only).

    For status ``tampered`` (spec 008 FR-017) the object carries only the schema, feature, round,
    cap and status: no ids, no counts.
    """
    if status == "tampered":
        return {"schema": FEEDBACK_SCHEMA, "feature": feature, "round": round,
                "round_cap": cap, "status": status}
    per: dict[str, list[int]] = {}
    tests = failing = skipped = 0
    for rec in records:
        outcome = rec.get("outcome")
        if outcome == "skipped":
            skipped += 1
            continue
        bad = outcome != "passed"
        tests += 1
        failing += int(bad)
        for label in _labels(rec.get("ids")):
            entry = per.setdefault(label, [0, 0])
            entry[1] += 1
            entry[0] += int(bad)
    ordered = sorted(per, key=_id_sort_key)
    failing_obj = {k: {"failing": per[k][0], "total": per[k][1]} for k in ordered if per[k][0] > 0}
    return {
        "schema": FEEDBACK_SCHEMA,
        "feature": feature,
        "round": round,
        "round_cap": cap,
        "status": status,
        "cap_reached": bool(round is not None and round >= cap and status != "passed"),
        "failing": failing_obj,
        "totals": {
            "tests": tests,
            "failing": failing,
            "skipped": skipped,
            "ids": sum(1 for k in per if per[k][1] > 0),
            "ids_failing": len(failing_obj),
        },
    }


def format_feedback(feedback: dict) -> str:
    """Render the FR-021 text feedback (no trailing newline)."""
    rnd = feedback.get("round")
    r = "-" if rnd is None else str(int(rnd))
    status = feedback.get("status")
    if status not in STATUSES:
        status = "infra_error"
    feature = feedback.get("feature")
    if not isinstance(feature, str) or not FEATURE_RX.match(feature):
        feature = "-"
    cap = int(feedback.get("round_cap", DEFAULT_CAP))
    lines = [f"heldout-run: {feature} round {r}/{cap}: {status}"]
    if status == "tampered":
        lines.append("status: tampered")
        if rnd is not None and int(rnd) >= cap:
            lines.append("round cap reached: escalate to owner")
        return "\n".join(lines)
    failing = feedback.get("failing") or {}
    for label in sorted((k for k in failing if _safe_label(k)), key=_id_sort_key):
        v = failing[label]
        lines.append(f"{label}: {int(v['failing'])}/{int(v['total'])} failing")
    t = feedback.get("totals") or {}
    lines.append(
        f"total: {int(t.get('failing', 0))}/{int(t.get('tests', 0))} tests failing, "
        f"{int(t.get('skipped', 0))} skipped; {int(t.get('ids_failing', 0))}/{int(t.get('ids', 0))}"
        " ids failing"
    )
    if feedback.get("cap_reached"):
        lines.append("round cap reached: escalate to owner")
    return "\n".join(lines)


def _sanitize_feedback(fb: dict) -> dict:
    """Final redaction guard: only ids/labels, the validated feature and integers survive."""
    if fb.get("status") == "tampered":
        clean = {k: fb.get(k) for k in ("schema", "feature", "round", "round_cap", "status")}
        if not (isinstance(fb["feature"], str) and FEATURE_RX.match(fb["feature"])):
            clean["feature"] = "-"
        return clean
    clean = dict(fb)
    clean["failing"] = {
        k: {"failing": int(v["failing"]), "total": int(v["total"])}
        for k, v in fb["failing"].items()
        if _safe_label(k)
    }
    if not (isinstance(fb["feature"], str) and FEATURE_RX.match(fb["feature"])):
        clean["feature"] = "-"
    return clean


# ---------------------------------------------------------------------------------------------
# requirement text (triage)

_LIST_ITEM_RX = re.compile(r"^([ \t]*)(?:[-*+]|[0-9]+[.)])(?:[ \t]|$)")
_HEADING_RX = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]|$)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def extract_requirement(spec_text: str, req_id: str) -> str | None:
    """Return the markdown list item starting ``- **<id>**`` (FR-037), or None when absent."""
    start_rx = re.compile(
        r"^([ \t]*)[-*+][ \t]+\*\*" + re.escape(req_id) + r"(?:[ \t]*\([^\n]*?\))?\*\*"
    )
    lines = spec_text.splitlines()
    for i, line in enumerate(lines):
        m = start_rx.match(line)
        if not m:
            continue
        base = len(m.group(1))
        out = [line]
        j = i + 1
        while j < len(lines):
            cur = lines[j]
            if not cur.strip():
                nxt = j + 1
                while nxt < len(lines) and not lines[nxt].strip():
                    nxt += 1
                if nxt >= len(lines) or _indent(lines[nxt]) == 0:
                    break
                out.append(cur)
                j += 1
                continue
            if _HEADING_RX.match(cur):
                break
            item = _LIST_ITEM_RX.match(cur)
            if item and len(item.group(1)) <= base:
                break
            out.append(cur)
            j += 1
        while out and not out[-1].strip():
            out.pop()
        return "\n".join(line.rstrip() for line in out)
    return None


# ---------------------------------------------------------------------------------------------
# filesystem helpers


def _runner_file() -> str:
    return os.path.realpath(__file__)


def _runner_bytes() -> bytes:
    with open(_runner_file(), "rb") as fh:
        return fh.read()


def _runner_sha256() -> str:
    return hashlib.sha256(_runner_bytes()).hexdigest()


def _real(path) -> str:
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def _inside(path: str, base: str) -> bool:
    path, base = _real(path), _real(base)
    if path == base:
        return True
    base = base.rstrip(os.sep) + os.sep
    return path.startswith(base)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(ts: datetime.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _mkdir_private(path: str) -> None:
    path = os.path.abspath(path)
    missing = []
    cur = path
    while not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        os.chmod(d, 0o700)


def _write_private(path: str, data) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
    finally:
        if fd >= 0:
            os.close(fd)


def _write_json_atomic(path: str, obj) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    _write_private(tmp, json.dumps(obj, indent=2, sort_keys=False) + "\n")
    os.replace(tmp, path)


def _rmtree(path: str | None) -> None:
    if not path or not os.path.lexists(path):
        return
    try:
        shutil.rmtree(path)
        return
    except OSError:
        pass
    for dirpath, dirnames, _ in os.walk(path):
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if not os.path.islink(p):
                try:
                    os.chmod(p, 0o700)
                except OSError:
                    pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _copy_filtered(src: str, dest: str) -> None:
    """Copy a directory tree as is: symlinks stay symlinks, excluded names are skipped."""
    os.makedirs(dest, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target = dest if rel == "." else os.path.join(dest, rel)
        keep = []
        for d in dirnames:
            if d in EXCLUDED_NAMES:
                continue
            s = os.path.join(dirpath, d)
            if os.path.islink(s):
                os.symlink(os.readlink(s), os.path.join(target, d))
                continue
            os.mkdir(os.path.join(target, d))
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if f in EXCLUDED_NAMES or f.endswith(".pyc"):
                continue
            s = os.path.join(dirpath, f)
            t = os.path.join(target, f)
            if os.path.islink(s):
                os.symlink(os.readlink(s), t)
            elif os.path.isfile(s):
                shutil.copy2(s, t)


def _tree_sha256(root: str) -> str:
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_NAMES)
        rel_dir = os.path.relpath(dirpath, root)
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        for name in sorted(filenames + links):
            if name in EXCLUDED_NAMES or name.endswith(".pyc"):
                continue
            p = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            h.update(rel.encode("utf-8", "surrogateescape") + b"\0")
            if os.path.islink(p):
                h.update(b"L" + os.readlink(p).encode("utf-8", "surrogateescape") + b"\0")
            elif os.path.isfile(p):
                h.update(b"F")
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 16), b""):
                        h.update(chunk)
                h.update(b"\0")
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------
# git


def _git(repo: str, *args: str, timeout: float = 120.0) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        raise HeldoutError("E_GIT") from None
    if proc.returncode != 0:
        raise HeldoutError("E_GIT")
    return proc.stdout.decode("utf-8", "surrogateescape")


def _worktrees(repo: str, *, strict: bool) -> list[str]:
    try:
        out = _git(repo, "worktree", "list", "--porcelain")
    except HeldoutError:
        if strict:
            raise
        return []
    return [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]


def _resolve_commit(repo: str, ref: str) -> str:
    if ref.startswith("-"):
        raise HeldoutError("E_GIT")
    commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise HeldoutError("E_GIT")
    return commit


def _extract_archive(repo: str, commit: str, dest: str) -> None:
    try:
        proc = subprocess.Popen(
            ["git", "-C", repo, "archive", "--format=tar", commit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise HeldoutError("E_GIT") from None
    ok = True
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            for member in tf:
                parts = member.name.split("/")
                if ".git" in parts or member.name.startswith("/") or ".." in parts:
                    continue
                if hasattr(tarfile, "tar_filter"):
                    tf.extract(member, dest, filter="tar")
                else:  # pragma: no cover - Python < 3.11.4
                    tf.extract(member, dest)
    except (tarfile.TarError, OSError):
        ok = False
    finally:
        if proc.stdout:
            proc.stdout.close()
        rc = proc.wait()
    if rc != 0 or not ok:
        raise HeldoutError("E_GIT")


# ---------------------------------------------------------------------------------------------
# argument parsing


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # noqa: D102 - never echo argument values
        raise HeldoutError("E_USAGE")

    def exit(self, status=0, message=None):  # noqa: D102
        if status:
            raise HeldoutError("E_USAGE")
        raise SystemExit(0)


def _positive_float(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise ValueError
    return value


def _memory(text: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*[bkmgBKMG]?", text):
        raise ValueError
    return text


def _image_name(text: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}", text):
        raise ValueError
    return text


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise ValueError
    return value


def _build_parser() -> _Parser:
    fmt = argparse.RawDescriptionHelpFormatter
    p = _Parser(
        prog="heldout-run",
        description="Run a held-out pytest suite on a throwaway copy; print redacted feedback.\n\n"
        + LIMIT_TEXT,
        formatter_class=fmt,
        allow_abbrev=False,
    )
    p.add_argument("--version", action="store_true", help="print version and sha256 of this file")
    sub = p.add_subparsers(dest="command", parser_class=_Parser)

    def common(sp):
        sp.add_argument("--repo-name", required=True)
        sp.add_argument("--feature", required=True)
        sp.add_argument("--heldout-root", help="held-out root (default: $HELDOUT_ROOT, else HOME)")

    r = sub.add_parser(
        "run",
        help="run a suite and print feedback",
        description="Run a held-out suite against a copy of a tree.\n\n" + LIMIT_TEXT,
        formatter_class=fmt,
        allow_abbrev=False,
    )
    common(r)
    r.add_argument("--worktree")
    r.add_argument("--repo")
    r.add_argument("--ref")
    r.add_argument("--profile", choices=sorted(PROFILES), default="generic")
    r.add_argument("--suite")
    r.add_argument("--suite-dest")
    r.add_argument("--report-dir")
    r.add_argument("--scratch")
    r.add_argument("--map", dest="map_file")
    r.add_argument("--timeout", type=_positive_float)
    r.add_argument("--sync-timeout", type=_positive_float, default=DEFAULT_SYNC_TIMEOUT)
    r.add_argument("--no-sync", action="store_true")
    r.add_argument("--pytest-arg", action="append", default=[])
    r.add_argument("--unset", action="append", default=[])
    r.add_argument("--forbid-under", action="append", default=[])
    r.add_argument("--cap", type=_positive_int, default=DEFAULT_CAP)
    r.add_argument("--no-count", action="store_true")
    r.add_argument("--keep", action="store_true")
    r.add_argument("--json", action="store_true")
    r.add_argument("--feedback-file")
    r.add_argument("--sync-cmd", help="sync step command ({copy} is the copy's path)")
    r.add_argument("--sandbox", choices=SANDBOX_MODES, default="none",
                   help="none (default, spec 007 behaviour) or docker (spec 008)")
    r.add_argument("--sandbox-image", type=_image_name,
                   help="image to use instead of the built heldout-sandbox:<tag>")
    r.add_argument("--sandbox-memory", type=_memory, default=SANDBOX_DEFAULT_MEMORY)
    r.add_argument("--sandbox-cpus", type=_positive_float, default=SANDBOX_DEFAULT_CPUS)

    t = sub.add_parser(
        "triage",
        help="PRIVATE: failing assertions next to spec text (head only)",
        allow_abbrev=False,
    )
    common(t)
    t.add_argument("--round", type=_positive_int)
    t.add_argument("--id", dest="ids", action="append", default=[])
    t.add_argument("--spec", required=True)
    t.add_argument("--json", action="store_true")

    rd = sub.add_parser("rounds", help="list or reset rounds", allow_abbrev=False)
    common(rd)
    rd.add_argument("--reset", action="store_true")
    rd.add_argument("--reason")

    c = sub.add_parser(
        "check-isolation", help="placement and permission checks only", allow_abbrev=False
    )
    c.add_argument("--suite", required=True)
    c.add_argument("--repo")
    c.add_argument("--forbid-under", action="append", default=[])
    c.add_argument("--heldout-root")
    return p


def _preprocess(argv: list[str]) -> list[str]:
    """Let ``--pytest-arg -x`` work: glue a dash-leading value to its option."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--pytest-arg" and i + 1 < len(argv):
            out.append(f"--pytest-arg={argv[i + 1]}")
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def _check_names(repo_name: str, feature: str) -> None:
    if not (isinstance(feature, str) and FEATURE_RX.match(feature)):
        raise HeldoutError("E_FEATURE")
    if not (isinstance(repo_name, str) and REPO_RX.match(repo_name)):
        raise HeldoutError("E_FEATURE")


def _heldout_root(explicit: str | None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("HELDOUT_ROOT")
    if env:
        return os.path.abspath(env)
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(os.path.abspath(home), "projects", ".heldout")


def _report_root(root: str, repo_name: str, feature: str) -> str:
    return os.path.join(root, "_reports", repo_name, feature)


def _check_perms(suite: str, root: str) -> None:
    for path in (suite, root):
        if os.path.isdir(path) and os.stat(path).st_mode & 0o077:
            raise HeldoutError("E_SUITE_PERMS")


def _forbidden_bases(tree: str | None, repo: str | None, forbid, *, strict_git: bool) -> list[str]:
    bases = []
    if tree:
        bases.append(tree)
    if repo:
        bases.extend(_worktrees(repo, strict=strict_git))
    bases.extend(os.path.abspath(p) for p in forbid)
    return bases


def _check_placement(bases, *, suite: str, report_dir: str | None, root: str) -> None:
    for base in bases:
        if _inside(suite, base):
            raise HeldoutError("E_SUITE_IN_TREE")
    for base in bases:
        if (report_dir and _inside(report_dir, base)) or _inside(root, base):
            raise HeldoutError("E_REPORT_IN_TREE")
    if report_dir and _inside(report_dir, suite):
        raise HeldoutError("E_REPORT_IN_TREE")


# ---------------------------------------------------------------------------------------------
# process control


class _State:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.docker: _Docker | None = None


def _killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _run_group(cmd, *, cwd, env, log_fh, timeout, state: _State) -> tuple[int | None, bool]:
    """Run ``cmd`` in a new process group with a wall-clock timeout; (returncode, timed_out)."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    state.proc = proc
    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                _killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    finally:
        if proc.poll() is None:
            _killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        # Leftover members of the group (background children of the suite).
        _killpg(proc.pid, signal.SIGKILL)
        state.proc = None
    return proc.returncode, timed_out


def _expand(items, copy: str) -> list[str]:
    return [s.replace("{copy}", copy) for s in items]


# ---------------------------------------------------------------------------------------------
# records -> tests


def _read_records(path: str):
    collected: dict[str, dict] = {}
    order: list[str] = []
    reports: dict[str, list[dict]] = {}
    collect_errors = 0
    finished = None
    if not os.path.exists(path):
        return collected, order, reports, collect_errors, finished
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            nodeid = str(rec.get("nodeid", ""))
            if kind == "collected":
                if nodeid not in collected:
                    order.append(nodeid)
                collected[nodeid] = rec
            elif kind == "report":
                reports.setdefault(nodeid, []).append(rec)
                if nodeid not in collected and nodeid not in order:
                    order.append(nodeid)
            elif kind == "collect_error":
                collect_errors += 1
            elif kind == "finish":
                finished = rec.get("exitstatus")
    return collected, order, reports, collect_errors, finished


def _key_from_nodeid(nodeid: str) -> str:
    return nodeid.rsplit("::", 1)[-1].split("[", 1)[0]


def _outcome(phases: list[dict], timed_out: bool) -> tuple[str, str, str]:
    failed = [p for p in phases if p.get("outcome") == "failed"]
    if failed:
        text = "\n".join(str(p.get("longrepr") or "") for p in failed)
        return "failed", str(failed[0].get("when")), text
    if not any(p.get("when") == "teardown" for p in phases):
        why = "timeout" if timed_out else "missing"
        return "failed", why, f"no final result ({why})"
    skipped = [p for p in phases if p.get("outcome") == "skipped"]
    if skipped:
        if any(p.get("wasxfail") for p in skipped):
            return "passed", str(skipped[0].get("when")), str(skipped[0].get("longrepr") or "")
        return "skipped", str(skipped[0].get("when")), str(skipped[0].get("longrepr") or "")
    return "passed", "call", ""


def _build_tests(records_path: str, id_map: dict, timed_out: bool):
    collected, order, reports, collect_errors, finished = _read_records(records_path)
    tests = []
    for nodeid in order:
        info = collected.get(nodeid, {})
        key = str(info.get("key") or _key_from_nodeid(nodeid))
        marks = [str(m) for m in info.get("marks") or []]
        ids = list(dict.fromkeys(marks + list(id_map.get(key, ()))))
        phases = reports.get(nodeid, [])
        outcome, when, longrepr = _outcome(phases, timed_out)
        tests.append(
            {
                "nodeid": nodeid,
                "key": key,
                "ids": ids,
                "outcome": outcome,
                "when": when,
                "duration_s": round(sum(float(p.get("duration") or 0) for p in phases), 6),
                "longrepr": longrepr,
            }
        )
    return tests, bool(reports), collect_errors, finished


def _decide_status(*, timed_out, rc, has_reports, collect_errors, tests) -> str:
    if timed_out:
        return "timeout"
    if collect_errors or rc in (2, 3, 4, 5) or not has_reports:
        return "collection_error"
    if rc == 0 and not any(t["outcome"] == "failed" for t in tests):
        return "passed"
    return "failed"


# ---------------------------------------------------------------------------------------------
# rounds


def _load_rounds(path: str, cap: int) -> dict:
    if not os.path.exists(path):
        return {"schema": ROUNDS_SCHEMA, "cap": cap, "rounds": [], "resets": []}
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        raise HeldoutError("E_USAGE") from None
    if not isinstance(obj, dict):
        raise HeldoutError("E_USAGE")
    obj.setdefault("schema", ROUNDS_SCHEMA)
    obj.setdefault("cap", cap)
    obj.setdefault("rounds", [])
    obj.setdefault("resets", [])
    return obj


class _Lock:
    def __init__(self, report_root: str):
        self.path = os.path.join(report_root, ".lock")
        self.fd = -1

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = -1
            raise HeldoutError("E_BUSY") from None
        return self

    def __exit__(self, *exc):
        if self.fd >= 0:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = -1


def _run_stamp() -> str:
    return _utcnow().strftime("run-%Y%m%dT%H%M%SZ")


def _fresh_run_dir(report_root: str) -> str:
    while True:
        path = os.path.join(report_root, _run_stamp())
        if not os.path.lexists(path):
            return path
        time.sleep(0.2)


# ---------------------------------------------------------------------------------------------
# output


def _emit_feedback(fb: dict, *, as_json: bool, feedback_file: str | None, out) -> None:
    fb = _sanitize_feedback(fb)
    if as_json:
        out.write(json.dumps(fb) + "\n")
    else:
        out.write(format_feedback(fb) + "\n")
    out.flush()
    if feedback_file:
        try:
            with open(feedback_file, "w", encoding="utf-8") as fh:
                json.dump(fb, fh)
                fh.write("\n")
        except OSError:
            pass


def _infra_feedback(feature: str, cap: int) -> dict:
    return summarize([], feature=feature, round=None, cap=cap, status="infra_error")


# ---------------------------------------------------------------------------------------------
# sandbox (spec 008): docker plumbing, the two phases and tamper detection


def _split_cmd(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return []


_MARKER_TOKEN_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*|\S")


def _marker_exprs(pytest_args) -> list[str]:
    exprs, args, i = [], list(pytest_args), 0
    while i < len(args):
        arg = args[i]
        if arg == "-m":
            if i + 1 < len(args):
                exprs.append(args[i + 1])
            i += 2
            continue
        if arg.startswith("-m") and not arg.startswith("--"):
            exprs.append(arg[2:].lstrip("=").strip())
        i += 1
    return exprs


def _selects_docker_marker(pytest_args) -> bool:
    """True when a ``-m`` expression selects ``docker`` (a ``docker`` term not negated by ``not``)."""
    for expr in _marker_exprs(pytest_args):
        prev = None
        for tok in _MARKER_TOKEN_RX.findall(expr):
            if tok in ("(", ")"):
                continue
            if tok == "docker" and prev != "not":
                return True
            prev = tok
    return False


def _narrows_selection(pytest_args) -> bool:
    """Whether extra pytest arguments may deselect tests (then canaries may be absent)."""
    for arg in pytest_args:
        if not arg.startswith("-") or "::" in arg:
            return True
        if re.match(r"-[km]", arg) or arg.startswith(
            ("--deselect", "--lf", "--last-failed", "--sw", "--stepwise", "--ignore",
             "--pyargs", "--collect-in-virtualenv")
        ):
            return True
    return False


def _stops_early(pytest_args) -> bool:
    for arg in pytest_args:
        if arg in ("-x", "--exitfirst") or arg.startswith(("--maxfail", "--sw", "--stepwise")):
            return True
        if re.fullmatch(r"-[qvsrlx]*x[qvsrlx]*", arg):
            return True
    return False


class _Docker:
    """The docker CLI, with every call logged to the private sandbox.log and run cleanup."""

    def __init__(self, exe: str, log_path: str):
        self.exe = exe
        self.log_path = log_path
        self.containers: list[str] = []
        self.volumes: list[str] = []

    @classmethod
    def connect(cls, log_path: str) -> "_Docker":
        exe = shutil.which("docker", path=os.environ.get("PATH") or os.defpath)
        if not exe:
            raise HeldoutError("E_SANDBOX_UNAVAILABLE")
        dk = cls(exe, log_path)
        p = dk.run(["version", "--format", "{{.Server.Version}}"], timeout=60, capture=True)
        if p is None or p.returncode != 0 or not p.stdout.strip():
            raise HeldoutError("E_SANDBOX_UNAVAILABLE")
        return dk

    def _log(self):
        try:
            fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError:  # the report directory is gone: cleanup must still run
            fd = os.open(os.devnull, os.O_WRONLY)
        return os.fdopen(fd, "ab")

    def run(self, args, *, timeout=300.0, capture=False, data: bytes | None = None,
            check: str | None = None):
        with self._log() as log:
            log.write(("$ docker " + " ".join(shlex.quote(x) for x in args[:12]) + "\n").encode())
            log.flush()
            try:
                p = subprocess.run([self.exe, *args], input=data,
                                   stdin=subprocess.DEVNULL if data is None else None,
                                   stdout=subprocess.PIPE if capture else log, stderr=log,
                                   timeout=timeout)
            except (OSError, subprocess.SubprocessError):
                p = None
        if check and (p is None or p.returncode != 0):
            raise HeldoutError(check)
        return p

    def popen(self, args, **kw) -> subprocess.Popen:
        return subprocess.Popen([self.exe, *args], start_new_session=True, **kw)

    def cleanup(self) -> None:
        """Remove every container, then every volume of the run (never raises)."""
        while self.containers:
            name = self.containers.pop()
            try:
                self.run(["rm", "-f", "-v", name], timeout=120)
            except Exception:  # noqa: BLE001
                pass
        while self.volumes:
            name = self.volumes.pop()
            try:
                self.run(["volume", "rm", "-f", name], timeout=120)
            except Exception:  # noqa: BLE001
                pass

    def ensure_image(self, image: str, scratch_dir: str, *, build: bool) -> None:
        p = self.run(["image", "inspect", "--format", "{{.Id}}", image], timeout=60, capture=True)
        if p is not None and p.returncode == 0:
            return
        if not build:
            self.run(["pull", "-q", image], timeout=1800, check="E_SANDBOX_UNAVAILABLE")
            return
        ctx = os.path.join(scratch_dir, "image")
        os.mkdir(ctx, 0o700)
        _write_private(os.path.join(ctx, "Dockerfile"), SANDBOX_DOCKERFILE)
        self.run(["build", "-t", image, "-f", os.path.join(ctx, "Dockerfile"), ctx],
                 timeout=3600, check="E_SANDBOX_UNAVAILABLE")

    def tar_into(self, container: str, dest: str, fill) -> None:
        """Stream a tar built by ``fill(tarfile)`` into ``container:dest`` (owners kept)."""
        with self._log() as log:
            proc = self.popen(["cp", "-", f"{container}:{dest}"], stdin=subprocess.PIPE,
                              stdout=log, stderr=log)
            ok = True
            try:
                with tarfile.open(fileobj=proc.stdin, mode="w|", format=tarfile.PAX_FORMAT) as tf:
                    fill(tf)
            except (OSError, tarfile.TarError):
                ok = False
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                rc = proc.wait()
        if not ok or rc != 0:
            raise HeldoutError("E_SANDBOX_UNAVAILABLE")

    def fetch_out(self, container: str, report_dir: str) -> None:
        """Copy the allowed regular files of ``container:/out`` into the report (FR-009)."""
        with self._log() as log:
            proc = self.popen(["cp", f"{container}:/out", "-"], stdout=subprocess.PIPE,
                              stderr=log)
            try:
                with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
                    for member in tf:
                        parts = member.name.split("/")
                        if (len(parts) != 2 or parts[1] not in _OUT_FILES
                                or not member.isreg()):
                            continue
                        src = tf.extractfile(member)
                        if src is None:
                            continue
                        _write_private(os.path.join(report_dir, parts[1]), src.read(_OUT_CAP))
            except (OSError, tarfile.TarError):
                pass
            finally:
                proc.stdout.close()
                proc.wait()


def _tar_tree(tf: tarfile.TarFile, src: str, uid: int, *, private: bool, gid: int | None = None,
              group_write: bool = False) -> None:
    """Add ``src``'s contents (not ``src`` itself) owned by ``uid``; skip special files.

    ``private``: judge-only modes (0700/0600). Otherwise modes are kept (setuid/setgid/sticky
    bits dropped) with the owner's rw added, and ``group_write`` makes them group-writable
    (the copy at /work: owner impl, group judge, so a suite that writes into the repo works).
    """
    group = uid if gid is None else gid

    def owned(ti: tarfile.TarInfo):
        if not (ti.isreg() or ti.isdir() or ti.issym()):
            return None
        ti.uid, ti.gid = uid, group
        ti.uname = ti.gname = ""
        if ti.isdir():
            ti.mode = 0o700 if private else (ti.mode & 0o777) | 0o700 | (0o2070 if group_write else 0)
        elif ti.isreg():
            ti.mode = 0o600 if private else (ti.mode & 0o777) | 0o600 | (0o060 if group_write else 0)
        return ti

    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            path = os.path.join(dirpath, name)
            arc = os.path.relpath(path, src)
            tf.add(path, arcname=arc, recursive=False, filter=owned)


def _tar_bytes(tf: tarfile.TarFile, name: str, data: bytes, uid: int, mode: int = 0o600) -> None:
    ti = tarfile.TarInfo(name)
    ti.size, ti.uid, ti.gid, ti.mode, ti.mtime = len(data), uid, uid, mode, int(time.time())
    tf.addfile(ti, io.BytesIO(data))


def _tar_dir(tf: tarfile.TarFile, name: str, uid: int, mode: int = 0o700) -> None:
    ti = tarfile.TarInfo(name)
    ti.type, ti.uid, ti.gid, ti.mode, ti.mtime = tarfile.DIRTYPE, uid, uid, mode, int(time.time())
    tf.addfile(ti)


def _make_canaries() -> list[dict]:
    """Two canaries with random test and file names (>= 64 random bits each, FR-014).

    They sit at the root of the suite copy; the plugin collects them even when the suite's
    ``python_files`` would not, and runs them last, the passing one (smaller file name) first,
    so ``-x`` still runs every real test before a canary can stop the session.
    """
    out = []
    files = sorted("test_" + secrets.token_hex(12) + ".py" for _ in range(2))
    for expect, fname in (("passed", files[0]), ("failed", files[1])):
        name = "test_" + secrets.token_hex(12)
        body = "assert False" if expect == "failed" else "pass"
        out.append({
            "name": name,
            "file": fname,
            "expect": expect,
            "source": f"import pytest\n\n\n@pytest.mark.fr({CANARY_LABEL!r})\n"
                      f"def {name}():\n    {body}\n",
        })
    return out


def _container_env(a, *, round_n) -> dict[str, str]:
    """007 FR-012 environment with in-container paths; host-bound names are dropped."""
    env = {k: v for k, v in os.environ.items()
           if k not in _HOST_ONLY_ENV and not k.startswith("DOCKER_")}
    for name in a.unset:
        env.pop(name, None)
    pp = [s.replace("{copy}", "/work") for s in PROFILES[a.profile]["pythonpath"]]
    env.update({
        "PATH": SANDBOX_PATH,
        "HOME": _SANDBOX_HOME,
        "TMPDIR": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": ":".join(pp + [_SANDBOX_PLUGIN]),
        ROUND_ENV: "-" if round_n is None else str(round_n),
        IMPL_ENV: SANDBOX_IMPL_PATH,
        "UV_OFFLINE": "1",
        "UV_CACHE_DIR": "/tmp/heldout-uv-cache",
    })
    return env


def _limits(a) -> list[str]:
    return ["--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(SANDBOX_PIDS_LIMIT), "--memory", a.sandbox_memory,
            "--cpus", f"{a.sandbox_cpus:g}"]


def _attach(dk: _Docker, name: str, log_path: str, timeout: float, state: _State):
    """``docker start -a`` with a wall-clock timeout; (exit code, timed_out)."""
    with open(log_path, "ab") as log:
        os.chmod(log_path, 0o600)
        proc = dk.popen(["start", "-a", name], stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT)
        state.proc = proc
        try:
            proc.wait(timeout=timeout)
            return proc.returncode, False
        except subprocess.TimeoutExpired:
            dk.run(["kill", name], timeout=60)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return None, True
        finally:
            state.proc = None


def _run_sandboxed(a, dk: _Docker, *, state, profile_name, copy, suite, plugin_dir, report_dir,
                   round_n, meta, id_map) -> dict:
    sbx = SANDBOX_PROFILES[profile_name]
    image = a.sandbox_image or sandbox_image()
    meta["image"] = image
    dk.ensure_image(image, os.path.dirname(plugin_dir), build=not a.sandbox_image)
    run_id = secrets.token_hex(8)
    meta["sandbox_run_id"] = run_id
    label = ["--label", f"{_SANDBOX_LABEL}={run_id}"]
    vol = {k: f"heldout-{run_id}-{k}" for k in ("work", "suite", "out")}
    for v in vol.values():
        dk.volumes.append(v)
        dk.run(["volume", "create", *label, v], timeout=120, check="E_SANDBOX_UNAVAILABLE")

    def fill_work(tf):
        _tar_tree(tf, copy, SANDBOX_IMPL_UID, private=False, gid=SANDBOX_JUDGE_UID,
                  group_write=True)

    # Build phase (FR-004, FR-005): untrusted code, network, the copy only.
    if a.sync_cmd is not None:
        sync = _split_cmd(a.sync_cmd)
    else:
        sync = sbx["sync"]
    work_filled = False
    if sync and not a.no_sync:
        build = f"heldout-{run_id}-build"
        dk.containers.append(build)
        dk.run(["create", "--name", build, *label, "--user",
                f"{SANDBOX_IMPL_UID}:{SANDBOX_IMPL_UID}", *_limits(a), "--network", "bridge",
                "-v", f"{vol['work']}:/work", "-w", "/work", "-e", "HOME=/home/impl",
                "-e", "UV_LINK_MODE=copy", image, *[x.replace("{copy}", "/work") for x in sync]],
               timeout=300, check="E_SANDBOX_UNAVAILABLE")
        dk.tar_into(build, "/work", fill_work)
        work_filled = True
        rc, to = _attach(dk, build, os.path.join(report_dir, "sync.log"), a.sync_timeout, state)
        dk.run(["rm", "-f", "-v", build], timeout=120)
        dk.containers.remove(build)
        if to or rc != 0:
            raise HeldoutError("E_SYNC")

    # Test phase (FR-006..FR-008): no network, read-only root, judge and impl separated.
    canaries = _make_canaries()
    test = f"heldout-{run_id}-test"
    dk.containers.append(test)
    dk.run(["create", "--name", test, *label, "--user", "0:0", "--cap-drop", "ALL",
            *[x for cap in ("SETUID", "SETGID", "SETPCAP", "KILL") for x in ("--cap-add", cap)],
            *_limits(a)[2:], "--security-opt", "no-new-privileges", "--network", "none",
            "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,mode=1777",
            "--tmpfs", "/run/heldout:rw,nosuid,nodev,noexec,mode=0755",
            "-v", f"{vol['work']}:/work", "-v", f"{vol['suite']}:/suite",
            "-v", f"{vol['out']}:/out", "-i", image, "/usr/local/sbin/heldout-init"],
           timeout=300, check="E_SANDBOX_UNAVAILABLE")
    if not work_filled:
        dk.tar_into(test, "/work", fill_work)

    def fill_suite(tf):
        _tar_tree(tf, suite, SANDBOX_JUDGE_UID, private=True)
        for c in canaries:
            _tar_bytes(tf, c["file"], c["source"].encode(), SANDBOX_JUDGE_UID)

    def fill_out(tf):
        _tar_dir(tf, "plugin", SANDBOX_JUDGE_UID)
        with open(os.path.join(plugin_dir, "heldout_run.py"), "rb") as fh:
            _tar_bytes(tf, "plugin/heldout_run.py", fh.read(), SANDBOX_JUDGE_UID)

    dk.tar_into(test, "/suite", fill_suite)
    dk.tar_into(test, "/out", fill_out)

    launcher = list(sbx["launcher"])
    targets = ["/suite"]
    plugin_args = ["-p", "no:cacheprovider", "-p", "heldout_run"]
    main_cmd = launcher + targets + ["-q", *plugin_args, "--junitxml=/out/junit.xml"]
    main_cmd += list(a.pytest_arg)
    collect_cmd = launcher + targets + ["--collect-only", "-q", *plugin_args,
                                        "--junitxml=/out/collect-junit.xml"] + list(a.pytest_arg)
    meta["pytest_command"] = main_cmd
    meta["collect_command"] = collect_cmd
    env = _container_env(a, round_n=round_n)
    cwd = _SANDBOX_CWD if PROFILES[profile_name]["cwd"] == "@scratch" else "/work"
    mkdirs = [[_SANDBOX_HOME, 0o700]] + ([[_SANDBOX_CWD, 0o755]] if cwd == _SANDBOX_CWD else [])
    config = {
        "judge_uid": SANDBOX_JUDGE_UID,
        "impl_uid": SANDBOX_IMPL_UID,
        "steps": [
            {"name": "collect", "argv": collect_cmd, "cwd": cwd, "log": "/out/collect.log",
             "mkdirs": mkdirs, "env": dict(env, **{RECORDS_ENV: "/out/collect.jsonl"})},
            {"name": "main", "argv": main_cmd, "cwd": cwd, "log": "/out/pytest.log",
             "mkdirs": mkdirs, "env": dict(env, **{RECORDS_ENV: "/out/records.jsonl"})},
        ],
    }
    timeout = a.timeout if a.timeout else PROFILES[profile_name]["timeout"]
    rcs, timed_out = _drive_test_container(dk, test, config, timeout, state)

    # FR-009: copy /out, then remove the container, the suite copy, /out and the copy.
    dk.fetch_out(test, report_dir)
    dk.cleanup()

    return _judge(a, report_dir=report_dir, canaries=canaries, rcs=rcs, timed_out=timed_out,
                  id_map=id_map, meta=meta)


def _drive_test_container(dk: _Docker, name: str, config: dict, timeout: float, state):
    """Run heldout-init; the timeout runs from the start of each pytest pass (FR-008)."""
    events: queue.Queue = queue.Queue()
    with dk._log() as log:
        proc = dk.popen(["start", "-a", "-i", name], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=log)
    state.proc = proc

    def reader():
        for line in proc.stdout:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                events.put(obj)
        events.put(None)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        proc.stdin.write(json.dumps(config).encode())
        proc.stdin.close()
    except OSError:
        pass
    rcs: dict[str, int] = {}
    timed_out = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                dk.run(["stop", "-t", str(int(KILL_GRACE_S)), name], timeout=KILL_GRACE_S + 60)
                dk.run(["kill", name], timeout=60)
                break
            try:
                ev = events.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if ev is None:
                break
            if ev.get("event") == "start":
                deadline = time.monotonic() + timeout
            elif ev.get("event") == "exit" and isinstance(ev.get("rc"), int):
                rcs[str(ev.get("step"))] = ev["rc"]
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    finally:
        state.proc = None
    t.join(10)
    return rcs, timed_out


def _junit_key(nodeid: str) -> tuple[str, str]:
    """The (classname, name) pytest's junitxml derives from a node id."""
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def _junit_outcomes(path: str):
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError, ValueError):
        return None
    out = []
    for tc in root.iter("testcase"):
        tags = [child.tag for child in tc]
        if "failure" in tags or "error" in tags:
            outcome = "failed"
        elif "skipped" in tags:
            sk = tc.find("skipped")
            outcome = "passed" if sk is not None and sk.get("type") == "pytest.xfail" else "skipped"
        else:
            outcome = "passed"
        out.append(((tc.get("classname") or "", tc.get("name") or ""), outcome))
    return out


def _final_outcomes(records_path: str) -> dict[str, str]:
    """nodeid -> outcome for every test with a final (teardown) result."""
    _c, order, reports, _e, _f = _read_records(records_path)
    out = {}
    for nodeid in order:
        phases = reports.get(nodeid, [])
        if any(p.get("when") == "teardown" for p in phases):
            out[nodeid] = _outcome(phases, False)[0]
    return out


def _collapse(outcomes: set) -> str:
    for word in ("failed", "passed", "skipped"):
        if word in outcomes:
            return word
    return "missing"


def _tamper_reasons(a, *, report_dir, canaries, rcs) -> list[str]:
    """The FR-016 checks (plus exit-status consistency); an empty list means none fired."""
    reasons = []
    main = os.path.join(report_dir, "records.jsonl")
    collected, _order, _reports, _errors, finished = _read_records(main)
    finals = _final_outcomes(main)
    c_collected, _o, _r, _e, c_finished = _read_records(os.path.join(report_dir,
                                                                     "collect.jsonl"))
    if finished is None:
        reasons.append("main pass recorded no session finish")
    if rcs.get("collect") != 0 or c_finished is None:
        reasons.append("collect-only pass did not finish cleanly")
    expected = len(c_collected)
    any_failed = any(o == "failed" for o in finals.values())
    if len(finals) != expected and not (
        _stops_early(a.pytest_arg) and any_failed and len(finals) < expected
    ):
        reasons.append(f"{len(finals)} results recorded, {expected} collected")
    narrowed = _narrows_selection(a.pytest_arg)
    canary_ids = {n for n in finals for c in canaries if n.endswith("::" + c["name"])}
    stopped = _stops_early(a.pytest_arg) and any(
        o == "failed" for n, o in finals.items() if n not in canary_ids)
    for c in canaries:
        nodeids = [n for n in finals if n.endswith("::" + c["name"])]
        outcome = finals[nodeids[0]] if nodeids else "missing"
        c["outcome"] = outcome
        in_collect = any(n.endswith("::" + c["name"]) for n in c_collected)
        if (not in_collect and narrowed) or (stopped and outcome == "missing"):
            continue
        if c["expect"] == "failed" and outcome in ("passed", "missing"):
            reasons.append("failing canary " + outcome)
        if c["expect"] == "passed" and outcome in ("failed", "missing"):
            reasons.append("passing canary " + outcome)
    if rcs.get("main") == 0 and any_failed:
        reasons.append("pytest exit status 0 with failures recorded")
    junit = _junit_outcomes(os.path.join(report_dir, "junit.xml"))
    if junit is None:
        reasons.append("junit file missing or unreadable")
    else:
        # One outcome per test: junit may list a test twice (a failure, then a teardown error).
        mine: dict = {}
        for nodeid, outcome in finals.items():
            mine.setdefault(_junit_key(nodeid), set()).add(outcome)
        theirs: dict = {}
        for key, outcome in junit:
            if outcome == "skipped" and key not in mine:
                continue  # a collection-level skip: junit lists it, the plugin does not
            theirs.setdefault(key, set()).add(outcome)
        if {k: _collapse(v) for k, v in mine.items()} != {k: _collapse(v) for k, v in theirs.items()}:
            reasons.append("junit and records disagree")
    return reasons


def _judge(a, *, report_dir, canaries, rcs, timed_out, id_map, meta) -> dict:
    records = os.path.join(report_dir, "records.jsonl")
    tests_all, _has, collect_errors, _fin = _build_tests(records, id_map, timed_out)
    names = {c["name"] for c in canaries}
    files = {c["file"] for c in canaries}

    def is_canary(t):
        return t["key"] in names and any(f in t["nodeid"] for f in files)

    tests = [t for t in tests_all if not is_canary(t)]
    finals = _final_outcomes(records)
    for c in canaries:
        hits = [n for n in finals if n.endswith("::" + c["name"])]
        c["outcome"] = finals[hits[0]] if hits else "missing"
    isolation = "partial"
    for rec in _iter_records(records):
        if rec.get("type") == "isolation":
            mods = rec.get("work_modules")
            isolation = "partial" if mods else "full"
            meta["work_modules"] = mods if isinstance(mods, list) else None
    meta["isolation"] = isolation
    rc = rcs.get("main")
    _c, _o, reports, _e, _f = _read_records(records)
    has_reports = bool(set(reports) - {t["nodeid"] for t in tests_all if is_canary(t)})
    if timed_out:
        status = "timeout"
    elif rc is None:
        raise HeldoutError("E_SANDBOX_UNAVAILABLE")
    elif collect_errors or rc in (2, 3, 4, 5) or not has_reports:
        status = "collection_error"
    else:
        reasons = _tamper_reasons(a, report_dir=report_dir, canaries=canaries, rcs=rcs)
        meta["tamper_reasons"] = reasons
        if reasons:
            status = "tampered"
        elif rc in (0, 1) and not any(t["outcome"] == "failed" for t in tests):
            status = "passed"
        else:
            status = "failed"
    report_canaries = [{"name": c["name"], "file": c["file"], "expect": c["expect"],
                        "outcome": c["outcome"]} for c in canaries]
    return {"tests": tests, "canaries": report_canaries, "status": status, "rc": rc}


def _iter_records(path: str):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


# ---------------------------------------------------------------------------------------------
# run


def _cmd_run(a, out) -> int:
    _check_names(a.repo_name, a.feature)
    if bool(a.worktree) == bool(a.repo) or bool(a.repo) != bool(a.ref):
        raise HeldoutError("E_USAGE")
    if a.sync_cmd is not None and not _split_cmd(a.sync_cmd):
        raise HeldoutError("E_USAGE")
    if a.sandbox == "docker" and _selects_docker_marker(a.pytest_arg):
        raise HeldoutError("E_SANDBOX_DOCKER")
    profile = PROFILES[a.profile]
    root = _heldout_root(a.heldout_root)
    suite = os.path.abspath(a.suite) if a.suite else os.path.join(root, a.repo_name, a.feature)
    if not os.path.isdir(suite):
        raise HeldoutError("E_SUITE_MISSING")

    if a.worktree:
        tree = os.path.abspath(a.worktree)
        if not os.path.isdir(tree):
            raise HeldoutError("E_USAGE")
        repo_for_wt = tree
    else:
        tree = os.path.abspath(a.repo)
        if not os.path.isdir(tree):
            raise HeldoutError("E_GIT")
        repo_for_wt = tree
    report_root = _report_root(root, a.repo_name, a.feature)
    explicit_report = os.path.abspath(a.report_dir) if a.report_dir else None
    bases = _forbidden_bases(tree, repo_for_wt, a.forbid_under, strict_git=bool(a.repo))
    _check_placement(bases, suite=suite, report_dir=explicit_report or report_root, root=root)
    _check_perms(suite, root)
    if a.worktree and _inside(_runner_file(), tree):
        raise HeldoutError("E_RUNNER_IN_TREE")

    scratch = os.path.abspath(a.scratch or os.environ.get("TMPDIR") or tempfile.gettempdir())
    if not os.path.isdir(scratch):
        raise HeldoutError("E_USAGE")
    for base in (tree, suite, root):
        if _inside(scratch, base):
            raise HeldoutError("E_USAGE")
    suite_rel = a.suite_dest or os.path.join("tests", "heldout", a.feature)
    norm = os.path.normpath(suite_rel)
    if os.path.isabs(suite_rel) or norm in (".", "") or norm.split(os.sep)[0] == "..":
        raise HeldoutError("E_USAGE")

    if a.map_file:
        id_map, map_source = load_id_map(a.map_file), "--map"
    elif os.path.isfile(os.path.join(suite, "SPEC-MAP.json")):
        id_map, map_source = load_id_map(os.path.join(suite, "SPEC-MAP.json")), "SPEC-MAP.json"
    elif os.path.isfile(os.path.join(suite, "fr_report.py")):
        id_map, map_source = load_id_map(os.path.join(suite, "fr_report.py")), "fr_report.py"
    else:
        id_map, map_source = {}, None

    commit = _resolve_commit(tree, a.ref) if a.repo else None

    _mkdir_private(report_root)
    try:
        lock = _Lock(report_root).__enter__()
    except HeldoutError:
        _emit_feedback(_infra_feedback(a.feature, a.cap), as_json=a.json,
                       feedback_file=a.feedback_file, out=out)
        raise
    try:
        return _run_locked(a, out, profile=profile, root=root, suite=suite, tree=tree,
                           commit=commit, report_root=report_root, explicit_report=explicit_report,
                           scratch=scratch, suite_rel=norm, id_map=id_map, map_source=map_source)
    finally:
        lock.__exit__(None, None, None)


def _run_locked(a, out, *, profile, root, suite, tree, commit, report_root, explicit_report,
                scratch, suite_rel, id_map, map_source) -> int:
    rounds_path = os.path.join(report_root, "rounds.json")
    counted = not a.no_count
    state_doc = _load_rounds(rounds_path, a.cap)
    if counted and len(state_doc["rounds"]) >= a.cap:
        raise HeldoutError("E_ROUND_CAP")
    round_n = len(state_doc["rounds"]) + 1 if counted else None

    if explicit_report:
        report_dir = explicit_report
    elif counted:
        report_dir = os.path.join(report_root, f"round-{round_n:02d}")
        if os.path.lexists(report_dir):
            stale = f"stale-{_run_stamp()[4:]}-round-{round_n:02d}"
            os.replace(report_dir, os.path.join(report_root, stale))
    else:
        report_dir = _fresh_run_dir(report_root)
    _mkdir_private(report_dir)

    started = _utcnow()
    t0 = time.monotonic()
    tree_info = (
        {"mode": "repo", "repo": tree, "ref": a.ref, "commit": commit}
        if a.repo
        else {"mode": "worktree", "path": tree}
    )
    meta = {
        "runner_version": VERSION,
        "runner_sha256": _runner_sha256(),
        "repo": a.repo_name,
        "feature": a.feature,
        "round": round_n,
        "round_cap": a.cap,
        "counted": counted,
        "profile": a.profile,
        "tree": tree_info,
        "commit": commit,
        "suite": suite,
        "suite_sha256": _tree_sha256(suite),
        "map_source": map_source,
        "pytest_command": None,
        "started_at": _iso(started),
        "duration_s": None,
        "status": None,
        "pytest_exit_code": None,
        "error": None,
        "sandbox": a.sandbox,
        "image": None,
        "isolation": None,
    }
    tests: list[dict] = []
    canaries: list[dict] = []
    status = "infra_error"
    error_code = None
    base = copy = None
    state = _State()
    old_handlers = {}

    def _on_signal(signum, frame):
        raise _Interrupted(signum)

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                old_handlers[sig] = signal.signal(sig, _on_signal)
            except ValueError:  # not the main thread
                pass
        base = tempfile.mkdtemp(prefix="heldout-run-", dir=scratch)
        if a.sandbox == "docker":
            # Checked before anything is copied (FR-003).
            state.docker = _Docker.connect(os.path.join(report_dir, "sandbox.log"))
        copy = tempfile.mkdtemp(prefix="heldout-copy-", dir=scratch)
        plugin_dir = os.path.join(base, "plugin")
        work = os.path.join(base, "work")
        child_tmp = os.path.join(base, "tmp")
        for d in (plugin_dir, work, child_tmp):
            os.mkdir(d, 0o700)
        records = os.path.join(base, "records.jsonl")
        if a.keep:
            meta["kept_copy"] = copy

        try:
            if a.repo:
                _extract_archive(tree, commit, copy)
            else:
                _copy_filtered(tree, copy)
            _rmtree(os.path.join(copy, ".git"))
            if a.sandbox != "docker":
                # Unsandboxed (007 FR-009): the suite sits inside the copy. Sandboxed (008
                # FR-006): it never does; it goes to the judge-only /suite.
                dest = os.path.join(copy, suite_rel)
                if os.path.lexists(dest):
                    if os.path.isdir(dest) and not os.path.islink(dest):
                        _rmtree(dest)
                    else:
                        os.unlink(dest)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                _copy_filtered(suite, dest)
            with open(os.path.join(plugin_dir, "heldout_run.py"), "wb") as fh:
                fh.write(_runner_bytes())
        except HeldoutError:
            raise
        except OSError:
            raise HeldoutError("E_SYNC") from None

        if a.sandbox == "docker":
            res = _run_sandboxed(a, state.docker, state=state, profile_name=a.profile,
                                 copy=copy, suite=suite, plugin_dir=plugin_dir,
                                 report_dir=report_dir, round_n=round_n, meta=meta,
                                 id_map=id_map)
            tests, canaries, status = res["tests"], res["canaries"], res["status"]
            meta["pytest_exit_code"] = res["rc"]
        else:
            env = dict(os.environ)
            for name in a.unset:
                env.pop(name, None)
            env.pop(IMPL_ENV, None)  # set only in sandboxed runs (008 FR-012)
            inherited = env.get("PYTHONPATH")
            pp = _expand(profile["pythonpath"], copy) + [plugin_dir]
            pp += [inherited] if inherited else []
            env["PYTHONPATH"] = os.pathsep.join(pp)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env[RECORDS_ENV] = records
            env[ROUND_ENV] = "-" if round_n is None else str(round_n)
            env["TMPDIR"] = child_tmp
            cwd = work if profile["cwd"] == "@scratch" else profile["cwd"].replace("{copy}", copy)

            sync_cmd = _split_cmd(a.sync_cmd) if a.sync_cmd is not None else profile["sync"]
            if sync_cmd and not a.no_sync:
                sync_log = os.path.join(report_dir, "sync.log")
                with open(sync_log, "wb") as log:
                    os.chmod(sync_log, 0o600)
                    try:
                        rc, to = _run_group(_expand(sync_cmd, copy), cwd=work, env=env,
                                            log_fh=log, timeout=a.sync_timeout, state=state)
                    except OSError:
                        rc, to = None, False
                if to or rc != 0:
                    raise HeldoutError("E_SYNC")

            launcher = [
                sys.executable if s == "@python" else s for s in _expand(profile["launcher"], copy)
            ]
            junit = os.path.join(report_dir, "junit.xml")
            cmd = launcher + [
                dest, "-q", "-p", "no:cacheprovider", "-p", "heldout_run", f"--junitxml={junit}",
            ] + list(a.pytest_arg)
            meta["pytest_command"] = cmd
            timeout = a.timeout if a.timeout else profile["timeout"]
            pytest_log = os.path.join(report_dir, "pytest.log")
            with open(pytest_log, "wb") as log:
                os.chmod(pytest_log, 0o600)
                try:
                    rc, timed_out = _run_group(cmd, cwd=cwd, env=env, log_fh=log,
                                               timeout=timeout, state=state)
                except OSError:
                    raise HeldoutError("E_SYNC") from None
            meta["pytest_exit_code"] = rc
            tests, has_reports, collect_errors, _finished = _build_tests(records, id_map,
                                                                         timed_out)
            status = _decide_status(timed_out=timed_out, rc=rc, has_reports=has_reports,
                                    collect_errors=collect_errors, tests=tests)
    except HeldoutError as exc:
        status, error_code = "infra_error", exc.code
    except _Interrupted:
        status, error_code = "infra_error", "E_SYNC"
        meta["error"] = "interrupted"
    except Exception as exc:  # noqa: BLE001 - any runner-side failure is infra_error
        status, error_code = "infra_error", "E_SYNC"
        meta["error"] = type(exc).__name__
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, signal.SIG_IGN)
            except ValueError:
                pass
        try:
            if state.proc is not None and state.proc.poll() is None:
                _killpg(state.proc.pid, signal.SIGKILL)
            if state.docker is not None:
                state.docker.cleanup()
            _rmtree(base)
            if not a.keep:
                _rmtree(copy)
        finally:
            for sig, handler in old_handlers.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, TypeError):
                    pass

    use_round = round_n if status in COUNTED_STATUSES else None
    fb = summarize(tests, feature=a.feature, round=use_round, cap=a.cap, status=status)
    meta["status"] = status
    meta["duration_s"] = round(time.monotonic() - t0, 3)
    if error_code:
        meta["error_code"] = error_code
    if status not in COUNTED_STATUSES:
        meta["round"] = None

    try:
        if not os.path.exists(os.path.join(report_dir, "junit.xml")):
            _write_private(os.path.join(report_dir, "junit.xml"), "<testsuites />\n")
        else:
            os.chmod(os.path.join(report_dir, "junit.xml"), 0o600)
        if not os.path.exists(os.path.join(report_dir, "pytest.log")):
            _write_private(os.path.join(report_dir, "pytest.log"), "")
        _write_json_atomic(os.path.join(report_dir, "report.json"),
                           {"schema": REPORT_SCHEMA, "meta": meta, "tests": tests,
                            "canaries": canaries})
        _write_json_atomic(os.path.join(report_dir, "feedback.json"), _sanitize_feedback(fb))
        if counted and status in COUNTED_STATUSES:
            state_doc["cap"] = a.cap
            state_doc["rounds"].append(
                {
                    "n": round_n,
                    "started_at": meta["started_at"],
                    "status": status,
                    "failing_ids": list(fb.get("failing") or {}),
                    "tree": tree_info,
                }
            )
            _write_json_atomic(os.path.join(report_root, "rounds.json"), state_doc)
        elif counted and not explicit_report:
            os.replace(report_dir, _fresh_run_dir(report_root))
    except OSError:
        status = "infra_error"
        error_code = error_code or "E_SYNC"
        fb = summarize(tests, feature=a.feature, round=None, cap=a.cap, status=status)

    _emit_feedback(fb, as_json=a.json, feedback_file=a.feedback_file, out=out)
    if error_code:
        raise HeldoutError(error_code)
    return _EXIT_OF_STATUS[status]


# ---------------------------------------------------------------------------------------------
# rounds / triage / check-isolation


def _cmd_rounds(a, out) -> int:
    _check_names(a.repo_name, a.feature)
    if bool(a.reset) != bool(a.reason) or (a.reason is not None and not a.reason.strip()):
        raise HeldoutError("E_USAGE")
    root = _heldout_root(a.heldout_root)
    report_root = _report_root(root, a.repo_name, a.feature)
    path = os.path.join(report_root, "rounds.json")
    if not a.reset:
        doc = _load_rounds(path, DEFAULT_CAP)
        for r in doc["rounds"]:
            ids = [i for i in r.get("failing_ids") or [] if _safe_label(i)]
            st = r.get("status") if r.get("status") in STATUSES else "infra_error"
            out.write(" ".join([f"round {int(r.get('n', 0))}: {st}", *ids]) + "\n")
        return 0
    _mkdir_private(report_root)
    with _Lock(report_root):
        doc = _load_rounds(path, DEFAULT_CAP)
        now = _utcnow()
        moved = doc["rounds"]
        doc["resets"].append({"at": _iso(now), "reason": a.reason, "rounds": moved})
        doc["rounds"] = []
        _write_json_atomic(path, doc)
        archive = os.path.join(report_root, now.strftime("reset-%Y%m%dT%H%M%S%fZ"))
        for name in sorted(os.listdir(report_root)):
            if re.fullmatch(r"round-[0-9]+", name):
                _mkdir_private(archive)
                os.replace(os.path.join(report_root, name), os.path.join(archive, name))
    out.write(f"heldout-run: {a.feature} rounds reset ({len(moved)} moved)\n")
    return 0


def _assertion_text(longrepr: str) -> str:
    lines = (longrepr or "").splitlines()
    e_lines = [ln for ln in lines if ln.startswith("E ")]
    text = "\n".join(e_lines if e_lines else lines[-20:])
    return text[:4000]


def _latest_report_dir(report_root: str) -> str | None:
    best = None
    if not os.path.isdir(report_root):
        return None
    for name in os.listdir(report_root):
        if not re.fullmatch(r"round-[0-9]+|run-[0-9]{8}T[0-9]{6}Z", name):
            continue
        path = os.path.join(report_root, name)
        rj = os.path.join(path, "report.json")
        if not os.path.isfile(rj):
            continue
        try:
            with open(rj, encoding="utf-8") as fh:
                started = json.load(fh)["meta"]["started_at"] or ""
        except (OSError, ValueError, KeyError, TypeError):
            started = ""
        key = (started, os.stat(rj).st_mtime)
        if best is None or key > best[0]:
            best = (key, path)
    return best[1] if best else None


def _cmd_triage(a, out) -> int:
    _check_names(a.repo_name, a.feature)
    root = _heldout_root(a.heldout_root)
    report_root = _report_root(root, a.repo_name, a.feature)
    if a.round:
        report_dir = os.path.join(report_root, f"round-{a.round:02d}")
    else:
        report_dir = _latest_report_dir(report_root)
    try:
        with open(os.path.join(report_dir or "", "report.json"), encoding="utf-8") as fh:
            report = json.load(fh)
        with open(a.spec, encoding="utf-8") as fh:
            spec_text = fh.read()
    except (OSError, ValueError):
        raise HeldoutError("E_USAGE") from None
    wanted = list(dict.fromkeys(a.ids))
    entries = []
    for t in report.get("tests", []):
        if t.get("outcome") != "failed":
            continue
        ids = [str(i) for i in t.get("ids") or []]
        tags = ids + sorted(_labels(ids) - set(ids))
        if wanted:
            hits = [w for w in wanted if w in tags]
            if not hits:
                continue
            head = hits[0]
        else:
            head = tags[0]
        spec_parts = []
        for i in ids or [UNMAPPED]:
            text = extract_requirement(spec_text, i) if _is_id(i) else None
            spec_parts.append(text if text is not None else "(not found in spec)")
        entries.append(
            {
                "id": head,
                "nodeid": t.get("nodeid"),
                "assertion": _assertion_text(t.get("longrepr") or ""),
                "spec": "\n".join(spec_parts),
            }
        )
    if a.json:
        # heldout-triage.v1: exactly one JSON object; no banner line in JSON mode.
        out.write(json.dumps({"schema": TRIAGE_SCHEMA, "entries": entries}, indent=2) + "\n")
        return 0
    lines = ["PRIVATE TRIAGE: not for the implementer"]
    for e in entries:
        lines.append(f"== {e['id']}: {e['nodeid']}")
        lines.append("assertion:")
        lines.append(e["assertion"])
        lines.append("spec:")
        lines.append(e["spec"])
    out.write("\n".join(lines) + "\n")
    return 0


def _cmd_check_isolation(a, out) -> int:
    root = _heldout_root(a.heldout_root)
    suite = os.path.abspath(a.suite)
    if not os.path.isdir(suite):
        raise HeldoutError("E_SUITE_MISSING")
    repo = os.path.abspath(a.repo) if a.repo else None
    bases = _forbidden_bases(repo, repo, a.forbid_under, strict_git=False)
    _check_placement(bases, suite=suite, report_dir=None, root=root)
    _check_perms(suite, root)
    out.write("heldout-run: isolated\n")
    return 0


# ---------------------------------------------------------------------------------------------
# entry point


def main(argv: list[str] | None = None) -> int:
    """The CLI. Returns the exit code; never raises for refusals."""
    out, err = sys.stdout, sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            try:
                args = _build_parser().parse_args(_preprocess(argv))
            except SystemExit as exc:  # --help
                return int(exc.code or 0)
            if args.version:
                if args.command:
                    raise HeldoutError("E_USAGE")
                out.write(f"heldout-run {VERSION} sha256:{_runner_sha256()}\n")
                return 0
            if args.command == "run":
                return _cmd_run(args, out)
            if args.command == "triage":
                return _cmd_triage(args, out)
            if args.command == "rounds":
                return _cmd_rounds(args, out)
            if args.command == "check-isolation":
                return _cmd_check_isolation(args, out)
            raise HeldoutError("E_USAGE")
        except HeldoutError as exc:
            err.write(f"heldout-run: error {exc.code}\n")
            err.flush()
            return exc.exit_code
        except KeyboardInterrupt:
            err.write("heldout-run: error E_SYNC\n")
            return 3
        except Exception:  # noqa: BLE001 - never print a traceback (it carries paths)
            err.write("heldout-run: error E_SYNC\n")
            return 3


# ---------------------------------------------------------------------------------------------
# pytest plugin (loaded in the child with ``-p heldout_run``); no pytest import needed


def _emit_record(rec: dict) -> None:
    path = os.environ.get(RECORDS_ENV)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def _flatten_marks(args) -> list[str]:
    out: list[str] = []
    for value in args:
        if isinstance(value, (list, tuple, set, frozenset)):
            out.extend(_flatten_marks(value))
        else:
            out.append(str(value))
    return out


def pytest_configure(config):
    config.addinivalue_line("markers", "fr(*ids): requirement ids this held-out test proves")


_CANARY_FILE_RX = re.compile(r"test_[0-9a-f]{24}\.py")


def pytest_collect_file(file_path, parent):
    """Sandboxed runs only: collect a root canary file the suite's ``python_files`` would skip."""
    if not (os.environ.get(IMPL_ENV) and os.environ.get(RECORDS_ENV)):
        return None
    if str(file_path.parent) != "/suite" or not _CANARY_FILE_RX.fullmatch(file_path.name):
        return None
    patterns = parent.config.getini("python_files") or []
    if any(fnmatch.fnmatch(file_path.name, pat) for pat in patterns):
        return None  # pytest's own collector takes it
    return sys.modules["pytest"].Module.from_parent(parent, path=file_path)


def pytest_collection_modifyitems(session, config, items):
    """Sandboxed runs only: run the CANARY tests last, in file-name order (spec 008 FR-014)."""
    if not (os.environ.get(IMPL_ENV) and os.environ.get(RECORDS_ENV)):
        return

    def is_canary(item):
        return any(CANARY_LABEL in _flatten_marks(m.args) for m in item.iter_markers("fr"))

    canaries = sorted((i for i in items if is_canary(i)), key=lambda i: i.nodeid)
    if canaries:
        items[:] = [i for i in items if not is_canary(i)] + canaries


def pytest_collection_finish(session):
    if not os.environ.get(RECORDS_ENV):
        return
    for item in session.items:
        marks: list[str] = []
        for mark in item.iter_markers("fr"):
            marks.extend(_flatten_marks(mark.args))
        key = getattr(item, "originalname", None) or item.name.split("[", 1)[0]
        _emit_record({"type": "collected", "nodeid": item.nodeid, "key": key, "marks": marks})


def pytest_collectreport(report):
    if report.failed and os.environ.get(RECORDS_ENV):
        _emit_record(
            {
                "type": "collect_error",
                "nodeid": report.nodeid,
                "longrepr": str(report.longrepr)[:100000],
            }
        )


def pytest_runtest_logreport(report):
    if not os.environ.get(RECORDS_ENV):
        return
    _emit_record(
        {
            "type": "report",
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "wasxfail": hasattr(report, "wasxfail"),
            "duration": getattr(report, "duration", 0.0),
            "longrepr": str(report.longrepr)[:100000] if report.longrepr else "",
        }
    )


def _work_modules() -> list[str]:
    found = []
    for name, mod in list(sys.modules.items()):
        path = getattr(mod, "__file__", None)
        if not isinstance(path, str):
            spec = getattr(mod, "__spec__", None)
            path = getattr(spec, "origin", None)
        if isinstance(path, str) and (path == "/work" or path.startswith("/work/")):
            found.append(name)
    return sorted(found)[:200]


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get(RECORDS_ENV):
        if os.environ.get(IMPL_ENV):  # sandboxed run (008 FR-013); computed in this process
            _emit_record({"type": "isolation", "work_modules": _work_modules()})
        _emit_record({"type": "finish", "exitstatus": int(exitstatus)})


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
