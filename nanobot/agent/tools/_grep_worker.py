"""Child-process entry point for grep patterns that may backtrack catastrophically.

CPython's ``re`` never releases the GIL while matching, so a runaway regex
cannot be timed out from a thread: it freezes the whole event loop. For the
patterns ``GrepTool`` flags as risky the scan runs here instead, in a process
the parent can kill on timeout.

Protocol: the parent writes one pickle ``(tool, scope, kwargs)`` to stdin. The
child imports and unpickles it, prints ``READY\\n`` on stdout, runs the scan
and writes one pickle ``("ok" | "err", str)`` after it. Stray prints go to
stderr so the framing on stdout stays clean.
"""

from __future__ import annotations

import pickle
import sys


def main() -> int:
    out = sys.stdout.buffer
    sys.stdout = sys.stderr
    tool, scope, kwargs = pickle.loads(sys.stdin.buffer.read())

    from nanobot.security.workspace_access import bind_workspace_scope

    if scope is not None:
        bind_workspace_scope(scope)
    out.write(b"READY\n")
    out.flush()
    try:
        message = ("ok", tool._execute_sync(deadline=None, **kwargs))
    except BaseException as exc:  # noqa: BLE001 - reported to the parent verbatim
        message = ("err", f"{type(exc).__name__}: {exc}")
    out.write(pickle.dumps(message))
    out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
