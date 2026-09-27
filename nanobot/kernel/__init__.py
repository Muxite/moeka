"""moeka kernel: public package name for the embeddable engine.

Re-exports the :mod:`nanobot.core` surface; ``MoekaKernel`` is ``MoekaCore``.
Adds no imports of its own.
"""

from nanobot.core import (
    Config,
    FunctionTool,
    MoekaCore,
    MoekaKernel,
    RetrievedChunk,
    RunResult,
    acomplete,
    acomplete_json,
    complete,
    complete_json,
    open_vec_store,
)

__all__ = [
    "MoekaKernel",
    "MoekaCore",
    "Config",
    "FunctionTool",
    "RetrievedChunk",
    "RunResult",
    "complete",
    "acomplete",
    "complete_json",
    "acomplete_json",
    "open_vec_store",
]
