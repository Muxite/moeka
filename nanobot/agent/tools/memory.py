"""Explicit, source-linked memory operations for daily conversations."""

# pyright: reportIncompatibleMethodOverride=false

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast
from uuid import uuid4

from nanobot.agent.tools.base import Tool, ToolResult, capability_request, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

_PARAMETERS = tool_parameters_schema(
    action=StringSchema("Operation: remember, search, correct, or forget", enum=["remember", "search", "correct", "forget"]),
    content=StringSchema("Fact or lexical search query"),
    memory_id=StringSchema("ID returned by a previous search; required for correct and forget"),
    required=["action"],
)
_WRITE_LOCK = threading.Lock()
_MAX_FACT_CHARS = 4_000
_MAX_SEARCH_RESULT_CHARS = 12_000


class MemoryFact(TypedDict):
    event: str
    id: str
    content: str
    created_at: str
    source: dict[str, str]
    supersedes: NotRequired[str]
    replaces: NotRequired[str]


def _active_facts(path: Path) -> list[MemoryFact]:
    facts: dict[str, MemoryFact] = {}
    removed: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            parsed: object = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        row = _string_mapping(parsed)
        if row is None:
            continue
        source_raw = _string_mapping(row.get("source"))
        source: dict[str, str] = {}
        if source_raw is not None:
            for key, value in source_raw.items():
                if not isinstance(value, str):
                    source = {}
                    break
                source[key] = value
        fact_id, content = row.get("id"), row.get("content")
        if (
            row.get("event") == "fact"
            and isinstance(fact_id, str)
            and isinstance(content, str)
            and source
        ):
            fact = MemoryFact(
                event="fact", id=fact_id, content=content,
                created_at=str(row.get("created_at", "")), source=source,
            )
            replaced = row.get("replaces")
            if isinstance(replaced, str):
                removed.add(replaced)
                fact["replaces"] = replaced
            supersedes = row.get("supersedes")
            if isinstance(supersedes, str):
                fact["supersedes"] = supersedes
            facts[fact["id"]] = fact
        elif row.get("event") in {"forget", "supersede"} and isinstance(row.get("id"), str):
            event_id = row.get("id")
            if isinstance(event_id, str):
                removed.add(event_id)
    return [fact for key, fact in facts.items() if key not in removed]


def _append(path: Path, row: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _WRITE_LOCK, path.open("a", encoding="utf-8") as stream:
        stream.write(line)
        stream.flush()
        import os

        os.fsync(stream.fileno())


@tool_parameters(_PARAMETERS)
class MemoryTool(Tool):
    """Store, search, correct, or forget a durable fact with provenance."""

    _capability_names = frozenset({"fs.read", "fs.write"})

    def __init__(self, workspace: Path) -> None:
        self._path = workspace / "memory" / "explicit.jsonl"

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return True

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(Path(ctx.workspace).expanduser().resolve())

    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return "Remember a durable fact, search saved facts, correct an outdated fact, or forget one. Searches use lexical matching and work without embeddings. Returned IDs and source references let later corrections and deletions target the right fact."

    def capabilities(self, params: dict[str, Any]) -> list[Any]:
        action = params.get("action")
        requests = [capability_request("fs.read", str(self._path))]
        if action != "search":
            requests.append(capability_request("fs.write", str(self._path)))
        return requests

    async def execute(
        self,
        action: str,
        content: str | None = None,
        memory_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        return await asyncio.to_thread(self._execute_sync, action, content, memory_id)

    def _execute_sync(self, action: str, content: str | None, memory_id: str | None) -> str:
        text = (content or "").strip()
        facts = _active_facts(self._path)
        if action == "search":
            if not text:
                return ToolResult.error("Error: content is required for search")
            terms = [term.casefold() for term in re.findall(r"\w+", text) if len(term) > 1]
            if not terms:
                return ToolResult.error("Error: search needs at least one word with two or more characters")
            if len(text) > 500:
                return ToolResult.error("Error: search query is limited to 500 characters")
            matches = [fact for fact in facts if all(term in fact["content"].casefold() for term in terms)]
            matches.sort(key=lambda fact: fact.get("created_at", ""), reverse=True)
            bounded: list[MemoryFact] = []
            output = "[]"
            for fact in matches[:20]:
                candidate = [*bounded, fact]
                serialized = json.dumps(candidate, ensure_ascii=False)
                if len(serialized) > _MAX_SEARCH_RESULT_CHARS:
                    break
                bounded = candidate
                output = serialized
            return output
        if action == "remember":
            if not text:
                return ToolResult.error("Error: content is required for remember")
            if len(text) > _MAX_FACT_CHARS:
                return ToolResult.error(f"Error: facts are limited to {_MAX_FACT_CHARS} characters")
            row = self._new_fact(text)
            _append(self._path, row)
            return json.dumps({"id": row["id"], "source": row["source"]}, ensure_ascii=False)
        if action in {"correct", "forget"}:
            if not memory_id:
                return ToolResult.error(f"Error: memory_id is required for {action}")
            old = next((fact for fact in facts if fact["id"] == memory_id), None)
            if old is None:
                return ToolResult.error(f"Error: active memory {memory_id!r} was not found")
            if action == "forget":
                _append(self._path, {"event": "forget", "id": memory_id, "at": _now()})
                return f"Forgot memory {memory_id}."
            if not text:
                return ToolResult.error("Error: content is required for correct")
            if len(text) > _MAX_FACT_CHARS:
                return ToolResult.error(f"Error: facts are limited to {_MAX_FACT_CHARS} characters")
            row = self._new_fact(text, supersedes=memory_id)
            row["replaces"] = memory_id
            _append(self._path, row)
            return json.dumps({"id": row["id"], "supersedes": memory_id, "source": row["source"]}, ensure_ascii=False)
        return ToolResult.error(f"Error: unsupported memory action {action!r}")

    def _new_fact(self, content: str, *, supersedes: str | None = None) -> MemoryFact:
        request = current_request_context()
        source: dict[str, str] = {"kind": "direct"}
        if request is not None:
            source.update({"channel": request.channel, "chat_id": request.chat_id})
            if request.session_key:
                source["session_key"] = request.session_key
            if request.message_id:
                source["message_id"] = request.message_id
        row = MemoryFact(
            event="fact", id=uuid4().hex, content=content,
            created_at=_now(), source=source,
        )
        if supersedes:
            row["supersedes"] = supersedes
        return row


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _string_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    raw_dict = cast(dict[object, object], value)
    result: dict[str, object] = {}
    for key, item in raw_dict.items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result
