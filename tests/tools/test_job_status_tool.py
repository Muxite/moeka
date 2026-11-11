"""job_status reads the record, never memory (spec 015)."""

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.job_status import JobStatusTool
from nanobot.config.schema import ToolsConfig
from nanobot.kernel.jobs import FailureSide, JobState, JobStore


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.sqlite3")
    yield s
    s.close()


def _ctx(store, host=True):
    return ToolContext(config=ToolsConfig(), workspace="/tmp", job_store=store, host_tools=host)


def test_only_loads_for_host_loops_with_a_store(store):
    assert JobStatusTool.enabled(_ctx(store))
    assert not JobStatusTool.enabled(_ctx(store, host=False))
    assert not JobStatusTool.enabled(_ctx(None))


@pytest.mark.asyncio
async def test_lists_active_and_recent_then_details_children(store):
    parent = store.open_job(session_key="s", request_text="ship the thing")
    child = store.open_job(session_key="s", request_text="worker", role="worker")
    store.link_child(parent.id, child.id, "worker")
    store.transition(child.id, JobState.FAILED, side=FailureSide.WORKER, cause="exit_1")
    tool = JobStatusTool.create(_ctx(store))
    listing = await tool.execute()
    assert "Active:" in listing and parent.id in listing and "Recent:" in listing
    detail = await tool.execute(job_id=parent.id)
    assert f"child worker {child.id}: failed" in detail
    assert "Error" in await tool.execute(job_id="nope")
