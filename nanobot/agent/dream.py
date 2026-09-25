"""Direct Dream invocation: one memory-consolidation run, no scheduler.

Core never schedules Dream. A caller (the ``/dream`` command, a harness, or a
host application) awaits :func:`run_dream` whenever it wants a run;
``DreamConfig.interval_h`` is only advice for such callers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from nanobot.agent.memory import MemoryStore

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop

DreamStatus = Literal["no_input", "completed", "incomplete", "failed"]


@dataclass(frozen=True)
class DreamRunResult:
    """Outcome of one Dream run."""

    status: DreamStatus
    elapsed_s: float = 0.0
    changed: bool = False  # durable memory files changed during the run
    cursor: int | None = None  # history cursor Dream advanced to (completed runs)
    reason: str | None = None  # why an incomplete run stopped
    error: Exception | None = None  # the exception behind a failed run
    commit_sha: str | None = None  # memory git commit recording the run


async def _silent(*_args: Any, **_kwargs: Any) -> None:
    pass


async def run_dream(
    loop: AgentLoop,
    *,
    commit_prefix: str = "dream: manual run",
) -> DreamRunResult:
    """Run one Dream consolidation pass over unprocessed history.

    The history cursor only advances when the run completes normally, so an
    incomplete or failed batch is retried by the next call. Memory edits are
    committed to the workspace memory git store (when initialised), history is
    compacted, the vector index refreshed and old Dream sessions pruned,
    whatever the outcome.
    """
    store = loop.context.memory
    t0 = time.monotonic()
    diff_body = ""
    commit_sha: str | None = None
    result: DreamRunResult
    try:
        batch = store.build_dream_prompt()
        if batch is None:
            result = DreamRunResult(status="no_input")
        else:
            prompt, last_cursor = batch
            resp = await loop.process_direct(
                prompt,
                session_key=MemoryStore.dream_session_key(),
                ephemeral=True,
                tools=store.build_dream_tools(),
                on_progress=_silent,
                runtime=loop.dream_runtime(),
            )
            elapsed = time.monotonic() - t0
            # The real file delta grounds the audit record; normal completion
            # decides whether this history batch has finished processing.
            diff_body = store.dream_content_diff()
            if MemoryStore.dream_run_completed(resp):
                store.set_last_dream_cursor(last_cursor)
                result = DreamRunResult(
                    status="completed",
                    elapsed_s=elapsed,
                    changed=bool(diff_body),
                    cursor=last_cursor,
                )
            else:
                result = DreamRunResult(
                    status="incomplete",
                    elapsed_s=elapsed,
                    changed=bool(diff_body),
                    reason=MemoryStore.dream_incompletion_reason(resp),
                )
    except Exception as exc:
        logger.exception("Dream run failed")
        result = DreamRunResult(
            status="failed",
            elapsed_s=time.monotonic() - t0,
            error=exc,
        )
    finally:
        if store.git.is_initialized():
            commit_sha = store.git.auto_commit(
                MemoryStore.build_dream_commit_message(commit_prefix, diff_body)
            )
        store.compact_history()
        store.reindex_memory()  # moeka: refresh VecStore after Dream edits
        MemoryStore.prune_dream_sessions(loop.sessions)
    return replace(result, commit_sha=commit_sha) if commit_sha else result
