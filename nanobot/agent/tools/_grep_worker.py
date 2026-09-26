"""Regex-only worker for grep patterns that may be expensive to match.

CPython's ``re`` never releases the GIL while matching, so a runaway regex cannot
be timed out from a thread: it freezes the whole event loop. ``GrepTool`` therefore
sends such patterns here, to a child process it can kill.

This file is deliberately stdlib-only and is run BY PATH (``python -I -S <this file>``)
with an empty environment and ``-S`` (no ``site``), so nothing from the parent's cwd, ``PYTHONPATH`` or
secrets is reachable. The worker never touches the filesystem: the parent reads and
decodes the files and only sends text.

Protocol (one JSON object per line, both directions):
  request 1:  {"pattern": str, "flags": int}           (no reply)
  request N:  {"texts": [str, ...]}
  reply N:    {"starts": [int, ...]}  (match start per text, -1 for no match)
              or {"error": str}
"""

import json
import re
import sys


def main() -> int:
    stdin = sys.stdin.buffer
    out = sys.stdout.buffer
    setup = json.loads(stdin.readline())
    try:
        regex = re.compile(setup["pattern"], setup["flags"])
    except re.error as exc:
        regex = None
        setup_error = str(exc)
    for raw in stdin:
        if regex is None:
            reply = {"error": f"invalid regex pattern: {setup_error}"}
        else:
            texts = json.loads(raw.decode("utf-8", "surrogatepass"))["texts"]
            starts = []
            for text in texts:
                match = regex.search(text)
                starts.append(match.start() if match else -1)
            reply = {"starts": starts}
        out.write(json.dumps(reply).encode("ascii") + b"\n")
        out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
