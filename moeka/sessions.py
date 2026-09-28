"""Sessions: ``kernel.sessions`` handles that append, checkpoint, fork and rewind.

A :class:`Session` is a host-ordered conversation in the kernel's store; pass it (or
its key) as ``agent.run(..., session=...)``. ``rewind``/``fork``/``delete`` on a key
with a run in progress raise :class:`SessionBusyError`; rewinding to a checkpoint
whose history has diverged raises :class:`CheckpointMismatch`.
"""

from nanobot.kernel.sessions import (
    Checkpoint,
    CheckpointMismatch,
    Session,
    SessionBusyError,
    SessionInfo,
    Sessions,
    SessionSnapshot,
)

__all__ = [
    "Checkpoint",
    "CheckpointMismatch",
    "Session",
    "SessionBusyError",
    "SessionInfo",
    "SessionSnapshot",
    "Sessions",
]
