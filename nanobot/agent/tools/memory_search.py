"""MemorySearchTool — semantic search over vector memory (Moeka extension).

Registered by :class:`nanobot.agent.loop.AgentLoop` when
``config.agents.defaults.vector_memory.enabled`` is ``true``.  Requires the
``[vec]`` optional dependency group::

    pip install "moeka[vec]"
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters

if TYPE_CHECKING:
    from nanobot.agent.memory_vec import VectorMemoryStore


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language description of what you want to recall.",
                "minLength": 1,
            },
            "k": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 5).",
                "minimum": 1,
                "maximum": 20,
            },
            "scope": {
                "type": "string",
                "enum": ["all", "memory", "history", "skills"],
                "description": (
                    'Where to search: "memory" (MEMORY.md), "history" (conversation history), '
                    '"skills" (skill descriptions), or "all" (default).'
                ),
            },
        },
        "required": ["query"],
    }
)
class MemorySearchTool(Tool):
    """Semantic similarity search over embedded memory, history, and skills.

    Use this tool to recall past context, find relevant long-term memories, or
    discover available skills by natural-language description.  It complements
    the ``grep`` tool — prefer ``memory_search`` for fuzzy/conceptual recall and
    ``grep`` for exact-text searches.
    """

    def __init__(self, vec_store: VectorMemoryStore) -> None:
        self._vec = vec_store

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Semantic (embedding-based) search over your long-term memory, conversation history, "
            "and skill descriptions.  Returns the most relevant chunks ranked by similarity.\n\n"
            "Scopes:\n"
            '- "memory"  — chunks of your MEMORY.md long-term memory file\n'
            '- "history" — past conversation entries\n'
            '- "skills"  — available skill names and descriptions\n'
            '- "all"     — all of the above (default)\n\n'
            "Use this when grep would miss paraphrased or semantically related content."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, query: str, k: int = 5, scope: str = "all") -> str:
        if not query.strip():
            return "Error: query must not be empty."

        results = self._vec.semantic_search(query, k=k, scope=scope)
        if not results:
            return "No results found. The index may be empty — try running Dream first to populate it."

        lines = [f"Semantic search results for: {query!r}  (scope={scope}, k={k})\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"--- Result {i} [{r.source}] score={r.score:.3f} ---")
            lines.append(r.text.strip())
            lines.append("")
        return "\n".join(lines)
