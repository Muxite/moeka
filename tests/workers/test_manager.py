"""WorkerManager with fake CLIs: resume, questions, failures, restart recovery (spec 015)."""

import asyncio
import os
import signal
import sys
import textwrap
from types import SimpleNamespace

import pytest

from nanobot.kernel.jobs import FailureSide, JobState, JobStore
from nanobot.workers.drivers import ClaudeDriver, CodexDriver, split_status, wrap_request
from nanobot.workers.manager import WorkerError, WorkerManager

FAKE = textwrap.dedent(
    """
    import json, os, signal, sys, time
    prompt, sid = sys.argv[1], sys.argv[2]
    sid = sid or "sess-%d" % os.getpid()
    def emit(o): print(json.dumps(o), flush=True)
    emit({"type": "system", "subtype": "init", "session_id": sid})
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}})
    if "MODE:slow" in prompt: time.sleep(float(os.environ.get("FAKE_SLEEP", "1.5")))
    if "MODE:hang" in prompt: time.sleep(60)
    if "MODE:crash" in prompt:
        sys.stderr.write("segfault-ish"); sys.exit(3)
    err = "MODE:fail" in prompt
    text = "Fixed it. resumed:%s" % sid if sid and "resumed" not in prompt and sys.argv[2] else "Fixed it."
    if "MODE:ask" in prompt: text = "Which table?\\nMOEKA_STATUS: needs_user"
    elif "MODE:block" in prompt: text = "Cannot run tests.\\nMOEKA_STATUS: blocked"
    elif err: text = "You have hit your usage limit"
    elif "MODE:nostatus" not in prompt: text += "\\nMOEKA_STATUS: completed"
    emit({"type": "result", "subtype": "error_during_execution" if err else "success",
          "is_error": err, "result": text, "session_id": sid, "total_cost_usd": 0.05,
          "usage": {"input_tokens": 10, "output_tokens": 20}, "permission_denials": []})
    """
)


class FakeClaude(ClaudeDriver):
    script = ""

    def argv(self, binary, prompt, session_id, extra, disallowed):
        return [sys.executable, self.script, prompt, session_id or ""]


def _cfg(**over):
    backend = lambda enable: SimpleNamespace(  # noqa: E731
        enable=enable, binary="fake", home="", max_concurrent=2, timeout_s=3600, extra_args=[],
    )
    base = dict(claude=backend(True), codex=backend(False), sandbox=False, idle_warn_s=600,
                claude_disallowed_tools=[])
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def env(tmp_path):
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE)
    driver = FakeClaude()
    driver.script = str(script)
    ws = tmp_path / "proj"
    ws.mkdir()
    store = JobStore(tmp_path / "state" / "jobs.sqlite3")

    def make(cfg=None, roots=None):
        return WorkerManager(
            store, tmp_path / "state", cfg or _cfg(), allowed_roots=lambda: roots or [tmp_path],
            protected_roots=lambda: [], env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
            home=tmp_path, poll_s=0.05, drivers={"claude": driver, "codex": CodexDriver()},
        )

    yield SimpleNamespace(store=store, ws=ws, make=make, tmp=tmp_path)
    store.close()


async def settled(store, job_id, *states, timeout=15):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        job = store.get(job_id)
        if job.state in states:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"{job_id} stuck in {store.get(job_id).state}")


async def test_done_stores_session_report_and_usage(env):
    m = env.make()
    job = await m.start(backend="claude", workspace=str(env.ws), request="fix it",
                        session_key="telegram:9", route=("telegram", "9", ""))
    job = await settled(env.store, job.id, JobState.DONE)
    last = env.store.history(job.id)[-1]
    assert "Claude reports: Fixed it." in last.detail
    assert job.refs["backend_session"].startswith("sess-") and job.role == "worker"
    assert "claude" in m.inspect() and "1 runs" in m.inspect()
    assert "finished" in m.inspect(job.id)
    m.close()


async def test_child_of_the_owner_chat_job(env):
    parent = env.store.open_job(session_key="telegram:9", request_text="fix the export")
    env.store.transition(parent.id, JobState.WORKING)
    m = env.make()
    job = await m.start(backend="claude", workspace=str(env.ws), request="go",
                        session_key="telegram:9")
    assert [c.id for c in env.store.children(parent.id)] == [job.id]
    await settled(env.store, job.id, JobState.DONE)
    m.close()


async def test_question_then_follow_up_resumes_the_same_session(env):
    m = env.make()
    job = await m.start(backend="claude", workspace=str(env.ws), request="MODE:ask migrate it")
    job = await settled(env.store, job.id, JobState.NEEDS_ANSWER)
    assert "Which table?" in env.store.history(job.id)[-1].detail
    sid = job.refs["backend_session"]
    await m.follow_up(job.id, "the users table")
    job = await settled(env.store, job.id, JobState.DONE)
    assert f"resumed:{sid}" in env.store.history(job.id)[-1].detail  # same session id passed
    m.close()


async def test_follow_up_while_running_is_refused(env):
    m = env.make()
    job = await m.start(backend="claude", workspace=str(env.ws), request="MODE:slow")
    with pytest.raises(WorkerError) as exc:
        await m.follow_up(job.id, "more")
    assert exc.value.cause == "still_running"
    await settled(env.store, job.id, JobState.DONE)
    m.close()


async def test_blocked_and_missing_status_line(env):
    m = env.make()
    a = await m.start(backend="claude", workspace=str(env.ws), request="MODE:block")
    a = await settled(env.store, a.id, JobState.BLOCKED)
    assert env.store.history(a.id)[-1].cause == "worker_blocked"
    b = await m.start(backend="claude", workspace=str(env.ws), request="MODE:nostatus")
    await settled(env.store, b.id, JobState.DONE)
    assert "gave no status line" in env.store.history(b.id)[-1].detail
    m.close()


async def test_usage_limit_names_the_backend_and_never_falls_back(env):
    m = env.make(_cfg(codex=SimpleNamespace(enable=True, binary="fake", home="", max_concurrent=2,
                                            timeout_s=3600, extra_args=[])))
    job = await m.start(backend="claude", workspace=str(env.ws), request="MODE:fail")
    job = await settled(env.store, job.id, JobState.FAILED)
    last = env.store.history(job.id)[-1]
    assert last.side is FailureSide.WORKER and last.cause == "claude:usage_limit"
    assert env.store.list(role="worker", states={JobState.DONE}) == []  # codex was not tried
    m.close()


async def test_nonzero_exit_without_result_fails_with_stderr(env):
    m = env.make()
    job = await m.start(backend="claude", workspace=str(env.ws), request="MODE:crash")
    job = await settled(env.store, job.id, JobState.FAILED)
    last = env.store.history(job.id)[-1]
    assert last.cause == "claude:exit_3" and "segfault-ish" in last.detail
    m.close()


async def test_timeout_kills_the_worker(env):
    cfg = _cfg()
    cfg.claude.timeout_s = 1
    m = env.make(cfg)
    job = await m.start(backend="claude", workspace=str(env.ws), request="MODE:hang")
    job = await settled(env.store, job.id, JobState.FAILED)
    assert env.store.history(job.id)[-1].cause == "claude:timeout"
    row = m._row(job.id)
    assert row.observed_state == "timeout"
    m.close()


async def test_stop_kills_and_records(env):
    m = env.make()
    job = await m.start(backend="claude", workspace=str(env.ws), request="MODE:hang")
    await asyncio.sleep(0.3)
    assert "Stopped" in await m.stop(job.id)
    last = env.store.history(job.id)[-1]
    assert last.cause == "stopped_by_owner" and job.id
    m.close()


async def test_refusals_are_named_on_the_job(env):
    m = env.make()
    with pytest.raises(WorkerError) as exc:
        await m.start(backend="codex", workspace=str(env.ws), request="x")  # disabled
    assert exc.value.cause == "backend_disabled"
    with pytest.raises(WorkerError) as exc:
        await m.start(backend="claude", workspace="/etc", request="x")  # outside the grants
    assert exc.value.cause == "workspace_not_granted"
    failed = env.store.list(role="worker", states={JobState.FAILED})
    assert {env.store.history(j.id)[-1].cause for j in failed} == {
        "codex:backend_disabled", "claude:workspace_not_granted"}
    m.close()


async def test_protected_roots_and_one_writer_per_workspace(env):
    m = env.make()
    m._protected_roots = lambda: [env.ws]
    with pytest.raises(WorkerError) as exc:
        await m.start(backend="claude", workspace=str(env.ws), request="x")
    assert exc.value.cause == "workspace_protected"
    m._protected_roots = lambda: []
    first = await m.start(backend="claude", workspace=str(env.ws), request="MODE:slow")
    with pytest.raises(WorkerError) as exc:
        await m.start(backend="claude", workspace=str(env.ws), request="again")
    assert exc.value.cause == "workspace_busy"
    await settled(env.store, first.id, JobState.DONE)
    m.close()


async def test_restart_reattaches_a_live_worker_and_settles_it(env):
    m1 = env.make()
    job = await m1.start(backend="claude", workspace=str(env.ws), request="MODE:slow")
    await asyncio.sleep(0.3)
    m1.close()  # the gateway goes away; the detached worker keeps running
    m2 = env.make()
    assert await m2.reconcile() == 1
    job = await settled(env.store, job.id, JobState.DONE)
    assert "Fixed it" in env.store.history(job.id)[-1].detail
    m2.close()


async def test_restart_with_a_dead_worker_and_no_result_is_outcome_unknown(env):
    m1 = env.make()
    job = await m1.start(backend="claude", workspace=str(env.ws), request="MODE:hang")
    await asyncio.sleep(0.3)
    row = m1._row(job.id)
    m1.close()
    os.killpg(row.pid, signal.SIGKILL)
    await asyncio.sleep(0.2)
    m2 = env.make()
    await m2.reconcile()
    job = env.store.get(job.id)
    assert job.state is JobState.OUTCOME_UNKNOWN
    last = env.store.history(job.id)[-1]
    assert last.side is FailureSide.WORKER and last.cause == "worker_vanished"
    m2.close()


def test_status_line_parsing_and_request_wrapping():
    assert split_status("did it\nMOEKA_STATUS: needs_user") == ("did it", "needs_user")
    assert split_status("nothing here") == ("nothing here", None)
    wrapped = wrap_request("fix X", "uses postgres")
    assert "fix X" in wrapped and "uses postgres" in wrapped and "MOEKA_STATUS" in wrapped
    assert "verbatim" in wrapped


def test_claude_and_codex_argv_resume_by_explicit_session_id():
    claude = ClaudeDriver().argv("claude", "hi", "abc-123", [], ["Bash(git push:*)"])
    assert claude[:3] == ["claude", "-p", "hi"] and claude[-2:] == ["--resume", "abc-123"]
    assert "--permission-prompts" in claude and "--setting-sources" in claude
    assert "Bash(git push:*)" in claude
    codex = CodexDriver().argv("codex", "hi", "tid-9", [], [])
    assert codex[:4] == ["codex", "exec", "resume", "tid-9"] and codex[-1] == "hi"
    assert "--dangerously-bypass-approvals-and-sandbox" not in claude + codex


def test_driver_parsing_of_real_captured_events():
    c = ClaudeDriver()
    assert c.parse('{"type":"system","subtype":"init","session_id":"s1"}').session_id == "s1"
    res = c.parse('{"type":"result","subtype":"success","is_error":false,"result":"ok",'
                  '"session_id":"s1","total_cost_usd":0.2,"usage":{"input_tokens":2,'
                  '"output_tokens":4},"permission_denials":[],"terminal_reason":"completed"}')
    assert (res.kind, res.text, res.is_error, res.usage["cost_usd"]) == ("result", "ok", False, 0.2)
    x = CodexDriver()
    assert x.parse('{"type":"thread.started","thread_id":"t1"}').session_id == "t1"
    assert x.parse('{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"ok"}}'
                   ).kind == "message"
    assert x.parse('{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}'
                   ).kind == "result"
    assert x.parse('{"type":"turn.failed","error":{"message":"quota"}}').is_error
    assert x.parse("not json") is None
