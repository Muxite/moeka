"""Session tools are read-only views over the canonical session store."""

from __future__ import annotations

import json

import pytest

from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.sessions import ReadSessionTool, SearchSessionsTool
from nanobot.session.manager import SessionManager


@pytest.mark.asyncio
async def test_session_tools_preserve_durable_sources_across_restart(tmp_path):
    manager = SessionManager(tmp_path)
    key = "websocket:history"
    session = manager.get_or_create(key)
    session.metadata.update({"title": "Project notes", "title_user_edited": True})
    session.add_message("user", "canonical needle")
    manager.save(session)

    def durable_files():
        return {
            path: path.read_bytes()
            for path in manager.sessions_dir.rglob("*.jsonl")
        }

    original = durable_files()
    for _ in range(2):
        manager = SessionManager(tmp_path)
        with request_context(RequestContext(
            channel="websocket", chat_id="current", session_key="websocket:current",
        )):
            search = json.loads(await SearchSessionsTool(manager).execute(query="needle"))
            read = await ReadSessionTool(manager).execute(session_key=key, query="needle")
        assert [row["session_key"] for row in search["results"]] == [key]
        assert [item["content"] for item in json.loads(read)["messages"]] == [
            "canonical needle"
        ]
        assert durable_files() == original
