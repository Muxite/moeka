"""Test-only certificate kit (spec 012): keys, trust dirs, bundles and a fixture signer.

Signing lives HERE, never under ``nanobot/`` (FR-004: moeka has no private-key code
path). Bundles are written under a temp workspace; ``certify`` signs a ``CERT.json``
the way the RSI harness's verifier would.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable

import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from nanobot.capabilities.manifest import parse_io
from nanobot.capabilities.provenance import (
    bundle_sha256,
    canonical_json,
    cert_id_of,
    io_to_cert,
    key_id_of,
)

DEFAULT_IO: dict[str, Any] = {
    "inputs": ["text/query"], "outputs": ["text/markdown"], "effects": ["read-workspace"],
}
PY = sys.executable


class Key:
    """An Ed25519 key pair for fixtures."""

    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key_id = key_id_of(self.public)

    def pub_line(self) -> str:
        return base64.b64encode(self.public).decode("ascii")

    def sign(self, cert: dict[str, Any]) -> str:
        return base64.b64encode(self.private.sign(canonical_json(cert))).decode("ascii")


def write_trust(directory: Path, keys: list[Key], revoked: list[str] = ()) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index, key in enumerate(keys):
        (directory / f"key{index}.pub").write_text(key.pub_line() + "\n", encoding="utf-8")
    if revoked:
        revoke(directory, *revoked)
    return directory


def revoke(directory: Path, *cert_ids: str) -> None:
    with (directory / "revoked.jsonl").open("a", encoding="utf-8") as handle:
        for cert_id in cert_ids:
            handle.write(json.dumps({"cert_id": cert_id, "reason": "test", "at": "now"}) + "\n")


def make_skill(
    workspace: Path, name: str, *, io: dict[str, Any] | None = DEFAULT_IO,
    description: str | None = None, body: str = "Body.\n",
) -> Path:
    bundle = workspace / "skills" / name
    bundle.mkdir(parents=True, exist_ok=True)
    front: dict[str, Any] = {"name": name, "description": description or f"The {name} skill."}
    if io is not None:
        front["io"] = io
    text = "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n\n" + body
    (bundle / "SKILL.md").write_text(text, encoding="utf-8")
    return bundle


def make_script(
    workspace: Path, name: str, script: str, *, io: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None, output_schema: dict[str, Any] | None = None,
    timeout_s: int | None = None, entry: str = "run", extra: dict[str, Any] | None = None,
) -> Path:
    """A ``tools-script/<name>`` bundle whose ``entry`` holds *script* (made executable)."""
    bundle = workspace / "tools-script" / name
    bundle.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "name": name,
        "description": f"The {name} script tool.",
        "parameters": parameters or {"type": "object", "properties": {}},
        "io": io if io is not None else {"inputs": [], "outputs": ["data/json"],
                                         "effects": ["none"]},
        "entry": entry,
    }
    if output_schema is not None:
        manifest["output_schema"] = output_schema
    if timeout_s is not None:
        manifest["timeout_s"] = timeout_s
    manifest.update(extra or {})
    (bundle / "tool.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    target = bundle / entry
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bundle


def python_script(body: str) -> str:
    """An entry script run by this interpreter (absolute shebang: PATH is /usr/bin:/bin)."""
    return f"#!{PY}\n" + body


def bundle_io_cert(bundle: Path) -> dict[str, list[str]]:
    """The certificate ``io`` for *bundle*'s own io block."""
    if (bundle / "SKILL.md").is_file():
        from nanobot.agent.skills import parse_skill_metadata

        raw = (parse_skill_metadata((bundle / "SKILL.md").read_text()) or {}).get("io")
    else:
        raw = yaml.safe_load((bundle / "tool.yaml").read_text()).get("io")
    spec = parse_io(raw)
    assert spec is not None, "fixture bundles carry an io block"
    return io_to_cert(spec)


def make_cert(bundle: Path, key: Key, **overrides: Any) -> dict[str, Any]:
    kind = "skill" if (bundle / "SKILL.md").is_file() else "script-tool"
    cert: dict[str, Any] = {
        "schema": "moeka.capcert.v1",
        "kind": kind,
        "name": bundle.name,
        "bundle_sha256": overrides.pop("bundle_sha256", None) or bundle_sha256(bundle),
        "io": bundle_io_cert(bundle),
        "scores": {"pass_rate": 1.0, "cases": 12},
        "lineage": {"parent": None},
        "verifier": {"version": "0.1.0", "suite": "fixture"},
        "issued_at": "2026-10-03T12:00:00Z",
        "key_id": key.key_id,
        "scope": "capabilities",
    }
    cert.update(overrides)
    return cert


def certify(
    bundle: Path, key: Key, *, signer: Key | None = None,
    mutate: Callable[[dict[str, Any]], None] | None = None, **overrides: Any,
) -> str:
    """Write a signed ``CERT.json`` into *bundle*; returns the cert id.

    *signer* signs instead of *key* (whose id the cert names); *mutate* edits the cert
    before signing.
    """
    cert = make_cert(bundle, key, **overrides)
    if mutate is not None:
        mutate(cert)
    document = {"cert": cert, "sig": (signer or key).sign(cert)}
    (bundle / "CERT.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    return cert_id_of(cert)


def flip_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[-2] ^= 0x01
    path.write_bytes(bytes(data))
    os.utime(path)
