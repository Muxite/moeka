"""moeka kernel: public package name for the embeddable engine.

Re-exports the :mod:`nanobot.core` surface; ``MoekaKernel`` is ``MoekaCore``.
Also exports the host-contract types (stdlib + loguru only).
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
from nanobot.kernel.env import (
    ConfigSource,
    CoreEnvironment,
    CredentialResolver,
    Paths,
    PathsOverlapError,
    StaticCredentialResolver,
)
from nanobot.kernel.trace import LoguruTraceSink, NullTraceSink, TraceSink, safe_emit

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
    "Paths",
    "PathsOverlapError",
    "CredentialResolver",
    "StaticCredentialResolver",
    "ConfigSource",
    "CoreEnvironment",
    "TraceSink",
    "NullTraceSink",
    "LoguruTraceSink",
    "safe_emit",
]
