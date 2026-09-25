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
    "nanobot.webui",
    "nanobot.gateway",
    "nanobot.pairing",
    "nanobot.audio",
    "nanobot.cron",
    "nanobot.triggers",
    "nanobot.apps",
    "nanobot.cli",
    "nanobot.optional_features",
)

_PROBE = """
import sys
import nanobot.core            # noqa: F401
import nanobot.core.vec        # noqa: F401
import nanobot.core.vec_store  # noqa: F401
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


_WEBUI_PROBE = """
import sys
import nanobot.agent.loop        # noqa: F401
import nanobot.session.recovery  # noqa: F401
import nanobot.agent.tools.sessions  # noqa: F401
import nanobot.utils.restart     # noqa: F401
leaked = sorted(
    m for m in sys.modules if m == "nanobot.webui" or m.startswith("nanobot.webui.")
)
print(",".join(leaked))
"""


def test_agent_loop_and_session_recovery_do_not_load_webui():
    """The agent loop and session recovery no longer depend on the WebUI package."""
    proc = subprocess.run(
        [sys.executable, "-c", _WEBUI_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    leaked = [m for m in proc.stdout.strip().split(",") if m]
    assert leaked == [], f"agent loop / session recovery import loaded webui: {leaked}"


_TRIGGERS_PROBE = """
import sys
import nanobot.agent.loop  # noqa: F401
leaked = sorted(
    m for m in sys.modules if m == "nanobot.triggers" or m.startswith("nanobot.triggers.")
)
print(",".join(leaked))
"""


def test_agent_loop_does_not_load_triggers():
    """The agent loop no longer depends on the local-triggers package."""
    proc = subprocess.run(
        [sys.executable, "-c", _TRIGGERS_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    leaked = [m for m in proc.stdout.strip().split(",") if m]
    assert leaked == [], f"agent loop import loaded triggers: {leaked}"
