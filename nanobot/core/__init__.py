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

Files are optional. The core consumes only the pydantic :class:`Config` data
object; reading a ``config.json`` is just one way to produce one::

    from nanobot.core import MoekaCore, Config

    config = Config.model_validate({"providers": {...}, "agents": {...}})
    core = MoekaCore.from_config(config, workspace="/tmp/my-agent")

``MoekaCore.create(config_dict=...)`` does the same in one call and, when no
workspace is given, runs in a throwaway temp dir instead of ``~/.nanobot``.

All heavy imports are lazy; importing this package has no channel/gateway deps.
"""

from nanobot.api.complete import acomplete, complete
from nanobot.config.schema import Config
from nanobot.core.core import MoekaCore
from nanobot.core.function_tool import FunctionTool
from nanobot.core.vec import RetrievedChunk, open_vec_store
from nanobot.nanobot import RunResult

__all__ = [
    "MoekaCore",
    "Config",
    "FunctionTool",
    "RetrievedChunk",
    "RunResult",
    "complete",
    "acomplete",
    "open_vec_store",
]
