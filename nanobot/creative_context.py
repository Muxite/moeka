"""Authenticate host-minted creative context on a direct Telegram turn."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from nanobot.peer_context import validate_peer_context

_KEYS = {"process": secrets.token_bytes(32)}
_SIGNATURE = "_creative_host_signature"


def register(workspace: Path) -> str:
    """Keep restart proofs outside the agent's filesystem/shell workspace."""
    key_id = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:24]
    root = Path.home() / ".local/state/moeka/creative-context"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (key_id + ".key")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_bytes(32))
    key = path.read_bytes()
    if len(key) != 32 or path.stat().st_mode & 0o077:
        raise ValueError("creative context key must be private and 32 bytes")
    _KEYS[key_id] = key
    return key_id


def stamp(metadata: dict[str, Any], context: dict[str, Any], key_id: str = "process") -> None:
    validated = validate_peer_context(context)
    if validated is None:
        raise ValueError("missing direct creative context")
    metadata["creative"] = validated
    metadata["_creative_host_key_id"] = key_id
    metadata[_SIGNATURE] = hmac.new(
        _KEYS[key_id], json.dumps(validated, sort_keys=True).encode(), hashlib.sha256
    ).hexdigest()


def clear(metadata: dict[str, Any]) -> None:
    metadata.pop("creative", None)
    metadata.pop(_SIGNATURE, None)
    metadata.pop("_creative_host_key_id", None)


def validate(
    metadata: dict[str, Any], *, channel: str, chat_id: str, sender_id: str | None
) -> dict[str, Any]:
    context = validate_peer_context(metadata.get("creative"))
    if context is None or channel != "telegram":
        raise ValueError("direct creative context requires authenticated Telegram ingress")
    key_id = metadata.get("_creative_host_key_id")
    key = _KEYS.get(key_id) if isinstance(key_id, str) else None
    if key is None:
        raise ValueError("unknown direct context owner")
    expected = hmac.new(
        key, json.dumps(context, sort_keys=True).encode(), hashlib.sha256
    ).hexdigest()
    signature = metadata.get(_SIGNATURE)
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        raise ValueError("untrusted direct creative metadata")
    route = context["route"]
    if route["chat_id"] != chat_id or route["owner_id"] != sender_id:
        raise ValueError("direct creative route differs from the actual caller")
    return context
