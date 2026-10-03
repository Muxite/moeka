"""Certificate enforcement for workspace capabilities (spec 012 FR-006, FR-007).

:class:`CertGate` turns :func:`~nanobot.capabilities.provenance.verify_bundle` verdicts
into a status per workspace bundle and decides whether the bundle may load:

- ``certified``: the certificate verified (``cert_id`` set);
- ``uncertified``: no ``CERT.json`` and enforcement is off;
- ``refused:<reason>``: any other verdict (with enforcement on, ``no-cert`` too).

With ``enforce_certs`` off every bundle loads (today's behaviour) and only the status is
reported. Built-in, inline and plugin skills never pass through the gate (their status
is ``builtin``). Verdicts are cached per bundle by a stat stamp of its tree and of the
trust dirs, so a byte flip, a rename or a new revocation is seen on the next check.
A refusal is logged once per process per ``(name, reason)``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.capabilities.provenance import (
    TrustStore,
    Verdict,
    bundle_stamp,
    trust_stamp,
    verify_bundle,
)

CERTIFIED = "certified"
UNCERTIFIED = "uncertified"
BUILTIN = "builtin"
REFUSED_PREFIX = "refused:"

_LOGGED: set[tuple[str, str]] = set()
_LOGGED_GUARD = threading.Lock()


@dataclass(frozen=True)
class Provenance:
    """A workspace bundle's provenance: its status, cert id and whether it may load."""

    status: str
    cert_id: str | None
    allowed: bool
    verdict: Verdict | None = None


BUILTIN_PROVENANCE = Provenance(status=BUILTIN, cert_id=None, allowed=True)


def status_for(verdict: Verdict, *, enforce: bool) -> str:
    """The FR-006 status string of *verdict*."""
    if verdict.ok:
        return CERTIFIED
    if verdict.reason == "no-cert" and not enforce:
        return UNCERTIFIED
    return REFUSED_PREFIX + verdict.reason


def log_refusal(kind: str, name: str, reason: str, bundle: Path) -> None:
    """Warn once per process per ``(name, reason)`` about a refused capability."""
    key = (name, reason)
    with _LOGGED_GUARD:
        if key in _LOGGED:
            return
        _LOGGED.add(key)
    logger.warning(
        "capabilities: refusing workspace {} '{}': certificate check failed ({}) [{}]",
        kind, name, reason, bundle,
    )


def capabilities_settings(raw: Any) -> tuple[bool, tuple[str, ...]]:
    """``(enforce_certs, trust_dirs)`` from a ``CapabilitiesConfig``, a mapping or ``None``."""
    if raw is None:
        return False, ()
    if isinstance(raw, Mapping):
        enforce = raw.get("enforce_certs", raw.get("enforceCerts", False))
        dirs = raw.get("trust_dirs", raw.get("trustDirs", ())) or ()
    else:
        enforce = getattr(raw, "enforce_certs", False)
        dirs = getattr(raw, "trust_dirs", ()) or ()
    if not isinstance(dirs, (list, tuple)):
        dirs = ()
    return bool(enforce is True), tuple(str(item) for item in dirs)


class CertGate:
    """Verify workspace bundles against a trust store, with a stat-stamped cache.

    *trust* pins a fixed :class:`TrustStore` (embedders, tests); otherwise the store is
    loaded from the package trust dir plus *trust_dirs* and reloaded whenever a key or
    revocation file changes.
    """

    def __init__(
        self,
        *,
        enforce: bool = False,
        trust_dirs: Iterable[str | Path] = (),
        trust: TrustStore | None = None,
    ) -> None:
        self.enforce = bool(enforce)
        self.trust_dirs = tuple(str(Path(str(item)).expanduser()) for item in trust_dirs)
        self._fixed_trust = trust
        self._trust: TrustStore | None = None
        self._trust_stamp: tuple[Any, ...] | None = None
        self._cache: dict[str, tuple[Any, Any, Verdict]] = {}
        self._lock = threading.RLock()

    def trust_store(self) -> TrustStore:
        """The current trust store (reloaded when its files changed)."""
        if self._fixed_trust is not None:
            return self._fixed_trust
        with self._lock:
            stamp = trust_stamp(self.trust_dirs)
            if self._trust is None or stamp != self._trust_stamp:
                self._trust = TrustStore.load(self.trust_dirs)
                self._trust_stamp = stamp
            return self._trust

    def verdict(self, bundle: Path) -> Verdict:
        """:func:`verify_bundle` for *bundle*, cached until its tree or the trust changes."""
        trust = self.trust_store()
        key = str(bundle)
        stamp = bundle_stamp(bundle)
        with self._lock:
            cached = self._cache.get(key)
            if (
                cached is not None and stamp is not None
                and cached[0] == stamp and cached[1] is trust
            ):
                return cached[2]
        result = verify_bundle(bundle, trust)
        with self._lock:
            self._cache[key] = (stamp, trust, result)
        return result

    def check(self, bundle: Path, *, kind: str, name: str | None = None) -> Provenance:
        """The provenance of a workspace bundle; logs a refusal that blocks loading."""
        verdict = self.verdict(bundle)
        status = status_for(verdict, enforce=self.enforce)
        allowed = verdict.ok or not self.enforce
        if not allowed:
            log_refusal(kind, name or bundle.name, verdict.reason, bundle)
        return Provenance(status=status, cert_id=verdict.cert_id, allowed=allowed,
                          verdict=verdict)


__all__ = [
    "BUILTIN",
    "BUILTIN_PROVENANCE",
    "CERTIFIED",
    "REFUSED_PREFIX",
    "UNCERTIFIED",
    "CertGate",
    "Provenance",
    "capabilities_settings",
    "log_refusal",
    "status_for",
]
