from __future__ import annotations

import json
from pathlib import Path

from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.memory import MemoryTool


def test_remember_search_correct_forget_preserves_source_references(tmp_path: Path) -> None:
    tool = MemoryTool(tmp_path)
    context = RequestContext(
        channel="telegram", chat_id="owner", message_id="m-17", session_key="telegram:owner",
    )

    with request_context(context):
        saved = json.loads(tool._execute_sync("remember", "The user prefers short answers", None))
    assert saved["id"]
    assert saved["source"]["message_id"] == "m-17"

    result = json.loads(tool._execute_sync("search", "prefers short", None))
    assert result[0]["id"] == saved["id"]
    assert result[0]["source"]["session_key"] == "telegram:owner"

    corrected = json.loads(tool._execute_sync("correct", "The user prefers detailed answers", saved["id"]))
    result = json.loads(tool._execute_sync("search", "prefers", None))
    assert len(result) == 1
    assert result[0]["id"] == corrected["id"]
    assert result[0]["supersedes"] == saved["id"]

    assert "Forgot memory" in tool._execute_sync("forget", None, corrected["id"])
    assert json.loads(tool._execute_sync("search", "prefers", None)) == []


def test_lexical_search_skips_malformed_rows_and_returns_no_embeddings_needed(tmp_path: Path) -> None:
    tool = MemoryTool(tmp_path)
    tool._path.parent.mkdir(parents=True)
    tool._path.write_text("bad json\n", encoding="utf-8")
    tool._execute_sync("remember", "Project uses SQLite", None)
    assert json.loads(tool._execute_sync("search", "sqlite", None))[0]["content"] == "Project uses SQLite"


def test_memory_tool_declares_workspace_file_effects(tmp_path: Path) -> None:
    tool = MemoryTool(tmp_path)
    assert tool.capability_surface() == frozenset({"fs.read", "fs.write"})
    assert [request.capability for request in tool.capabilities({"action": "search"})] == [
        "fs.read",
    ]
    assert [
        request.capability for request in tool.capabilities({"action": "remember"})
    ] == ["fs.read", "fs.write"]
