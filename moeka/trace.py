"""Trace: where the kernel's structured events go.

``kernel.trace`` is a :class:`Tracer`: ``span(name, **tags)`` (sync or async context
manager), ``subscribe(event, fn)``. Every event carries ``trace_id``, ``span``,
``tags`` and ``ts``; :data:`EVENTS` lists the event names and their payload keys;
:func:`args_digest` is the ``tool.call.args_digest`` function.
"""

from nanobot.kernel.trace import (
    EVENTS,
    FanoutSink,
    JsonlTraceSink,
    LoguruTraceSink,
    MemoryTraceSink,
    NullTraceSink,
    Tracer,
    TraceSink,
)
from nanobot.kernel.trace_hook import args_digest

__all__ = [
    "EVENTS",
    "FanoutSink",
    "JsonlTraceSink",
    "LoguruTraceSink",
    "MemoryTraceSink",
    "NullTraceSink",
    "TraceSink",
    "Tracer",
    "args_digest",
]
