"""Guard: importing moeka-core must not drag in channel/gateway runtime deps.

`from nanobot.core import MoekaCore` is the embeddable surface — it promises no
channel/gateway/webui imports (all heavy paths are lazy). This locks that in so a
stray top-level import (e.g. a channel or the websocket gateway) is caught here
instead of inflating a host's dependency footprint.
"""

from __future__ import annotations

import subprocess
import sys

# Package prefixes that belong to the chat-bot runtime, not the core.
_FORBIDDEN = (
    "nanobot.channels",
    "nanobot.web",
    "nanobot.gateway",
    "nanobot.heartbeat",
    "nanobot.pairing",
    "nanobot.cli",
)

_PROBE = """
import sys
import nanobot.core            # noqa: F401
import nanobot.api.complete    # noqa: F401
forbidden = {forbidden!r}
leaked = sorted(
    m for m in sys.modules
    if any(m == p or m.startswith(p + ".") for p in forbidden)
)
print(",".join(leaked))
"""


def test_core_import_has_no_runtime_deps():
    """A fresh interpreter importing nanobot.core pulls in no runtime packages."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(forbidden=_FORBIDDEN)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    leaked = [m for m in proc.stdout.strip().split(",") if m]
    assert leaked == [], f"moeka-core import leaked runtime modules: {leaked}"
