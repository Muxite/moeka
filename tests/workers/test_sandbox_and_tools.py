"""Worker bwrap wrapper and the four worker tools (spec 015)."""

import shutil
import subprocess

import pytest

from nanobot.agent.tools.context import (
    RequestContext,
    ToolContext,
    bind_request_context,
    reset_request_context,
)
from nanobot.agent.tools.worker import (
    WorkerFollowUpTool,
    WorkerInspectTool,
    WorkerStartTool,
    WorkerStopTool,
)
from nanobot.config.schema import ToolsConfig
from nanobot.kernel.jobs import JobStore
from nanobot.workers.manager import WorkerError
from nanobot.workers.sandbox import worker_bwrap_argv


def test_bwrap_argv_has_no_die_with_parent_and_binds_only_what_is_granted(tmp_path):
    ws, home, login = tmp_path / "proj", tmp_path / "home", tmp_path / "home" / ".claude"
    for d in (ws, home, login):
        d.mkdir()
    argv = worker_bwrap_argv(["claude", "-p", "x"], workspace=ws, home=login, state_home=home,
                             env={"HOME": str(home)})
    assert argv[0] == "bwrap" and "--die-with-parent" not in argv
    assert argv[argv.index("--tmpfs", argv.index("--dev")) + 1] == "/tmp"
    pairs = list(zip(argv, argv[1:], argv[2:]))
    assert ("--tmpfs", str(home), "--bind") in pairs or ("--tmpfs", str(home)) in zip(argv, argv[1:])
    assert ("--bind", str(ws), str(ws)) in pairs
    assert ("--bind", str(login), str(login)) in pairs
    assert argv[-3:] == ["claude", "-p", "x"] and "--chdir" in argv


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
def test_real_bwrap_workspace_writable_dotenv_masked_other_home_hidden(tmp_path):
    home = tmp_path / "home"
    ws = home / "projects" / "app"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id").write_text("PRIVATE")
    (home / "other").mkdir()
    (home / "other" / "x.txt").write_text("elsewhere")
    ws.mkdir(parents=True)
    (ws / ".env").write_text("SECRET=1")
    login = home / ".claude"
    login.mkdir()
    script = (
        "echo ok > made.txt; "
        "cat .env | wc -c; "
        "cat ../../.ssh/id 2>&1 | head -c 20; echo; "
        "ls ../../other 2>&1 | head -c 40; echo; "
        "ls ../../.claude | wc -l"
    )
    argv = worker_bwrap_argv(["sh", "-c", script], workspace=ws, home=login, state_home=home)
    probe = worker_bwrap_argv(["/usr/bin/true"], workspace=ws, home=login, state_home=home)
    if subprocess.run(probe, capture_output=True).returncode != 0:
        pytest.skip("user namespaces unavailable")
    out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert (ws / "made.txt").read_text().strip() == "ok", out.stderr
    lines = out.stdout.splitlines()
    assert lines[0].strip() == "0"  # .env is an empty file
    assert "PRIVATE" not in out.stdout
    assert "elsewhere" not in out.stdout and "x.txt" not in out.stdout
    assert (ws / ".env").read_text() == "SECRET=1"  # host file untouched


class FakeManager:
    def __init__(self, store, backends=("claude",)):
        self.store, self._b, self.calls = store, list(backends), []

    def enabled_backends(self):
        return self._b

    async def start(self, **kw):
        self.calls.append(("start", kw))
        if kw["backend"] == "codex":
            raise WorkerError("backend_disabled", "codex workers are disabled in config.")
        return self.store.open_job(session_key=kw["session_key"], request_text=kw["request"],
                                   role="worker")

    async def follow_up(self, job_id, message):
        self.calls.append(("follow_up", job_id, message))

    async def stop(self, job_id):
        return f"Stopped worker {job_id}."

    def inspect(self, job_id=""):
        return f"inspect:{job_id}"


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.sqlite3")
    yield s
    s.close()


def _ctx(manager, host=True):
    return ToolContext(config=ToolsConfig(), workspace="/tmp", host_tools=host,
                       worker_manager=manager)


def test_tools_load_only_for_host_loops_with_an_enabled_backend(store):
    assert WorkerStartTool.enabled(_ctx(FakeManager(store)))
    assert not WorkerStartTool.enabled(_ctx(FakeManager(store), host=False))
    assert not WorkerStartTool.enabled(_ctx(FakeManager(store, backends=())))
    assert not WorkerStartTool.enabled(_ctx(None))


@pytest.mark.asyncio
async def test_start_uses_the_chat_turns_session_and_route(store):
    parent = store.open_job(session_key="telegram:9:topic:4", request_text="fix export",
                            route=("telegram", "9", "4"))
    store.transition(parent.id, __import__("nanobot.kernel.jobs", fromlist=["JobState"]).JobState.WORKING)
    m = FakeManager(store)
    tool = WorkerStartTool.create(_ctx(m))
    token = bind_request_context(RequestContext(channel="telegram", chat_id="9",
                                                session_key="telegram:9:topic:4"))
    try:
        out = await tool.execute(backend="claude", workspace="/p", request="fix the export")
    finally:
        reset_request_context(token)
    kw = m.calls[0][1]
    assert kw["session_key"] == "telegram:9:topic:4" and kw["route"] == ("telegram", "9", "4")
    assert "Job id:" in out and "no automatic" not in out


@pytest.mark.asyncio
async def test_refusals_come_back_as_named_errors(store):
    out = await WorkerStartTool.create(_ctx(FakeManager(store))).execute(
        backend="codex", workspace="/p", request="x")
    assert "Error" in out and "backend_disabled" in out


@pytest.mark.asyncio
async def test_follow_up_inspect_and_stop_delegate(store):
    m = FakeManager(store)
    assert "same session" in await WorkerFollowUpTool.create(_ctx(m)).execute(
        job_id="abc", message="the users table")
    assert m.calls[-1] == ("follow_up", "abc", "the users table")
    assert await WorkerInspectTool.create(_ctx(m)).execute(job_id="abc") == "inspect:abc"
    assert WorkerInspectTool.create(_ctx(m)).read_only
    assert "Stopped worker abc" in await WorkerStopTool.create(_ctx(m)).execute(job_id="abc")
