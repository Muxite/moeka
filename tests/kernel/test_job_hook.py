"""JobHook: run outcomes land in the job record with a named side and cause (spec 015)."""

from __future__ import annotations

import pytest

from nanobot.agent.hook import AgentRunHookContext
from nanobot.kernel.job_hook import JobHook
from nanobot.kernel.jobs import FailureSide, JobState, JobStore


@pytest.fixture
def setup(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = store.open_job(session_key="s", request_text="make the video")
    yield store, job, JobHook(store, job.id)
    store.close()


def _ctx(stop="completed", events=None, tools=None, exc=None):
    return AgentRunHookContext(
        messages=[], stop_reason=stop, tool_events=events or [], tools_used=tools or [],
        exception=exc,
    )


async def test_completed_run_is_working_then_done(setup):
    store, job, hook = setup
    await hook.before_run(_ctx())
    assert store.get(job.id).state is JobState.WORKING
    await hook.after_run(_ctx("completed", tools=["tmux"]))
    await hook.on_finally(_ctx("completed"))
    assert store.get(job.id).state is JobState.DONE
    assert [e.state for e in store.history(job.id)] == [
        JobState.HEARD, JobState.WORKING, JobState.DONE,
    ]


async def test_oct26_dead_turn_names_policy_wall(setup):
    """Tools ran, reads hit the workspace wall, loop ended empty: blocked by policy."""
    store, job, hook = setup
    events = [
        {"name": "read_file", "status": "error",
         "detail": "workspace_violation: /mnt/arteta/stagecraft/Scene.tsx"},
    ]
    await hook.before_run(_ctx())
    await hook.after_run(_ctx("policy_denials", events, ["tmux", "read_file"]))
    last = store.history(job.id)[-1]
    assert store.get(job.id).state is JobState.BLOCKED
    assert last.side is FailureSide.POLICY
    assert last.cause == "workspace_violation"
    assert "/mnt/arteta/stagecraft" in last.detail


async def test_empty_final_response_is_outcome_unknown_with_summary(setup):
    store, job, hook = setup
    events = [{"name": "tmux", "status": "ok", "detail": ""}]
    await hook.before_run(_ctx())
    await hook.after_run(_ctx("empty_final_response", events, ["tmux", "mcp_wonhee_show"]))
    last = store.history(job.id)[-1]
    assert last.state is JobState.OUTCOME_UNKNOWN
    assert last.cause == "empty_final_response"
    assert "tmux" in last.detail


async def test_tool_error_attributes_wonhee_side(setup):
    store, job, hook = setup
    events = [{"name": "mcp_wonhee_make_submit", "status": "error", "detail": "invalid_request"}]
    await hook.before_run(_ctx())
    await hook.after_run(_ctx("tool_error", events, ["mcp_wonhee_make_submit"]))
    last = store.history(job.id)[-1]
    assert last.state is JobState.FAILED
    assert last.side is FailureSide.WONHEE
    assert last.cause == "tool_error:mcp_wonhee_make_submit"


async def test_ask_user_is_needs_answer(setup):
    store, job, hook = setup
    await hook.before_run(_ctx())
    await hook.after_run(_ctx("ask_user"))
    assert store.get(job.id).state is JobState.NEEDS_ANSWER


async def test_crash_without_after_run_is_outcome_unknown(setup):
    store, job, hook = setup
    await hook.before_run(_ctx())
    await hook.on_finally(_ctx("error", exc=RuntimeError("boom")))
    last = store.history(job.id)[-1]
    assert last.state is JobState.OUTCOME_UNKNOWN
    assert last.cause == "RuntimeError"


async def test_hook_failure_never_raises(setup):
    store, job, hook = setup
    store.close()
    await hook.before_run(_ctx())  # closed db: logged, not raised
    await hook.after_run(_ctx("completed"))


def _turn(text="open a port", key="telegram:1", channel="telegram", ephemeral=False):
    from nanobot.agent.hook import AgentTurnHookContext

    return AgentTurnHookContext(
        channel=channel, chat_id="1", session_key=key, ephemeral=ephemeral, request_text=text,
    )


def test_factory_opens_one_job_per_owner_turn(tmp_path):
    from nanobot.kernel.job_hook import job_hook_factory

    store = JobStore(tmp_path / "j.sqlite3")
    factory = job_hook_factory(store, role="operator")
    assert factory(_turn()) is not None
    assert len(store.active()) == 1 and store.active()[0].role == "operator"
    for skipped in (
        _turn(text=""), _turn(ephemeral=True), _turn(channel="cron"),
        _turn(key="heartbeat"), _turn(key="dream:2026"),
    ):
        assert factory(skipped) is None
    assert len(store.active()) == 1
    store.close()


async def test_answer_to_a_question_resumes_the_waiting_job(tmp_path):
    from nanobot.kernel.job_hook import job_hook_factory

    store = JobStore(tmp_path / "j.sqlite3")
    factory = job_hook_factory(store)
    first = factory(_turn("run my program"))
    await first.before_run(_ctx())
    await first.after_run(_ctx("ask_user"))
    second = factory(_turn("the mesh one"))
    assert second._job_id == first._job_id
    assert len(store.recent()) == 1
    store.close()


async def test_wonhee_error_code_becomes_the_cause(setup):
    store, job, hook = setup
    events = [{
        "name": "mcp_wonhee_make_submit", "status": "error",
        "detail": '[External content] {"error": {"code": "invalid_request", "message": "x"}}',
    }]
    await hook.before_run(_ctx())
    await hook.after_run(_ctx("tool_error", events, ["mcp_wonhee_make_submit"]))
    last = store.history(job.id)[-1]
    assert last.side is FailureSide.WONHEE
    assert last.cause == "mcp_wonhee_make_submit:invalid_request"
