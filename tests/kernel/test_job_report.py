"""System-built job reports and the delayed acknowledgement (spec 015 phase 3)."""

from __future__ import annotations

import asyncio

import pytest

from nanobot.agent.hook import AgentRunHookContext, AgentTurnHookContext
from nanobot.kernel.job_hook import JobHook, job_hook_factory
from nanobot.kernel.job_report import format_ack, format_dead_turn_report, format_job_notice
from nanobot.kernel.jobs import FailureSide, JobState, JobStore


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.sqlite3")
    yield s
    s.close()


def test_dead_turn_report_names_side_cause_and_does_not_guess(store):
    job = store.open_job(session_key="telegram:1", request_text="render the video")
    store.transition(job.id, JobState.WORKING)
    store.transition(
        job.id, JobState.OUTCOME_UNKNOWN, side=FailureSide.MOEKA, cause="empty_final_response",
        detail="tools run: mcp_wonhee_stagecraft_submit; last error: stagecraft: timeout",
    )
    text = format_dead_turn_report(store.get(job.id), store.history(job.id))
    assert "I did not finish this turn" in text
    assert "moeka itself" in text and "empty_final_response" in text
    assert "mcp_wonhee_stagecraft_submit" in text
    assert "cannot tell whether the work completed" in text
    assert "couldn't produce a final answer" not in text


def test_notice_for_a_policy_block_names_the_policy(store):
    job = store.open_job(session_key="s", request_text="x")
    store.transition(
        job.id, JobState.BLOCKED, side=FailureSide.POLICY, cause="workspace_violation",
        detail="read /mnt/arteta/x denied",
    )
    text = format_job_notice(store.get(job.id), store.history(job.id))
    assert "Blocked" in text and "the access policy" in text and "workspace_violation" in text


def test_worker_side_is_named(store):
    job = store.open_job(session_key="s", request_text="x", role="worker")
    store.transition(job.id, JobState.FAILED, side=FailureSide.WORKER, cause="claude_exit_1")
    assert "the worker agent" in format_job_notice(store.get(job.id), store.history(job.id))


def test_ack_mentions_the_job(store):
    job = store.open_job(session_key="s", request_text="x")
    assert job.id in format_ack(job)


def _turn(**kw):
    return AgentTurnHookContext(
        channel="telegram", chat_id="9", session_key="telegram:9", request_text="do it", **kw,
    )


def test_factory_records_the_route_with_topic(store):
    factory = job_hook_factory(store)
    factory(_turn(metadata={"message_thread_id": 4}))
    (job,) = store.active()
    assert job.route == ("telegram", "9", "4")


@pytest.mark.asyncio
async def test_ack_fires_only_when_the_run_is_slow(store):
    sent: list[str] = []

    async def ack(turn, job):
        sent.append(job.id)

    factory = job_hook_factory(store, ack=ack, ack_after_s=0.05)
    fast = factory(_turn())
    await fast.before_run(AgentRunHookContext(messages=[]))
    await fast.after_run(AgentRunHookContext(messages=[], stop_reason="completed"))
    await asyncio.sleep(0.1)
    assert sent == []  # finished before the delay: no ack noise

    slow = factory(_turn())
    await slow.before_run(AgentRunHookContext(messages=[]))
    await asyncio.sleep(0.15)
    await slow.after_run(AgentRunHookContext(messages=[], stop_reason="completed"))
    assert sent == [slow.job_id]


@pytest.mark.asyncio
async def test_failing_ack_never_breaks_the_run(store):
    async def ack(turn, job):
        raise RuntimeError("network down")

    hook = JobHook(store, store.open_job(session_key="s", request_text="x").id,
                   ack=lambda job: ack(None, job), ack_after_s=0.01)
    await hook.before_run(AgentRunHookContext(messages=[]))
    await asyncio.sleep(0.05)
    await hook.after_run(AgentRunHookContext(messages=[], stop_reason="completed"))
    assert store.get(hook.job_id).state is JobState.DONE


def _fake_turn(store, stop_reason, final, kind=None, ephemeral=False):
    from types import SimpleNamespace

    from nanobot.agent.loop import AgentLoop, TurnKind

    turn = SimpleNamespace(
        kind=kind or TurnKind.USER, ephemeral=ephemeral, stop_reason=stop_reason,
        final_content=final, require_session=lambda: SimpleNamespace(key="telegram:9"),
    )
    loop = SimpleNamespace(job_store=store)
    return AgentLoop._dead_turn_report(loop, turn)


def test_loop_replaces_the_generic_empty_message_with_a_record_report(store):
    job = store.open_job(session_key="telegram:9", request_text="make the cover")
    store.transition(
        job.id, JobState.OUTCOME_UNKNOWN, side=FailureSide.MOEKA, cause="empty_final_response",
    )
    text = _fake_turn(
        store, "empty_final_response",
        "I completed the tool steps but couldn't produce a final answer.",
    )
    assert text and job.id in text and "empty_final_response" in text


def test_loop_leaves_a_real_answer_alone_and_skips_ephemeral_turns(store):
    store.open_job(session_key="telegram:9", request_text="x")
    assert _fake_turn(store, "completed", "Here you go.") is None
    assert _fake_turn(store, "empty_final_response", "", ephemeral=True) is None
    assert _fake_turn(None, "empty_final_response", "") is None
