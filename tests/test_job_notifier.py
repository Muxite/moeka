"""Owner notifier (spec 015 phase 3): who gets told, never twice, never for the wrong job."""

import pytest

from nanobot.job_notifier import RESTART_CAUSE, JobNotifier, decide
from nanobot.kernel.jobs import FailureSide, JobState, JobStore


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.sqlite3")
    yield s
    s.close()


class Sink:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def __call__(self, msg, *, record=False, session_key=None):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((msg, record))


def _worker(store, route=("telegram", "9", "")):
    return store.open_job(session_key="telegram:9", request_text="fix export", role="worker",
                          route=route)


@pytest.mark.asyncio
async def test_worker_done_is_sent_to_its_own_chat_and_recorded(store):
    sink = Sink()
    job = _worker(store, route=("telegram", "9", "4"))
    store.transition(job.id, JobState.DONE, detail="Claude reports the export now streams.")
    n = JobNotifier(store, sink)
    assert await n.flush() == 1
    (msg, record), = sink.sent
    assert (msg.channel, msg.chat_id, msg.metadata) == ("telegram", "9", {"message_thread_id": 4})
    assert record is True and "export now streams" in msg.content and job.id in msg.content
    assert await n.flush() == 0  # never twice
    assert store.notification_status(job.id)[0][1] == "sent"


@pytest.mark.asyncio
async def test_worker_failure_names_side_and_cause(store):
    sink = Sink()
    job = _worker(store)
    store.transition(job.id, JobState.FAILED, side=FailureSide.WORKER, cause="claude_exit_1")
    await JobNotifier(store, sink).flush()
    assert "the worker agent" in sink.sent[0][0].content
    assert "claude_exit_1" in sink.sent[0][0].content


@pytest.mark.asyncio
async def test_chat_turn_wonhee_and_parent_done_are_not_repeated(store):
    sink = Sink()
    chat = store.open_job(session_key="telegram:9", request_text="hi", route=("telegram", "9", ""))
    store.transition(chat.id, JobState.DONE)
    wonhee = store.open_job(session_key="telegram:9", request_text="clip", role="wonhee")
    store.transition(wonhee.id, JobState.DONE)
    parent = store.open_job(session_key="telegram:9", request_text="p", role="worker",
                            route=("telegram", "9", ""))
    child = _worker(store)
    store.link_child(parent.id, child.id, "worker")
    store.transition(parent.id, JobState.DONE)
    assert await JobNotifier(store, sink).flush() == 0
    assert sink.sent == []


@pytest.mark.asyncio
async def test_uncertain_send_is_not_retried(store):
    job = _worker(store)
    store.transition(job.id, JobState.DONE)
    n = JobNotifier(store, Sink(fail=True))
    assert await n.flush() == 0
    assert store.notification_status(job.id)[0][1] == "uncertain"
    good = Sink()
    assert await JobNotifier(store, good).flush() == 0 and good.sent == []


@pytest.mark.asyncio
async def test_restart_orphans_in_flight_chat_jobs_and_tells_the_owner(store):
    sink = Sink()
    stuck = store.open_job(session_key="telegram:9", request_text="render it",
                           route=("telegram", "9", ""))
    waiting = store.open_job(session_key="telegram:9", request_text="q")
    store.transition(waiting.id, JobState.NEEDS_ANSWER)
    n = JobNotifier(store, sink, poll_s=60)
    await n.start()
    try:
        await n.flush()
    finally:
        await n.stop()
    assert store.get(stuck.id).state is JobState.OUTCOME_UNKNOWN
    assert store.get(waiting.id).state is JobState.NEEDS_ANSWER
    (msg, _), = sink.sent
    assert RESTART_CAUSE in msg.content and "render it" in msg.content


@pytest.mark.asyncio
async def test_missing_route_falls_back_to_the_heartbeat_target(store):
    sink = Sink()
    job = _worker(store, route=None)
    store.transition(job.id, JobState.DONE)
    await JobNotifier(store, sink, fallback_target=lambda: ("telegram", "77")).flush()
    assert sink.sent[0][0].chat_id == "77"


def test_decide_matrix(store):
    chat = store.open_job(session_key="s", request_text="x")
    assert decide(chat, JobState.DONE, None, False) == "skip"
    assert decide(chat, JobState.OUTCOME_UNKNOWN, RESTART_CAUSE, False) == "send"
    w = store.open_job(session_key="s", request_text="x", role="worker")
    assert decide(w, JobState.NEEDS_ANSWER, None, False) == "send"
    assert decide(w, JobState.DONE, None, True) == "skip"
    assert decide(w, JobState.FAILED, "x", True) == "send"
