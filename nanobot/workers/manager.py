"""WorkerManager: start, resume, watch and stop coding-agent sessions (spec 015 phase 5).

Design rules (all mechanical, no model in the loop):
- One kernel job per worker run (role ``worker``), linked as a child of the owner's chat job.
  Its state is driven only by observed facts: events in the worker's own output log, process
  liveness, exit status and timeouts.
- Backend is explicit and never falls back. A disabled, missing or out-of-usage backend fails
  the job with a named cause.
- Workers are detached (own session, output to a file) so a gateway restart does not kill them.
  ``reconcile()`` re-attaches live ones and settles the rest from their log.
- Results are reports, not proof: the stored text says what the worker reported.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanobot.kernel.jobs import FailureSide, Job, JobState, JobStore
from nanobot.workers.drivers import DRIVERS, Driver, WorkerEvent, split_status, wrap_request
from nanobot.workers.sandbox import worker_bwrap_argv

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    job_id TEXT PRIMARY KEY,
    owner_job TEXT NOT NULL DEFAULT '',
    backend TEXT NOT NULL,
    workspace TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    pid INTEGER NOT NULL DEFAULT 0,
    pid_start TEXT NOT NULL DEFAULT '',
    log_path TEXT NOT NULL,
    offset INTEGER NOT NULL DEFAULT 0,
    observed_state TEXT NOT NULL,
    started REAL NOT NULL,
    last_event REAL NOT NULL,
    last_update TEXT NOT NULL DEFAULT '',
    last_message TEXT NOT NULL DEFAULT '',
    result_text TEXT NOT NULL DEFAULT '',
    got_result INTEGER NOT NULL DEFAULT 0,
    result_error TEXT NOT NULL DEFAULT '',
    denials TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    runs INTEGER NOT NULL DEFAULT 1,
    idle_warned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS worker_usage (
    ts REAL NOT NULL, backend TEXT NOT NULL, job_id TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
);
"""
_RUNNING = ("starting", "running")


class WorkerError(Exception):
    """A refusal with a message that is safe to show the model and the owner."""

    def __init__(self, cause: str, message: str) -> None:
        super().__init__(message)
        self.cause = cause


@dataclass(slots=True)
class WorkerRow:
    job_id: str
    owner_job: str
    backend: str
    workspace: str
    session_id: str
    pid: int
    pid_start: str
    log_path: str
    offset: int
    observed_state: str
    started: float
    last_event: float
    last_update: str
    last_message: str
    result_text: str
    got_result: bool
    result_error: str
    denials: str
    exit_code: int | None
    runs: int
    idle_warned: bool


def _proc_start(pid: int) -> str:
    """Start time field of /proc/<pid>/stat: identifies one process even if the pid is reused."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def _alive(pid: int, start: str) -> bool:
    if pid <= 0 or not start:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat.rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return False
    return fields[0] != "Z" and fields[19] == start


class WorkerManager:
    def __init__(
        self,
        store: JobStore,
        state_dir: Path,
        config: Any,  # WorkersToolConfig
        *,
        allowed_roots: Callable[[], list[Path]],
        protected_roots: Callable[[], list[Path]] = lambda: [],
        env: dict[str, str] | None = None,
        home: Path | None = None,
        poll_s: float = 0.5,
        drivers: dict[str, Driver] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.dir = Path(state_dir) / "workers"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._allowed_roots = allowed_roots
        self._protected_roots = protected_roots
        self._env = env if env is not None else dict(os.environ)
        self._home = home or Path(self._env.get("HOME") or Path.home())
        self._poll_s = poll_s
        self._drivers = drivers or DRIVERS
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.dir / "workers.sqlite3"), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # -- storage --------------------------------------------------------------------

    def _row(self, job_id: str) -> WorkerRow | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM workers WHERE job_id=?", (job_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["got_result"], d["idle_warned"] = bool(d["got_result"]), bool(d["idle_warned"])
        return WorkerRow(**d)

    def _set(self, job_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE workers SET {cols} WHERE job_id=?", [*fields.values(), job_id])
            self._db.commit()

    def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        with self._lock:
            self._db.close()

    # -- config helpers -------------------------------------------------------------

    def backend_config(self, backend: str) -> Any:
        cfg = getattr(self.config, backend, None)
        if backend not in self._drivers or cfg is None:
            raise WorkerError("unknown_backend", f"Unknown backend {backend!r}. Use claude or codex.")
        return cfg

    def enabled_backends(self) -> list[str]:
        return [b for b in self._drivers if getattr(getattr(self.config, b, None), "enable", False)]

    def _binary(self, backend: str, cfg: Any) -> str:
        if cfg.binary:
            return cfg.binary
        found = shutil.which(backend, path=self._env.get("PATH"))
        if found:
            return found
        local = self._home / ".local" / "bin" / backend
        if local.exists():
            return str(local)
        raise WorkerError("binary_missing", f"The {backend} binary was not found.")

    def _login_dir(self, backend: str, cfg: Any) -> Path:
        return Path(cfg.home) if cfg.home else self._home / (".claude" if backend == "claude" else ".codex")

    def _check_workspace(self, workspace: str) -> Path:
        ws = Path(workspace).expanduser().resolve()
        if not ws.is_dir():
            raise WorkerError("workspace_missing", f"{ws} is not a directory.")
        roots = [r.resolve() for r in self._allowed_roots()]
        if not any(ws == r or r in ws.parents for r in roots):
            raise WorkerError(
                "workspace_not_granted",
                f"{ws} is outside the granted write roots (tools.file.writeRoots).",
            )
        for bad in self._protected_roots():
            bad = bad.resolve()
            if ws == bad or bad in ws.parents or ws in bad.parents:
                raise WorkerError("workspace_protected", f"{ws} overlaps moeka's own files.")
        return ws

    # -- start / follow up ----------------------------------------------------------

    def _fail_job(self, job: Job, cause: str, detail: str) -> None:
        self.store.transition(job.id, JobState.FAILED, side=FailureSide.WORKER, cause=cause,
                              detail=detail)

    async def start(
        self, *, backend: str, workspace: str, request: str, context: str = "",
        session_key: str = "", route: tuple[str, str, str] | None = None,
    ) -> Job:
        job = self.store.open_job(
            session_key=session_key or "worker", request_text=request, role="worker", route=route,
        )
        try:
            cfg = self.backend_config(backend)
            if not cfg.enable:
                raise WorkerError("backend_disabled", f"{backend} workers are disabled in config.")
            ws = self._check_workspace(workspace)
            self._check_capacity(backend, cfg, ws)
            binary = self._binary(backend, cfg)
        except WorkerError as exc:
            self._fail_job(job, f"{backend}:{exc.cause}", str(exc))
            raise
        if session_key:
            parent = next((j for j in reversed(self.store.active(session_key=session_key))
                           if j.role == ""), None)
            if parent is not None:
                self.store.link_child(parent.id, job.id, "worker")
                self.store.link_ref(job.id, "parent_job", parent.id)
        self.store.link_ref(job.id, "backend", backend)
        self.store.link_ref(job.id, "workspace", str(ws))
        now = time.time()
        log = self.dir / f"{job.id}.log"
        with self._lock:
            self._db.execute(
                "INSERT INTO workers(job_id, backend, workspace, log_path, observed_state,"
                " started, last_event) VALUES (?,?,?,?,?,?,?)",
                (job.id, backend, str(ws), str(log), "starting", now, now),
            )
            self._db.commit()
        await self._launch(job.id, wrap_request(request, context), None, binary=binary)
        return job

    async def follow_up(self, job_id: str, message: str) -> Job:
        row = self._row(job_id)
        job = self.store.get(job_id)
        if row is None or job is None:
            raise WorkerError("unknown_job", f"No worker job {job_id!r}.")
        if row.observed_state in _RUNNING:
            raise WorkerError("still_running", "That worker is still running; wait for it or stop it.")
        if not row.session_id:
            raise WorkerError("no_session", "That worker never reported a session id, so it cannot be resumed.")
        cfg = self.backend_config(row.backend)
        if not cfg.enable:
            raise WorkerError("backend_disabled", f"{row.backend} workers are disabled in config.")
        self._check_capacity(row.backend, cfg, Path(row.workspace), exclude=job_id)
        binary = self._binary(row.backend, cfg)
        self._set(job_id, got_result=0, result_error="", result_text="", last_message="",
                  denials="", exit_code=None, runs=row.runs + 1, idle_warned=0,
                  observed_state="starting", last_event=time.time())
        self.store.transition(job_id, JobState.WORKING, detail=f"follow-up #{row.runs}: {message[:200]}")
        await self._launch(job_id, message, row.session_id, binary=binary)
        return self.store.get(job_id)

    def _check_capacity(self, backend: str, cfg: Any, ws: Path, exclude: str = "") -> None:
        with self._lock:
            rows = self._db.execute(
                "SELECT job_id, backend, workspace FROM workers WHERE observed_state IN (?,?)",
                _RUNNING,
            ).fetchall()
        rows = [r for r in rows if r["job_id"] != exclude]
        if sum(1 for r in rows if r["backend"] == backend) >= cfg.max_concurrent:
            raise WorkerError("at_capacity", f"{backend} already has {cfg.max_concurrent} running.")
        if any(r["workspace"] == str(ws) for r in rows):
            raise WorkerError("workspace_busy", "Another worker is already writing in that workspace.")

    async def _launch(self, job_id: str, prompt: str, session_id: str | None, *, binary: str) -> None:
        row = self._row(job_id)
        assert row is not None
        cfg = self.backend_config(row.backend)
        driver = self._drivers[row.backend]
        argv = driver.argv(binary, prompt, session_id, list(cfg.extra_args),
                           list(getattr(self.config, "claude_disallowed_tools", []))
                           if row.backend == "claude" else [])
        ws = Path(row.workspace)
        login = self._login_dir(row.backend, cfg)
        env = {"PATH": self._env.get("PATH", "/usr/bin:/bin"), "HOME": str(self._home),
               "LANG": self._env.get("LANG", "C.UTF-8"), "TERM": "dumb"}
        if row.backend == "claude" and cfg.home:
            env["CLAUDE_CONFIG_DIR"] = str(login)
        if row.backend == "codex" and cfg.home:
            env["CODEX_HOME"] = str(login)
        if self.config.sandbox:
            if shutil.which("bwrap", path=self._env.get("PATH")) is None:
                self._fail_job(self.store.get(job_id), f"{row.backend}:sandbox_unavailable",
                               "bwrap is not installed; refusing to run a worker unsandboxed.")
                self._set(job_id, observed_state="failed")
                raise WorkerError("sandbox_unavailable", "bwrap is not installed.")
            rw = [self._home / ".claude.json"] if row.backend == "claude" and not cfg.home else []
            ro = [self._home / ".local"]
            argv = worker_bwrap_argv(argv, workspace=ws, home=login, state_home=self._home,
                                     ro_binds=ro, rw_binds=rw, env=env)
        if shutil.which("setpriv", path=self._env.get("PATH")):
            argv = ["setpriv", "--no-new-privs", *argv]
        log = Path(row.log_path)
        out = open(log, "ab")
        err = open(self.dir / f"{job_id}.err", "wb")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=err,
                cwd=str(ws), env=env, start_new_session=True,
            )
        except OSError as exc:
            self._fail_job(self.store.get(job_id), f"{row.backend}:spawn_failed", str(exc))
            self._set(job_id, observed_state="failed")
            raise WorkerError("spawn_failed", str(exc)) from exc
        finally:
            out.close()
            err.close()
        self._procs[job_id] = proc
        self._set(job_id, pid=proc.pid, pid_start=_proc_start(proc.pid), observed_state="running",
                  offset=log.stat().st_size, last_event=time.time())
        self.store.transition(job_id, JobState.WORKING, detail=f"{row.backend} started (pid {proc.pid})")
        self._tasks[job_id] = asyncio.create_task(self._watch(job_id), name=f"worker-{job_id}")

    # -- watching -------------------------------------------------------------------

    def _drain(self, job_id: str) -> None:
        row = self._row(job_id)
        if row is None:
            return
        driver = self._drivers[row.backend]
        try:
            with open(row.log_path, "rb") as fh:
                fh.seek(row.offset)
                data = fh.read()
        except OSError:
            return
        cut = data.rfind(b"\n") + 1
        if cut == 0:
            return
        for raw in data[:cut].decode("utf-8", "replace").splitlines():
            ev = driver.parse(raw)
            if ev is not None:
                self._apply(job_id, ev)
        self._set(job_id, offset=row.offset + cut)

    def _apply(self, job_id: str, ev: WorkerEvent) -> None:
        now = time.time()
        row = self._row(job_id)
        assert row is not None
        fields: dict[str, Any] = {"last_event": now, "idle_warned": 0}
        if ev.kind == "session" and ev.session_id:
            fields["session_id"] = ev.session_id
            self.store.link_ref(job_id, "backend_session", ev.session_id)
        elif ev.kind == "progress" and ev.text:
            fields["last_update"] = ev.text
        elif ev.kind == "message":
            fields["last_message"] = ev.text
            fields["last_update"] = ev.text[:300]
        elif ev.kind == "error":
            fields["result_error"] = ev.text
        elif ev.kind == "result":
            fields["got_result"] = 1
            if ev.session_id:
                fields["session_id"] = ev.session_id
            fields["result_text"] = ev.text
            if ev.is_error:
                fields["result_error"] = ev.cause or "error"
            if ev.permission_denials:
                fields["denials"] = ", ".join(ev.permission_denials)[:300]
            u = ev.usage
            with self._lock:
                self._db.execute(
                    "INSERT INTO worker_usage(ts, backend, job_id, cost_usd, input_tokens,"
                    " output_tokens) VALUES (?,?,?,?,?,?)",
                    (now, row.backend, job_id, float(u.get("cost_usd") or 0),
                     int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)),
                )
                self._db.commit()
        elif ev.kind == "rate_limit":
            fields["last_update"] = f"usage window: {ev.usage}"[:300] if ev.text != "allowed" else row.last_update
        self._set(job_id, **fields)

    def _err_tail(self, job_id: str) -> str:
        try:
            return (self.dir / f"{job_id}.err").read_text(errors="replace").strip()[-300:]
        except OSError:
            return ""

    async def _watch(self, job_id: str) -> None:
        proc = self._procs.get(job_id)
        try:
            while True:
                await asyncio.sleep(self._poll_s)
                self._drain(job_id)
                row = self._row(job_id)
                if row is None or row.observed_state not in _RUNNING:
                    return
                if proc is not None and proc.returncode is not None:
                    self._set(job_id, exit_code=proc.returncode)
                    gone = True
                else:
                    gone = not _alive(row.pid, row.pid_start)
                if gone:
                    await asyncio.sleep(self._poll_s)
                    self._drain(job_id)
                    self._settle(job_id)
                    return
                limit = int(getattr(self.backend_config(row.backend), "timeout_s", 3600))
                if time.time() - row.started > limit:
                    self._kill(row)
                    self._drain(job_id)
                    self._set(job_id, observed_state="timeout")
                    self.store.transition(
                        job_id, JobState.FAILED, side=FailureSide.WORKER,
                        cause=f"{row.backend}:timeout", detail=f"no result within {limit}s; killed",
                    )
                    return
                idle = time.time() - row.last_event
                if idle > self.config.idle_warn_s and not row.idle_warned:
                    self._set(job_id, idle_warned=1)
                    self.store.transition(
                        job_id, JobState.WORKING, side=FailureSide.WORKER, cause="idle",
                        detail=f"no output for {idle / 60:.0f} min",
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker watch failed for {}", job_id)
        finally:
            self._procs.pop(job_id, None)
            self._tasks.pop(job_id, None)

    def _settle(self, job_id: str) -> None:
        """Map the worker's observed outcome to the kernel job state (once)."""
        row = self._row(job_id)
        job = self.store.get(job_id)
        if row is None or job is None or row.observed_state not in _RUNNING:
            return
        b = row.backend.capitalize()
        text = row.result_text or row.last_message
        body, status = split_status(text)
        denied = f" Denied tools: {row.denials}." if row.denials else ""
        if row.result_error or (row.got_result is False and row.exit_code not in (None, 0)):
            msg = body or row.result_error
            low = (msg or "").lower()
            cause = "usage_limit" if ("usage limit" in low or "rate limit" in low or "limit reached" in low) \
                else (row.result_error or f"exit_{row.exit_code}")
            self._set(job_id, observed_state="failed")
            self.store.transition(
                job_id, JobState.FAILED, side=FailureSide.WORKER, cause=f"{row.backend}:{cause}",
                detail=f"{b} reports an error: {(msg or self._err_tail(job_id))[:600]}",
            )
        elif row.got_result:
            self._set(job_id, observed_state="finished")
            if status == "needs_user":
                self.store.transition(job_id, JobState.NEEDS_ANSWER,
                                      detail=f"{b} needs you: {body[:1500]}")
            elif status == "blocked":
                self.store.transition(
                    job_id, JobState.BLOCKED, side=FailureSide.WORKER, cause="worker_blocked",
                    detail=f"{b} reports it is blocked: {body[:1500]}{denied}",
                )
            else:
                note = "" if status else " (it gave no status line)"
                self.store.transition(job_id, JobState.DONE,
                                      detail=f"{b} reports{note}: {body[:1500]}{denied}")
        else:
            self._set(job_id, observed_state="lost")
            self.store.transition(
                job_id, JobState.OUTCOME_UNKNOWN, side=FailureSide.WORKER, cause="worker_vanished",
                detail=f"{b} ended without a result. {self._err_tail(job_id)}".strip(),
            )

    def _kill(self, row: WorkerRow) -> None:
        if row.pid <= 0 or not _alive(row.pid, row.pid_start):
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(row.pid, signal.SIGTERM)
        deadline = time.time() + 3
        while time.time() < deadline and _alive(row.pid, row.pid_start):
            time.sleep(0.1)
        if _alive(row.pid, row.pid_start):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(row.pid, signal.SIGKILL)

    # -- stop / inspect / reconcile -------------------------------------------------

    async def stop(self, job_id: str) -> str:
        row = self._row(job_id)
        if row is None:
            raise WorkerError("unknown_job", f"No worker job {job_id!r}.")
        if row.observed_state not in _RUNNING:
            return f"Worker {job_id} is not running ({row.observed_state})."
        self._kill(row)
        self._set(job_id, observed_state="stopped")
        task = self._tasks.get(job_id)
        if task:
            task.cancel()
        self.store.transition(job_id, JobState.FAILED, side=FailureSide.MOEKA,
                              cause="stopped_by_owner", detail="stopped on request")
        return f"Stopped worker {job_id}."

    def inspect(self, job_id: str = "") -> str:
        if not job_id:
            return self._overview()
        row = self._row(job_id)
        job = self.store.get(job_id)
        if row is None or job is None:
            return f"No worker job {job_id!r}."
        age = time.time() - row.last_event
        alive = _alive(row.pid, row.pid_start)
        lines = [
            f"Worker {job_id}: {row.backend} in {row.workspace}",
            f"Observed: {row.observed_state}; process {'alive' if alive else 'not running'};"
            f" last output {age:.0f}s ago; runs {row.runs}",
            f"Job state: {job.state.value}; session {row.session_id or 'not yet known'}",
        ]
        if row.last_update and row.observed_state in _RUNNING:
            lines.append(f"Latest: {row.last_update}")
        last = self.store.history(job_id)[-1]
        if last.state not in (JobState.WORKING, JobState.HEARD) or last.detail:
            who = f" ({last.side.value}: {last.cause})" if last.side and last.cause else ""
            lines.append(f"Latest record{who}: {last.detail[:1500]}")
        return "\n".join(lines)

    def _overview(self) -> str:
        with self._lock:
            rows = self._db.execute(
                "SELECT job_id, backend, observed_state, workspace FROM workers"
                " ORDER BY started DESC LIMIT 10").fetchall()
            usage = self._db.execute(
                "SELECT backend, COUNT(*) n, SUM(cost_usd) c, SUM(input_tokens) i,"
                " SUM(output_tokens) o FROM worker_usage WHERE ts > ? GROUP BY backend",
                (time.time() - 86400,)).fetchall()
        out = [f"{r['job_id']} {r['backend']} {r['observed_state']} {r['workspace']}" for r in rows]
        out = out or ["No worker jobs yet."]
        out.append("Usage, last 24h (each backend is its own account):")
        for b in self._drivers:
            u = next((x for x in usage if x["backend"] == b), None)
            out.append(f"  {b}: " + (
                f"{u['n']} runs, ${u['c'] or 0:.2f}, {u['i'] or 0} in / {u['o'] or 0} out tokens"
                if u else "none"))
        return "\n".join(out)

    async def reconcile(self) -> int:
        """After a gateway restart: re-attach live workers, settle the rest from their logs."""
        with self._lock:
            ids = [r["job_id"] for r in self._db.execute(
                "SELECT job_id FROM workers WHERE observed_state IN (?,?)", _RUNNING)]
        for job_id in ids:
            row = self._row(job_id)
            self._drain(job_id)
            if row and _alive(row.pid, row.pid_start):
                self._tasks[job_id] = asyncio.create_task(self._watch(job_id), name=f"worker-{job_id}")
            else:
                self._settle(job_id)
        return len(ids)

    async def start_service(self) -> None:
        n = await self.reconcile()
        if n:
            logger.info("worker manager reconciled {} worker run(s)", n)
