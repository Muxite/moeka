"""Shared, session-scoped capability probes for tests that need something the
current environment might not have -- DNS/network egress, ``npx`` on PATH.

Skip on the missing *capability*, never on a hostname literal, a platform
check, or an environment-name guess: that is what lets the exact same test
run for real wherever the capability genuinely exists (a sandbox with
outbound DNS, a CI image with Node/npx installed) and skip -- loudly, with a
reason naming exactly what is missing -- everywhere it doesn't.

Before this module existed, a fixed set of "known" failures (DNS-dependent
SSRF-guard tests, npx-dependent MCP preset tests) was treated as an accepted
baseline and reported as failures every run. A permanently non-zero failure
count is exactly how a *new*, real regression hides in the noise. See
``.agent/post-sync-fixes-report.md`` for the decision this module implements.

Not a conftest (no test collection here), so it needs no ``Dockerfile.test``
change: everything under ``tests/`` already ships to the test image via
``COPY tests/ tests/`` (see ``tests/_home_guard.py`` for the same reasoning,
and the incident that established the pattern).
"""

from __future__ import annotations

import functools
import shutil
import socket

DNS_EGRESS_SKIP_REASON = "requires DNS/network egress, not available in this environment"
NPX_SKIP_REASON = "requires npx on PATH, not available in this environment"


@functools.lru_cache(maxsize=None)
def has_dns_egress(hostname: str = "example.com", *, timeout: float = 2.0) -> bool:
    """True if this process can resolve *hostname* right now.

    Cached (module import lives for the whole pytest session, so this is
    effectively a session-scoped probe) since a real DNS lookup has real
    latency and this environment's capability does not change mid-run.
    ``example.com`` is IANA's reserved-for-documentation domain -- a stable,
    neutral probe target. What's being tested is whether outbound DNS
    resolution works *at all*, not whether any specific domain is reachable.
    """
    original_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        socket.getaddrinfo(hostname, 80, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    finally:
        socket.setdefaulttimeout(original_timeout)
    return True


@functools.lru_cache(maxsize=None)
def has_npx() -> bool:
    """True if an ``npx`` executable is on PATH."""
    return shutil.which("npx") is not None
