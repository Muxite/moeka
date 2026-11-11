"""Downtime reviewer: only when idle, quiet-hours limited, one question per pattern."""

import pytest

from nanobot.job_review_service import JobReviewer, build_digest
from nanobot.kernel.jobs import FailureSide, JobState, JobStore


class Limiter:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, 0

    def reserve(self, **kw):
        self.calls += 1
        return self.ok


class Sink:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg, *, record=False, session_key=None):
        self.sent.append((msg, record))


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.sqlite3")
    yield s
    s.close()


def _repeat(store, n=3):
    for _ in range(n):
        j = store.open_job(session_key="s", request_text="send the weekly status report to the team")
        store.transition(j.id, JobState.DONE)


@pytest.mark.asyncio
async def test_asks_once_in_downtime_and_never_builds_anything(store):
    _repeat(store)
    sink, lim = Sink(), Limiter()
    r = JobReviewer(store, sink, lambda: ("telegram", "9"), lim)
    assert await r.run_once() is True
    (msg, record), = sink.sent
    assert record and msg.chat_id == "9"
    assert "3 times" in msg.content and "/approve-skill" in msg.content
    assert await r.run_once() is False  # same pattern is not asked twice


@pytest.mark.asyncio
async def test_not_while_a_job_is_active_and_not_outside_quiet_hours(store):
    _repeat(store)
    store.open_job(session_key="s", request_text="in flight")
    sink = Sink()
    assert await JobReviewer(store, sink, lambda: ("t", "1"), Limiter()).run_once() is False
    for j in store.active():
        store.transition(j.id, JobState.DONE)
    busy_hours = Limiter(ok=False)
    assert await JobReviewer(store, sink, lambda: ("t", "1"), busy_hours).run_once() is False
    assert sink.sent == [] and not store.was_asked("repeat:send weekly status report team")


def test_digest_includes_repeated_failures(store):
    for _ in range(3):
        j = store.open_job(session_key="s", request_text="render clip", role="worker")
        store.transition(j.id, JobState.FAILED, side=FailureSide.WORKER, cause="codex:usage_limit")
    text, keys = build_digest(store)
    assert "worker:codex:usage_limit" in text and keys[0][1] == "failure"
