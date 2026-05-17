"""Background shell tool — start long-running commands and report when they exit.

Unlike ExecTool, which blocks the agent until the subprocess completes, this
tool returns immediately with a ``task_id``. The agent keeps responding to
the user while the work runs. When the process exits, the registry publishes
a ``channel="system"`` inbound message to the bus, which wakes the originating
session's agent loop so it can decide whether to message the user (the
existing system-channel dispatch path in loop.py handles routing).

The agent can also poll on its own — see the ``status``, ``tail``, ``list``,
and ``kill`` actions — which is what enables a human-feeling exchange like:

    user: dd that ISO to the SD card
    moeka: started dd, will let you know when it finishes
    user: how's it going?
    moeka: [calls bg.tail] roughly 42% — 4 GB written
    (later, unprompted, on process exit)
    moeka: dd finished, exit 0
"""

from __future__ import annotations

import asyncio
import os
import shutil
import shlex
import sys
import time
import uuid
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus

_IS_WINDOWS = sys.platform == "win32"


@dataclass
class _BgTask:
    task_id: str
    label: str
    command: str
    started_at: float
    log_path: Path
    pid: int
    process: asyncio.subprocess.Process
    monitor_task: asyncio.Task[None]
    origin_channel: str
    origin_chat_id: str
    origin_session_key: str
    exit_code: int | None = None
    finished_at: float | None = None


class BackgroundProcessRegistry:
    """In-process registry for background shell tasks.

    Lives for the lifetime of the agent loop. Publishes a system-channel
    inbound message whenever a tracked process exits so the originating
    session re-engages the agent loop.
    """

    def __init__(self, bus: MessageBus, workspace: Path) -> None:
        self._bus = bus
        self._workspace = Path(workspace).expanduser()
        self._tasks: dict[str, _BgTask] = {}
        self._log_dir = self._workspace / "bg-shell"
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def _new_id(self) -> str:
        return uuid.uuid4().hex[:8]

    async def start(
        self,
        command: str,
        label: str | None,
        origin_channel: str,
        origin_chat_id: str,
        origin_session_key: str,
    ) -> _BgTask:
        task_id = self._new_id()
        display_label = (label or command[:40]).strip() or task_id
        log_path = self._log_dir / f"{task_id}.log"
        # Write a small header so the agent can correlate logs with tasks
        # without having to call status() first.
        log_path.write_text(
            f"# task_id={task_id}\n# label={display_label}\n# cmd={command}\n",
            encoding="utf-8",
        )

        # Open the log for stdout+stderr merge. Using a real fd (not PIPE)
        # means the OS handles buffering — agent process memory doesn't grow
        # with log volume.
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_APPEND)
        try:
            if _IS_WINDOWS:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=log_fd,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(self._workspace),
                )
            else:
                bash = shutil.which("bash") or "/bin/bash"
                process = await asyncio.create_subprocess_exec(
                    bash, "-l", "-c", command,
                    stdout=log_fd,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(self._workspace),
                    # New session detaches the child from the agent's
                    # controlling tty / process group, so SIGINT to the
                    # agent doesn't propagate and kill long jobs.
                    start_new_session=True,
                )
        finally:
            # The subprocess inherits the fd; we can close our handle.
            os.close(log_fd)

        task = _BgTask(
            task_id=task_id,
            label=display_label,
            command=command,
            started_at=time.time(),
            log_path=log_path,
            pid=process.pid,
            process=process,
            monitor_task=None,  # set below
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            origin_session_key=origin_session_key,
        )
        task.monitor_task = asyncio.create_task(self._monitor(task))
        self._tasks[task_id] = task
        logger.info("bg-shell {} started (pid={}): {}", task_id, process.pid, display_label)
        return task

    async def _monitor(self, task: _BgTask) -> None:
        try:
            rc = await task.process.wait()
        except asyncio.CancelledError:
            with suppress(Exception):
                task.process.kill()
            raise
        task.exit_code = rc
        task.finished_at = time.time()
        duration = task.finished_at - task.started_at
        logger.info(
            "bg-shell {} exited rc={} after {:.1f}s", task.task_id, rc, duration,
        )
        # Publish a system-channel inbound so the originating agent session
        # re-engages and can decide whether to notify the user. Mirror the
        # subagent announcement contract: chat_id="<channel>:<chat_id>",
        # session_key_override carries the user's session key.
        content = (
            f"Background task {task.task_id} ({task.label!r}) finished. "
            f"Exit code: {rc}. Duration: {duration:.1f}s. "
            f"Log: {task.log_path}. "
            "Use bg_shell action=tail to read recent output before deciding "
            "whether to notify the user."
        )
        msg = InboundMessage(
            channel="system",
            sender_id="bg_shell",
            chat_id=f"{task.origin_channel}:{task.origin_chat_id}",
            content=content,
            session_key_override=task.origin_session_key,
            metadata={
                "_proactive_source": "bg_shell",
                "_bg_task_id": task.task_id,
            },
        )
        try:
            await self._bus.publish_inbound(msg)
        except Exception:
            logger.exception("bg-shell {}: failed to publish exit announcement", task.task_id)

    def get(self, task_id: str) -> _BgTask | None:
        return self._tasks.get(task_id)

    def list_active(self, session_key: str | None = None) -> list[_BgTask]:
        items = [t for t in self._tasks.values() if t.exit_code is None]
        if session_key is not None:
            items = [t for t in items if t.origin_session_key == session_key]
        return items

    def list_all(self, session_key: str | None = None) -> list[_BgTask]:
        items = list(self._tasks.values())
        if session_key is not None:
            items = [t for t in items if t.origin_session_key == session_key]
        return items

    def tail(self, task_id: str, max_chars: int = 4000) -> str:
        task = self._tasks.get(task_id)
        if task is None:
            return f"Error: no background task with id {task_id!r}"
        try:
            data = task.log_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading log: {e}"
        if len(data) <= max_chars:
            return data
        return f"... ({len(data) - max_chars:,} earlier chars omitted) ...\n" + data[-max_chars:]

    async def kill(self, task_id: str) -> str:
        task = self._tasks.get(task_id)
        if task is None:
            return f"Error: no background task with id {task_id!r}"
        if task.exit_code is not None:
            return f"Task {task_id} already exited with code {task.exit_code}"
        try:
            task.process.terminate()
            try:
                await asyncio.wait_for(task.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                task.process.kill()
        except ProcessLookupError:
            pass
        return f"Sent terminate to task {task_id} (pid={task.pid})"

    async def shutdown(self) -> None:
        """Stop all tracked processes (gateway shutdown)."""
        for task in list(self._tasks.values()):
            if task.exit_code is None:
                with suppress(Exception):
                    task.process.terminate()
        for task in list(self._tasks.values()):
            if task.monitor_task is not None and not task.monitor_task.done():
                task.monitor_task.cancel()


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "One of: start | status | tail | list | kill",
        ),
        command=StringSchema("Shell command to run (action=start only)"),
        label=StringSchema("Short human-readable label for status/list output (action=start only)"),
        task_id=StringSchema("Task id (action=status|tail|kill)"),
        max_chars=IntegerSchema(
            4000,
            description="Tail size in characters (action=tail). Default 4000.",
            minimum=200,
            maximum=20000,
        ),
        required=["action"],
    )
)
class BackgroundShellTool(Tool):
    """Run shell commands in the background and read their progress.

    The agent uses this to start a long-running task without blocking the
    conversation. The registry publishes a system-channel inbound message
    when each task exits so the agent re-engages and can notify the user.
    """

    def __init__(self, registry: BackgroundProcessRegistry) -> None:
        self._registry = registry
        self._origin_channel: ContextVar[str] = ContextVar("bg_origin_channel", default="cli")
        self._origin_chat_id: ContextVar[str] = ContextVar("bg_origin_chat_id", default="direct")
        self._origin_session_key: ContextVar[str] = ContextVar(
            "bg_origin_session_key", default="cli:direct",
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        # bg_shell needs a BackgroundProcessRegistry that the loop wires in
        # manually outside the auto-loader. Skip auto-discovery.
        return False

    @property
    def name(self) -> str:
        return "bg_shell"

    @property
    def description(self) -> str:
        return (
            "Start, inspect, and stop background shell commands. "
            "Use action=start for any task expected to take more than ~10s "
            "(downloads, builds, dd, long rsync). Returns a task_id immediately; "
            "the agent is re-woken automatically when the task exits, so you "
            "can announce completion to the user without being prompted. "
            "Use action=tail to check progress when the user asks how it's "
            "going. Use action=list to see your active background work."
        )

    def set_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,  # noqa: ARG002 — kept for interface parity
        metadata: dict[str, Any] | None = None,  # noqa: ARG002
        session_key: str | None = None,
    ) -> None:
        self._origin_channel.set(channel)
        self._origin_chat_id.set(chat_id)
        self._origin_session_key.set(session_key or f"{channel}:{chat_id}")

    async def execute(
        self,
        action: str,
        command: str | None = None,
        label: str | None = None,
        task_id: str | None = None,
        max_chars: int = 4000,
        **kwargs: Any,
    ) -> str:
        action = (action or "").strip().lower()
        if action == "start":
            if not command:
                return "Error: action=start requires command"
            try:
                task = await self._registry.start(
                    command=command,
                    label=label,
                    origin_channel=self._origin_channel.get(),
                    origin_chat_id=self._origin_chat_id.get(),
                    origin_session_key=self._origin_session_key.get(),
                )
            except Exception as e:
                logger.exception("bg-shell start failed")
                return f"Error starting background task: {e}"
            return (
                f"Started background task {task.task_id} (pid={task.pid}): "
                f"{shlex.quote(command) if not _IS_WINDOWS else command}. "
                "You will be re-woken when it exits."
            )
        if action == "status":
            if not task_id:
                return "Error: action=status requires task_id"
            t = self._registry.get(task_id)
            if t is None:
                return f"No task with id {task_id!r}"
            return self._format_status(t)
        if action == "tail":
            if not task_id:
                return "Error: action=tail requires task_id"
            return self._registry.tail(task_id, max_chars=max_chars)
        if action == "list":
            session_key = self._origin_session_key.get()
            items = self._registry.list_all(session_key=session_key)
            if not items:
                return "No background tasks for this session."
            return "\n".join(self._format_status(t) for t in items)
        if action == "kill":
            if not task_id:
                return "Error: action=kill requires task_id"
            return await self._registry.kill(task_id)
        return f"Error: unknown action {action!r}. Use start | status | tail | list | kill."

    @staticmethod
    def _format_status(t: _BgTask) -> str:
        if t.exit_code is None:
            elapsed = time.time() - t.started_at
            return f"[{t.task_id}] running ({elapsed:.0f}s) — {t.label}"
        duration = (t.finished_at or t.started_at) - t.started_at
        return f"[{t.task_id}] exited rc={t.exit_code} ({duration:.0f}s) — {t.label}"
