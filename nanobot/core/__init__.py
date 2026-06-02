"""moeka-core — a reusable RAG/agentic "thinking core".

Embed moeka's agent engine in your own Python code: register plain functions as
actions, ingest documents for retrieval, and run a multi-step tool-calling loop
with memory and RAG — without the chat-bot runtime (channels, gateway, WebUI).

    from nanobot.core import MoekaCore

    core = MoekaCore.create()

    @core.action
    def add(a: int, b: int) -> str:
        "Add two numbers."
        return str(a + b)

    result = await core.run("what is 2 + 3?")

All heavy imports are lazy; importing this package has no channel/gateway deps.
"""

from nanobot.api.complete import acomplete, complete
from nanobot.core.core import MoekaCore
from nanobot.core.function_tool import FunctionTool
from nanobot.nanobot import RunResult

__all__ = [
    "MoekaCore",
    "FunctionTool",
    "RunResult",
    "complete",
    "acomplete",
]
