"""Model calls: ``kernel.llm.generate`` / ``complete`` / ``complete_json`` / ``stream`` / ``batch``.

``SolverRegistry`` / ``Solved``: a host builds its own registry for
``Kernel(env, solvers=...)`` (the deterministic fast path of ``complete_json``).
"""

from nanobot.kernel.llm import (
    LLM,
    BatchResult,
    Completion,
    GenerateOptions,
    Request,
    TextStream,
    Usage,
)
from nanobot.kernel.messages import assistant, image_part, system, user
from nanobot.kernel.sampling import Sampling
from nanobot.kernel.solvers import Solved, SolverRegistry

__all__ = [
    "LLM",
    "BatchResult",
    "Completion",
    "GenerateOptions",
    "Request",
    "Sampling",
    "Solved",
    "SolverRegistry",
    "TextStream",
    "Usage",
    "assistant",
    "image_part",
    "system",
    "user",
]
