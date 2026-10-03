"""Capability provenance: bundle hashes, trust stores and certificate checks (spec 012).

A **bundle** is a capability directory: ``<workspace>/skills/<name>/`` (``SKILL.md``)
or ``<workspace>/tools-script/<name>/`` (``tool.yaml``). Its ``CERT.json`` holds::

    {"cert": {...moeka.capcert.v1...}, "sig": "<base64 Ed25519 signature>"}

where ``sig`` signs the canonical JSON of ``cert``. Moeka only VERIFIES: this module
reads public keys and has no code path for private keys. Certificates are issued
outside moeka (the RSI harness's host-side verifier holds the key).

- :func:`bundle_sha256`: sha256 of the canonical JSON ``[[relpath, sha256hex], ...]``
  over every regular file except the root ``CERT.json``; a symlink, device, fifo or
  socket anywhere in the bundle raises :class:`BundleError`.
- :class:`TrustStore`: trusted Ed25519 public keys (``*.pub``) and revocations
  (``revoked.jsonl``) from trust directories.
- :func:`verify_bundle`: the FR-002 checks in their fixed order; the first failure is
  the :class:`Verdict` reason.

Kernel code: imports no host package.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.capabilities.manifest import IOSpec, ManifestError, parse_io

CERT_FILENAME = "CERT.json"
REVOKED_FILENAME = "revoked.jsonl"
PUBKEY_SUFFIX = ".pub"
CERT_SCHEMA = "moeka.capcert.v1"
CERT_KINDS: tuple[str, ...] = ("skill", "script-tool")
CERT_SCOPES: tuple[str, ...] = ("capabilities", "kernel")
LOADABLE_SCOPE = "capabilities"
# The package's own trust directory: ships empty; hosts add ``capabilities.trust_dirs``.
BUILTIN_TRUST_DIR = Path(__file__).parent / "trust"

# FR-002 reason codes, in the order the checks run.
REASONS: tuple[str, ...] = (
    "no-cert",
    "bad-json",
    "bad-schema",
    "unknown-key",
    "bad-signature",
    "name-mismatch",
    "kind-mismatch",
    "scope-mismatch",
    "hash-mismatch",
    "io-mismatch",
    "revoked",
    "bad-bundle",
)
OK = "ok"

_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^(?!.*--)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_RFC3339_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|\+00:00)$"
)
_CERT_KEYS = frozenset({
    "schema", "kind", "name", "bundle_sha256", "io", "scores", "lineage", "verifier",
    "issued_at", "key_id", "scope",
})
_IO_KEYS = ("inputs_all", "inputs_any", "outputs", "effects")
_ED25519_KEY_BYTES = 32
_ED25519_SIG_BYTES = 64


class BundleError(ValueError):
    """A bundle that has no valid hash (a symlink, device, fifo or socket in it)."""


# -- canonical forms ------------------------------------------------------------------


def canonical_json(obj: Any) -> bytes:
    """The canonical JSON bytes of *obj* (sorted keys, no spaces, UTF-8)."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def cert_id_of(cert: Any) -> str:
    """First 16 hex characters of ``sha256(canonical JSON of cert)``."""
    return hashlib.sha256(canonical_json(cert)).hexdigest()[:16]


def key_id_of(public_key: bytes) -> str:
    """First 16 hex characters of ``sha256(raw 32-byte public key)``."""
    return hashlib.sha256(bytes(public_key)).hexdigest()[:16]


def io_to_cert(io: IOSpec) -> dict[str, list[str]]:
    """An :class:`IOSpec` as the certificate's ``io`` object (sorted lists)."""
    return {
        "inputs_all": list(io.inputs_all),
        "inputs_any": list(io.inputs_any),
        "outputs": list(io.outputs),
        "effects": list(io.effects),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bundle_files(root: Path) -> list[tuple[str, Path]]:
    """``(relpath, path)`` for every regular file under *root* (``BundleError`` otherwise)."""
    out: list[tuple[str, Path]] = []
    pending: list[tuple[Path, str]] = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise BundleError(f"cannot list {directory}: {exc.strerror or exc}") from exc
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise BundleError(f"cannot stat {rel}: {exc.strerror or exc}") from exc
            if stat.S_ISLNK(mode):
                raise BundleError(f"bundle contains a symlink: {rel}")
            if stat.S_ISDIR(mode):
                pending.append((Path(entry.path), rel + "/"))
            elif stat.S_ISREG(mode):
                if rel != CERT_FILENAME:
                    out.append((rel, Path(entry.path)))
            else:
                raise BundleError(f"bundle contains a special file (device, fifo or socket): {rel}")
    return out


def bundle_sha256(bundle_dir: str | Path) -> str:
    """The bundle hash (spec 012 Terms): sha256 hex of the canonical ``[[relpath, sha]]``.

    Raises :class:`BundleError` when the bundle is not a directory or contains a
    symlink, device, fifo or socket (such a bundle has no valid hash).
    """
    root = Path(bundle_dir)
    if not root.is_dir():
        raise BundleError(f"bundle is not a directory: {root}")
    try:
        pairs = sorted(
            [rel, _sha256_file(path)] for rel, path in _bundle_files(root)
        )
    except OSError as exc:
        raise BundleError(f"cannot read bundle file: {exc}") from exc
    return hashlib.sha256(canonical_json(pairs)).hexdigest()


def bundle_stamp(bundle_dir: str | Path) -> tuple[Any, ...] | None:
    """A cheap change stamp of a bundle tree (names, types, sizes, mtimes): no reads.

    ``None`` when the tree cannot be listed. Equal stamps mean nothing on disk changed
    as far as ``stat`` can tell; used to cache verdicts, never as proof.
    """
    items: list[tuple[Any, ...]] = []
    pending = [Path(bundle_dir)]
    try:
        while pending:
            directory = pending.pop()
            for entry in os.scandir(directory):
                st = entry.stat(follow_symlinks=False)
                items.append(
                    (entry.path, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns)
                )
                if stat.S_ISDIR(st.st_mode):
                    pending.append(Path(entry.path))
    except OSError:
        return None
    return tuple(sorted(items))


# -- trust store ----------------------------------------------------------------------


def _public_key(raw: bytes) -> Any:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(raw)


@dataclass(frozen=True)
class TrustStore:
    """Trusted Ed25519 public keys (by key id) and revoked certificate ids.

    ``keys`` maps a key id to the raw 32-byte public keys with that id (the same key
    loaded twice is kept once). ``revocations`` maps a revoked cert id to its
    ``revoked.jsonl`` record.
    """

    keys: Mapping[str, tuple[bytes, ...]] = field(default_factory=dict)
    revocations: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def revoked(self) -> frozenset[str]:
        return frozenset(self.revocations)

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self.keys)

    def is_revoked(self, cert_id: str) -> bool:
        return cert_id in self.revocations

    def keys_for(self, key_id: str) -> tuple[bytes, ...]:
        return tuple(self.keys.get(key_id, ()))

    @classmethod
    def from_keys(
        cls, pubkeys: Iterable[bytes], revoked: Iterable[str] = (),
    ) -> TrustStore:
        """A store from raw 32-byte public keys and revoked cert ids (embedders, tests).

        Raises ``ValueError`` for a key that is not 32 bytes.
        """
        keys: dict[str, list[bytes]] = {}
        for raw in pubkeys:
            raw = bytes(raw)
            if len(raw) != _ED25519_KEY_BYTES:
                raise ValueError(f"an Ed25519 public key is 32 bytes, got {len(raw)}")
            bucket = keys.setdefault(key_id_of(raw), [])
            if raw not in bucket:
                bucket.append(raw)
        return cls(
            keys={kid: tuple(values) for kid, values in keys.items()},
            revocations={str(cid): {"cert_id": str(cid)} for cid in revoked},
        )

    @classmethod
    def load(
        cls, dirs: Iterable[str | Path] = (), *, include_builtin: bool = True,
    ) -> TrustStore:
        """Read ``*.pub`` keys and ``revoked.jsonl`` from *dirs* (``~`` expanded).

        The package's own (empty) trust dir is read first unless *include_builtin* is
        false. A missing dir is skipped; a malformed ``.pub`` file or a
        ``revoked.jsonl`` line that is not a JSON object with a ``cert_id`` string is
        skipped with a warning. Duplicate key ids (the same key twice) are fine.
        """
        roots: list[Path] = [BUILTIN_TRUST_DIR] if include_builtin else []
        roots.extend(Path(str(item)).expanduser() for item in dirs)
        keys: dict[str, list[bytes]] = {}
        revocations: dict[str, dict[str, Any]] = {}
        seen: set[Path] = set()
        for root in roots:
            try:
                resolved = root.resolve()
            except (OSError, RuntimeError):
                resolved = root
            if resolved in seen:
                continue
            seen.add(resolved)
            if not root.is_dir():
                if root != BUILTIN_TRUST_DIR:
                    logger.debug("trust store: {} is not a directory; skipped", root)
                continue
            for pub in sorted(root.glob(f"*{PUBKEY_SUFFIX}")):
                raw = _read_pubkey(pub)
                if raw is None:
                    continue
                bucket = keys.setdefault(key_id_of(raw), [])
                if raw not in bucket:
                    bucket.append(raw)
            revoked_file = root / REVOKED_FILENAME
            if revoked_file.is_file():
                revocations.update(_read_revocations(revoked_file))
        return cls(
            keys={kid: tuple(values) for kid, values in keys.items()},
            revocations=revocations,
        )


def _read_pubkey(path: Path) -> bytes | None:
    """The raw key of a ``.pub`` file (one line: base64 of 32 bytes), else ``None``."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("trust store: skipping unreadable key file {} ({})", path, exc)
        return None
    if not text or "\n" in text:
        logger.warning("trust store: skipping malformed key file {} (expected one line)", path)
        return None
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("trust store: skipping malformed key file {} (not base64)", path)
        return None
    if len(raw) != _ED25519_KEY_BYTES:
        logger.warning(
            "trust store: skipping malformed key file {} ({} bytes, expected 32)", path, len(raw),
        )
        return None
    try:
        _public_key(raw)
    except Exception:  # noqa: BLE001 - any parse failure means a malformed key
        logger.warning("trust store: skipping malformed key file {} (not an Ed25519 key)", path)
        return None
    return raw


def _read_revocations(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("trust store: cannot read {} ({})", path, exc)
        return out
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            logger.warning("trust store: {} line {} is not valid JSON; skipped", path, number)
            continue
        cert_id = record.get("cert_id") if isinstance(record, dict) else None
        if not isinstance(cert_id, str) or not cert_id:
            logger.warning("trust store: {} line {} has no cert_id; skipped", path, number)
            continue
        out[cert_id] = record
    return out


def trust_stamp(dirs: Iterable[str | Path], *, include_builtin: bool = True) -> tuple[Any, ...]:
    """A change stamp of the trust dirs' key and revocation files (no reads)."""
    roots: list[Path] = [BUILTIN_TRUST_DIR] if include_builtin else []
    roots.extend(Path(str(item)).expanduser() for item in dirs)
    items: list[tuple[Any, ...]] = []
    for root in roots:
        try:
            names = sorted(
                entry.name for entry in os.scandir(root)
                if entry.name.endswith(PUBKEY_SUFFIX) or entry.name == REVOKED_FILENAME
            )
        except OSError:
            items.append((str(root), None))
            continue
        for name in names:
            try:
                st = (root / name).stat()
            except OSError:
                continue
            items.append((str(root), name, st.st_size, st.st_mtime_ns, st.st_ino))
    return tuple(items)


# -- certificate schema (FR-001) ------------------------------------------------------


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def cert_schema_problems(cert: object) -> list[str]:
    """Every way *cert* violates ``capability-cert.v1`` (``[]`` = valid).

    The same rules as ``schemas/capability-cert.v1.schema.json``, in stdlib Python
    (no ``jsonschema`` at runtime).
    """
    if not isinstance(cert, dict):
        return ["cert: must be an object"]
    problems: list[str] = []
    missing = sorted(_CERT_KEYS - set(cert))
    extra = sorted(str(key) for key in cert if key not in _CERT_KEYS)
    problems.extend(f"cert.{key}: required" for key in missing)
    problems.extend(f"cert.{key}: unknown property" for key in extra)

    def check(key: str, ok: bool, message: str) -> None:
        if key in cert and not ok:
            problems.append(f"cert.{key}: {message}")

    check("schema", cert.get("schema") == CERT_SCHEMA, f"must be {CERT_SCHEMA!r}")
    check("kind", cert.get("kind") in CERT_KINDS, f"must be one of {CERT_KINDS}")
    name = cert.get("name")
    check(
        "name",
        isinstance(name, str) and 1 <= len(name) <= 64 and _NAME.match(name) is not None,
        "must be a hyphen-case name of at most 64 chars",
    )
    digest = cert.get("bundle_sha256")
    check("bundle_sha256", isinstance(digest, str) and bool(_HEX64.match(digest)),
          "must be 64 lowercase hex characters")
    io = cert.get("io")
    io_ok = (
        isinstance(io, dict)
        and set(io) == set(_IO_KEYS)
        and all(
            isinstance(io[key], list)
            and all(isinstance(item, str) for item in io[key])
            and len(set(io[key])) == len(io[key])
            for key in _IO_KEYS
        )
    )
    check("io", io_ok, f"must be an object with exactly {_IO_KEYS} as lists of unique strings")
    scores = cert.get("scores")
    check("scores", isinstance(scores, dict) and all(_is_number(v) for v in scores.values()),
          "must be an object of numbers")
    lineage = cert.get("lineage")
    check(
        "lineage",
        isinstance(lineage, dict)
        and set(lineage) == {"parent"}
        and (
            lineage["parent"] is None
            or (isinstance(lineage["parent"], str) and bool(_HEX16.match(lineage["parent"])))
        ),
        "must be {'parent': <16 hex cert id> | null}",
    )
    verifier = cert.get("verifier")
    check(
        "verifier",
        isinstance(verifier, dict)
        and set(verifier) == {"version", "suite"}
        and all(isinstance(verifier[key], str) for key in ("version", "suite")),
        "must be {'version': str, 'suite': str}",
    )
    issued = cert.get("issued_at")
    check("issued_at", isinstance(issued, str) and bool(_RFC3339_UTC.match(issued)),
          "must be an RFC 3339 UTC timestamp")
    key_id = cert.get("key_id")
    check("key_id", isinstance(key_id, str) and bool(_HEX16.match(key_id)),
          "must be 16 lowercase hex characters")
    check("scope", cert.get("scope") in CERT_SCOPES, f"must be one of {CERT_SCOPES}")
    return problems


# -- verification (FR-002) ------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """The outcome of :func:`verify_bundle`.

    ``ok`` is true only when every check passed (``reason == "ok"``); otherwise
    ``reason`` is the first failing FR-002 code. ``cert_id`` is set once the
    certificate passed the schema check (``None`` for ``no-cert``, ``bad-json`` and
    ``bad-schema``).
    """

    ok: bool
    reason: str
    cert_id: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def bundle_kind(bundle_dir: Path) -> str | None:
    """``"skill"`` (``SKILL.md``), ``"script-tool"`` (``tool.yaml``) or ``None``.

    A bundle holding both is classified by its parent directory (``skills`` or
    ``tools-script``), else as a skill.
    """
    has_skill = (bundle_dir / "SKILL.md").is_file()
    has_tool = (bundle_dir / "tool.yaml").is_file()
    if has_skill and has_tool:
        return "script-tool" if bundle_dir.parent.name == "tools-script" else "skill"
    if has_skill:
        return "skill"
    if has_tool:
        return "script-tool"
    return None


def bundle_io(bundle_dir: Path, kind: str) -> IOSpec | None:
    """The bundle's own normalised ``io`` block (``None`` when absent or invalid)."""
    import yaml

    try:
        if kind == "skill":
            from nanobot.agent.skills import parse_skill_metadata

            text = (bundle_dir / "SKILL.md").read_text(encoding="utf-8")
            metadata = parse_skill_metadata(text) or {}
        else:
            loaded = yaml.safe_load((bundle_dir / "tool.yaml").read_text(encoding="utf-8"))
            metadata = loaded if isinstance(loaded, dict) else {}
        return parse_io(metadata.get("io"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ManifestError):
        return None


def _signature_valid(cert: Mapping[str, Any], sig: str, keys: tuple[bytes, ...]) -> bool:
    from cryptography.exceptions import InvalidSignature

    try:
        signature = base64.b64decode(sig, validate=True)
    except (binascii.Error, ValueError):
        return False
    if len(signature) != _ED25519_SIG_BYTES:
        return False
    message = canonical_json(cert)
    for raw in keys:
        try:
            _public_key(raw).verify(signature, message)
            return True
        except InvalidSignature:
            continue
        except Exception:  # noqa: BLE001 - a key that cannot verify never vouches
            continue
    return False


def _io_matches(cert_io: Mapping[str, Any], own: IOSpec | None) -> bool:
    if own is None:
        return False
    expected = io_to_cert(own)
    return all(sorted(cert_io.get(key, [])) == expected[key] for key in _IO_KEYS)


def verify_bundle(bundle_dir: str | Path, trust: TrustStore) -> Verdict:
    """Check a bundle's ``CERT.json`` against *trust* (spec 012 FR-002).

    The checks run in this order and the first failure is the reason: ``no-cert``,
    ``bad-json``, ``bad-schema``, ``unknown-key``, ``bad-signature``,
    ``name-mismatch``, ``kind-mismatch``, ``scope-mismatch``, ``hash-mismatch``,
    ``io-mismatch``, ``revoked``, ``bad-bundle``. A bundle that cannot be hashed
    (``BundleError``: a symlink, fifo or socket in it) is ``bad-bundle``, reported at the
    hash step, i.e. before ``hash-mismatch``, ``io-mismatch`` and ``revoked``.
    """
    root = Path(os.path.abspath(Path(bundle_dir)))
    cert_file = root / CERT_FILENAME
    if not cert_file.exists() and not cert_file.is_symlink():
        return Verdict(False, "no-cert")
    try:
        document = json.loads(cert_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return Verdict(False, "bad-json")
    if not isinstance(document, dict):
        return Verdict(False, "bad-json")

    cert = document.get("cert")
    sig = document.get("sig")
    if not isinstance(sig, str) or cert_schema_problems(cert):
        return Verdict(False, "bad-schema")
    assert isinstance(cert, dict)
    cert_id = cert_id_of(cert)

    keys = trust.keys_for(cert["key_id"])
    if not keys:
        return Verdict(False, "unknown-key", cert_id)
    if not _signature_valid(cert, sig, keys):
        return Verdict(False, "bad-signature", cert_id)
    if cert["name"] != root.name:
        return Verdict(False, "name-mismatch", cert_id)
    kind = bundle_kind(root)
    if cert["kind"] != kind:
        return Verdict(False, "kind-mismatch", cert_id)
    if cert["scope"] != LOADABLE_SCOPE:
        return Verdict(False, "scope-mismatch", cert_id)
    try:
        digest = bundle_sha256(root)
    except BundleError:
        # A bundle with no valid hash fails where the hash is computed.
        return Verdict(False, "bad-bundle", cert_id)
    if digest != cert["bundle_sha256"]:
        return Verdict(False, "hash-mismatch", cert_id)
    if not _io_matches(cert["io"], bundle_io(root, cert["kind"])):
        return Verdict(False, "io-mismatch", cert_id)
    if trust.is_revoked(cert_id):
        return Verdict(False, "revoked", cert_id)
    return Verdict(True, OK, cert_id)


__all__ = [
    "BUILTIN_TRUST_DIR",
    "CERT_FILENAME",
    "OK",
    "REASONS",
    "REVOKED_FILENAME",
    "BundleError",
    "TrustStore",
    "Verdict",
    "bundle_kind",
    "bundle_sha256",
    "canonical_json",
    "cert_id_of",
    "cert_schema_problems",
    "io_to_cert",
    "key_id_of",
    "verify_bundle",
]
