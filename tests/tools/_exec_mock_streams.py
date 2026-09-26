"""Mock-process helpers for ExecTool tests that fake the one-shot subprocess.

One-shot exec reads ``process.stdout`` / ``process.stderr`` incrementally
(bounded capture) instead of calling ``process.communicate()``, so a mocked
process needs stream objects with an async ``read(n)``.
"""

from __future__ import annotations

from typing import Any


class FakeStream:
    """Minimal ``asyncio.StreamReader`` stand-in: yields *data*, then EOF."""

    def __init__(self, data: bytes = b"", error: BaseException | None = None) -> None:
        self._data = data
        self._error = error

    async def read(self, n: int = -1) -> bytes:
        if self._error is not None:
            raise self._error
        if n < 0:
            n = len(self._data)
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


def set_output(proc: Any, stdout: bytes = b"", stderr: bytes = b"") -> None:
    """Make a mocked process emit *stdout* / *stderr* and then EOF."""
    proc.stdout = FakeStream(stdout)
    proc.stderr = FakeStream(stderr)


def set_read_error(proc: Any, error: BaseException) -> None:
    """Make reading a mocked process's output raise *error* (timeout, cancel, OSError)."""
    proc.stdout = FakeStream(error=error)
    proc.stderr = FakeStream(error=error)
