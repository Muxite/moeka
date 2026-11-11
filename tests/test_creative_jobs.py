"""Wonhee seam writes the job record: states, evidence, and which side failed (spec 015)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.config.schema import CreativeBridgeConfig
from nanobot.creative_bridge import CreativeActionError, CreativeBridge
from nanobot.creative_jobs import WonheeJobTracker, classify_call_error, classify_delivery_error
from nanobot.kernel.jobs import FailureSide, JobState, JobStore

ROUTE = {"channel": "telegram", "chat_id": "123"}


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.sqlite3")
    yield s
    s.close()


def _active(**kw):
    base = {"id": 7, "project_id": 2, "status": "running", "phase": "render", "idle_s": 5,
            "stalled": False, "route": ROUTE}
    return {**base, **kw}


def _job(store, wid="7"):
    return store.find_by_ref("wonhee_job", wid)


def test_active_job_is_tracked_and_not_rewritten_when_unchanged(store):
    t = WonheeJobTracker(store)
    t.observe_health([_active()])
    t.observe_health([_active()])
    job = _job(store)
    assert job.state is JobState.WORKING and job.role == "wonhee"
    assert job.session_key == "telegram:123"
    assert len(store.history(job.id)) == 2  # heard + one working event


def test_stalled_job_names_wonhee_and_cause(store):
    t = WonheeJobTracker(store)
    t.observe_health([_active()])
    t.observe_health([_active(stalled=True, idle_s=900)])
    last = store.history(_job(store).id)[-1]
    assert (last.side, last.cause) == (FailureSide.WONHEE, "stalled")
    assert "15 min" in last.detail


def test_oct26_queued_then_done_is_observed(store):
    """The stale-status bug: queued, then finished, and the record said queued forever."""
    t = WonheeJobTracker(store)
    t.observe_health([_active(status="queued", phase=None)])
    t.observe_health([])  # left the active list: not done yet, no false claim
    job = _job(store)
    assert job.state is JobState.WORKING
    assert "waiting for its output" in store.history(job.id)[-1].detail
    delivery = {"id": 31, "submission_id": 7, "filename": "reel.mp4"}
    t.observe_delivery_ready(delivery)
    assert _job(store).state is JobState.WORKING
    t.delivery_sent(delivery, [901])
    done = _job(store)
    assert done.state is JobState.DONE
    assert done.refs["wonhee_delivery"] == "31"
    assert "reel.mp4" in store.history(done.id)[-1].detail


def test_gone_marker_written_once(store):
    t = WonheeJobTracker(store)
    t.observe_health([_active()])
    t.observe_health([])
    t.observe_health([])
    assert len(store.history(_job(store).id)) == 3


def test_failure_for_tracked_job_is_failed_on_wonhee_side(store):
    t = WonheeJobTracker(store)
    t.observe_health([_active()])
    t.observe_failures([{"id": 7, "error": "ffmpeg exited 1"}, {"id": 3, "error": "old"}])
    job = _job(store)
    last = store.history(job.id)[-1]
    assert job.state is JobState.FAILED
    assert (last.side, last.cause) == (FailureSide.WONHEE, "submission_failed")
    assert "ffmpeg" in last.detail
    assert _job(store, "3") is None  # untracked history is not imported


def test_delivery_failures_name_the_right_side(store):
    t = WonheeJobTracker(store)
    t.observe_health([_active()])
    delivery = {"id": 31, "submission_id": 7, "filename": "reel.mp4"}
    t.delivery_failed(delivery, TimeoutError("ambiguous"), uncertain=True)
    job = _job(store)
    last = store.history(job.id)[-1]
    assert job.state is JobState.OUTCOME_UNKNOWN
    assert (last.side, last.cause) == (FailureSide.TELEGRAM, "TimeoutError")
    assert classify_delivery_error(FileNotFoundError())[0] is FailureSide.WONHEE


def test_call_errors_split_link_from_wonhee():
    assert classify_call_error(CreativeActionError("timeout", "x", permanent=False))[0] \
        is FailureSide.WRAPPER
    assert classify_call_error(CreativeActionError("invalid_request", "x", permanent=True)) \
        == (FailureSide.WONHEE, "invalid_request")


def test_intake_failure_is_recorded(store):
    WonheeJobTracker(store).intake_failed(
        "telegram:123:0:o", "ab" * 32, CreativeActionError("transfer_failed", "drive 403", permanent=True),
    )
    job = store.recent(1)[0]
    last = store.history(job.id)[-1]
    assert job.state is JobState.FAILED and last.side is FailureSide.WONHEE


def test_tracker_without_a_store_is_a_noop():
    t = WonheeJobTracker(None)
    t.observe_health([_active()])
    t.observe_failures([{"id": 7}])
    t.delivery_sent({"id": 1}, [1])


def test_record_write_failure_never_raises(store):
    t = WonheeJobTracker(store)
    t.observe_health([_active()])
    store.close()
    t.observe_failures([{"id": 7, "error": "x"}])
    t.delivery_sent({"id": 1, "submission_id": 7}, [1])


@pytest.mark.asyncio
async def test_call_timeout_is_classified_not_raw(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr("nanobot.creative_bridge.get_media_dir", lambda _: media)
    peers = SimpleNamespace(peers={"peer-1": object()}, enqueue=AsyncMock(), result_sink=None)
    config = CreativeBridgeConfig(enabled=True, peer_id="peer-1", python="/usr/bin/python3",
                                  cwd=str(tmp_path), home=str(tmp_path / "data"))
    bridge = CreativeBridge(config, workspace=tmp_path, bus=MessageBus(), peers=peers,
                            get_channel=lambda _: None)

    class Slow:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return -9

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=Slow()))

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    with pytest.raises(CreativeActionError) as err:
        await bridge.call("health")
    assert err.value.code == "timeout" and not err.value.permanent


def test_wonhee_job_is_attached_to_the_chat_turn_that_started_it(tmp_path):
    from nanobot.creative_jobs import WonheeJobTracker
    from nanobot.kernel.jobs import JobStore

    store = JobStore(tmp_path / "jobs.sqlite3")
    try:
        other = store.open_job(session_key="telegram:5", request_text="elsewhere")
        topic = store.open_job(session_key="telegram:9:topic:4", request_text="make a clip")
        tracker = WonheeJobTracker(store)
        tracker.observe_health([{"id": 31, "status": "running", "route": {"chat_id": "9"},
                                 "project_id": 2}])
        child = store.find_by_ref("wonhee_job", "31")
        assert child.refs["parent_job"] == topic.id
        assert [c.id for c in store.children(topic.id)] == [child.id]
        assert store.children(other.id) == []
    finally:
        store.close()
