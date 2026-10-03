"""The package's built-in capability trust store (spec 012 FR-003): ships EMPTY.

A trust directory holds ``*.pub`` files (one line each: the base64 of a raw 32-byte
Ed25519 public key) and an optional ``revoked.jsonl``. Hosts add their own directories
with ``capabilities.trust_dirs``; this one is read first and is write-protected for the
file tools (``nanobot.security.protected_paths``). This module exists only so the
directory ships in the wheel.
"""
