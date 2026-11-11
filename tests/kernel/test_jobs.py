"""Job record store (spec 015 phase 1): states, real timestamps, side + cause, redaction."""

from __future__ import annotations

import threading

import pytest

from nanobot.kernel.jobs import (
    ACTIVE_STATES,
    FailureSide,
    JobState,
    JobStore,
    new_job_id,
)


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.sqlite3")
    yield s
    s.close()


def test_open_job_starts_heard_with_one_event(store):
    job = store.open_job(session_key="telegram:1", request_text="open a port", role="operator")
    got = store.get(job.id)
    assert got is not None
    assert got.state is JobState.HEARD
    assert got.role == "operator"
    events = store.history(job.id)
    assert [e.state for e in events] == [JobState.HEARD]
    assert events[0].ts  # real timestamp


def test_transition_appends_history_in_order(store):
    job = store.open_job(session_key="s", request_text="x")
    store.transition(job.id, JobState.WORKING, detail="started")
    store.transition(job.id, JobState.DONE, detail="port 41234 open")
    assert store.get(job.id).state is JobState.DONE
    assert [e.state for e in store.history(job.id)] == [
        JobState.HEARD, JobState.WORKING, JobState.DONE,
    ]


def test_failure_requires_side_and_cause(store):
    job = store.open_job(session_key="s", request_text="x")
    with pytest.raises(ValueError):
        store.transition(job.id, JobState.FAILED)
    store.transition(
        job.id, JobState.FAILED, side=FailureSide.POLICY, cause="workspace_violation",
        detail="read /mnt/arteta/stagecraft denied",
    )
    last = store.history(job.id)[-1]
    assert last.side is FailureSide.POLICY
    assert last.cause == "workspace_violation"


def test_outcome_unknown_and_blocked_also_need_side_and_cause(store):
    job = store.open_job(session_key="s", request_text="x")
    for state in (JobState.OUTCOME_UNKNOWN, JobState.BLOCKED):
        with pytest.raises(ValueError):
            store.transition(job.id, state)


def test_link_ref_merges_and_is_queryable(store):
    job = store.open_job(session_key="s", request_text="x")
    store.link_ref(job.id, "wonhee_job", "abc")
    store.link_ref(job.id, "wonhee_delivery", "12")
    assert store.get(job.id).refs == {"wonhee_job": "abc", "wonhee_delivery": "12"}
    assert store.find_by_ref("wonhee_job", "abc").id == job.id
    assert store.find_by_ref("wonhee_job", "nope") is None


def test_active_excludes_terminal_states(store):
    a = store.open_job(session_key="s", request_text="a")
    b = store.open_job(session_key="s", request_text="b")
    store.transition(b.id, JobState.DONE)
    ids = {j.id for j in store.active()}
    assert a.id in ids and b.id not in ids
    assert JobState.DONE not in ACTIVE_STATES


def test_secrets_redacted_and_detail_capped(store):
    job = store.open_job(
        session_key="s", request_text="use token sk-abcdefghijklmnopqrstuvwxyz123456 now",
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in store.get(job.id).request_text
    store.transition(job.id, JobState.WORKING, detail="x" * 10_000)
    assert len(store.history(job.id)[-1].detail) < 3000


def test_unknown_job_transition_raises(store):
    with pytest.raises(KeyError):
        store.transition("nope", JobState.DONE)


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    s1 = JobStore(path)
    job = s1.open_job(session_key="s", request_text="x")
    s1.transition(job.id, JobState.DONE)
    s1.close()
    s2 = JobStore(path)
    assert s2.get(job.id).state is JobState.DONE
    s2.close()


def test_concurrent_transitions_all_recorded(store):
    job = store.open_job(session_key="s", request_text="x")

    def worker(i: int) -> None:
        store.transition(job.id, JobState.WORKING, detail=f"n{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.history(job.id)) == 21


def test_new_job_id_unique():
    assert len({new_job_id() for _ in range(200)}) == 200


def test_jobs_command_text_lists_and_shows_history(store):
    from nanobot.command.builtin import format_jobs

    job = store.open_job(session_key="s", request_text="make the video", role="wonhee")
    store.transition(job.id, JobState.WORKING)
    store.transition(
        job.id, JobState.BLOCKED, side=FailureSide.POLICY, cause="workspace_violation",
        detail="read /mnt/arteta denied",
    )
    listing = format_jobs(store, "")
    assert job.id in listing and "blocked (policy: workspace_violation)" in listing
    detail = format_jobs(store, job.id[:6])
    assert "policy:workspace_violation" in detail and "read /mnt/arteta denied" in detail
    assert "No job" in format_jobs(store, "zzzz")
    assert "No jobs recorded" in format_jobs(JobStore(store.path.parent / "empty.sqlite3"), "")


# -- spec 015 phase 3: route, links, notification outbox ------------------------------------


def test_route_is_recorded_and_exposed(store):
    job = store.open_job(
        session_key="telegram:9:topic:4", request_text="x", route=("telegram", "9", "4"),
    )
    assert store.get(job.id).route == ("telegram", "9", "4")
    plain = store.open_job(session_key="s", request_text="x")
    assert store.get(plain.id).route is None


def test_notifications_are_queued_only_for_states_worth_telling(store):
    job = store.open_job(session_key="s", request_text="x")
    store.transition(job.id, JobState.WORKING)
    assert store.pending_notifications() == []
    store.transition(job.id, JobState.DONE, detail="ok")
    pending = store.pending_notifications()
    assert [(n.job_id, n.state) for n in pending] == [(job.id, JobState.DONE)]
    store.set_notification(job.id, pending[0].event_seq, "sent")
    assert store.pending_notifications() == []
    assert store.notification_status(job.id)[0][1:] == ("sent", "done")


def test_crashed_sending_row_becomes_uncertain_not_resent(store):
    job = store.open_job(session_key="s", request_text="x")
    store.transition(job.id, JobState.FAILED, side=FailureSide.WORKER, cause="exit_1")
    (n,) = store.pending_notifications()
    store.set_notification(job.id, n.event_seq, "sending")
    assert store.pending_notifications() == []
    assert store.recover_notifications() == 1
    assert store.notification_status(job.id)[0][1] == "uncertain"
    assert store.pending_notifications() == []


def test_children_and_active_children(store):
    parent = store.open_job(session_key="s", request_text="p")
    a = store.open_job(session_key="s", request_text="a", role="worker")
    b = store.open_job(session_key="s", request_text="b", role="wonhee")
    store.link_child(parent.id, a.id, "worker")
    store.link_child(parent.id, b.id, "wonhee")
    store.transition(a.id, JobState.DONE)
    assert {c.id for c in store.children(parent.id)} == {a.id, b.id}
    assert [c.id for c in store.active_children(parent.id)] == [b.id]
    assert store.parent_of(a.id).id == parent.id


def test_list_filters_and_latest_for_session(store):
    one = store.open_job(session_key="s", request_text="1")
    two = store.open_job(session_key="s", request_text="2", role="worker")
    store.transition(one.id, JobState.FAILED, side=FailureSide.MOEKA, cause="x")
    assert [j.id for j in store.list(states={JobState.FAILED})] == [one.id]
    assert [j.id for j in store.list(role="worker")] == [two.id]
    assert store.latest_for_session("s").id == two.id
    assert store.latest_for_session("none") is None


def test_listener_wakes_after_commit_and_cannot_break_the_record(store):
    seen = []
    store.add_listener(lambda jid, st: seen.append((jid, st, store.get(jid).state)))
    store.add_listener(lambda jid, st: 1 / 0)
    job = store.open_job(session_key="s", request_text="x")
    store.transition(job.id, JobState.DONE)
    assert seen == [(job.id, JobState.DONE, JobState.DONE)]
