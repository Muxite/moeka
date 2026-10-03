"""Spec 012 Part A: bundle hash, trust store, certificate schema and verification."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
from _certkit import Key, certify, make_cert, make_script, make_skill, revoke, write_trust

from nanobot.capabilities import provenance
from nanobot.capabilities.provenance import (
    REASONS,
    BundleError,
    TrustStore,
    Verdict,
    bundle_sha256,
    canonical_json,
    cert_id_of,
    cert_schema_problems,
    key_id_of,
    verify_bundle,
)

REPO = Path(__file__).resolve().parents[2]


# -- canonical forms and the bundle hash ----------------------------------------------


def test_canonical_json_is_sorted_compact_utf8():
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


def test_bundle_hash_matches_the_definition(tmp_path):
    bundle = tmp_path / "b"
    (bundle / "sub").mkdir(parents=True)
    (bundle / "SKILL.md").write_bytes(b"one")
    (bundle / "sub" / "x.txt").write_bytes(b"two")
    (bundle / "CERT.json").write_bytes(b"ignored")
    pairs = sorted([
        ["SKILL.md", hashlib.sha256(b"one").hexdigest()],
        ["sub/x.txt", hashlib.sha256(b"two").hexdigest()],
    ])
    expected = hashlib.sha256(
        json.dumps(pairs, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert bundle_sha256(bundle) == expected


def test_bundle_hash_ignores_cert_and_tracks_every_other_byte(tmp_path):
    bundle = make_skill(tmp_path, "alpha")
    before = bundle_sha256(bundle)
    (bundle / "CERT.json").write_text("{}")
    assert bundle_sha256(bundle) == before
    (bundle / "extra.txt").write_text("x")
    assert bundle_sha256(bundle) != before
    (bundle / "extra.txt").unlink()
    (bundle / "sub").mkdir()
    (bundle / "sub" / "CERT.json").write_text("{}")  # only the root CERT.json is excluded
    assert bundle_sha256(bundle) != before


def test_bundle_hash_rejects_symlinks_and_special_files(tmp_path):
    bundle = make_skill(tmp_path, "alpha")
    (bundle / "link").symlink_to(bundle / "SKILL.md")
    with pytest.raises(BundleError):
        bundle_sha256(bundle)
    (bundle / "link").unlink()
    (bundle / "dirlink").symlink_to(tmp_path)
    with pytest.raises(BundleError):
        bundle_sha256(bundle)
    (bundle / "dirlink").unlink()
    os.mkfifo(bundle / "fifo")
    with pytest.raises(BundleError):
        bundle_sha256(bundle)


def test_bundle_hash_of_a_missing_dir_is_a_bundle_error(tmp_path):
    with pytest.raises(BundleError):
        bundle_sha256(tmp_path / "nope")


# -- trust store ----------------------------------------------------------------------


def test_trust_store_loads_keys_and_revocations(tmp_path, caplog_warnings):
    a, b = Key(), Key()
    trust = write_trust(tmp_path / "t", [a, b, a])  # a twice: same key id is fine
    (trust / "bad.pub").write_text("not base64!!\n")
    (trust / "short.pub").write_text(base64.b64encode(b"x" * 31).decode() + "\n")
    (trust / "revoked.jsonl").write_text(
        '{"cert_id": "aaaaaaaaaaaaaaaa", "reason": "r", "at": "t"}\nnot json\n\n'
        '{"reason": "no id"}\n'
    )
    store = TrustStore.load([trust])
    assert store.key_ids == {a.key_id, b.key_id}
    assert store.keys_for(a.key_id) == (a.public,)
    assert store.revoked == {"aaaaaaaaaaaaaaaa"}
    joined = "\n".join(caplog_warnings)
    assert "bad.pub" in joined and "short.pub" in joined and "not valid JSON" in joined


def test_trust_store_expands_home_and_skips_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    key = Key()
    write_trust(tmp_path / "trusted", [key])
    store = TrustStore.load(["~/trusted", tmp_path / "missing"])
    assert store.key_ids == {key.key_id}


def test_trust_store_from_keys():
    key = Key()
    store = TrustStore.from_keys([key.public], {"0123456789abcdef"})
    assert store.key_ids == {key.key_id}
    assert store.is_revoked("0123456789abcdef")
    with pytest.raises(ValueError):
        TrustStore.from_keys([b"short"])


def test_key_id_is_sha256_prefix():
    key = Key()
    assert key.key_id == hashlib.sha256(key.public).hexdigest()[:16] == key_id_of(key.public)


def test_builtin_trust_dir_ships_empty():
    keys = list(provenance.BUILTIN_TRUST_DIR.glob("*.pub"))
    assert provenance.BUILTIN_TRUST_DIR.is_dir() and keys == []
    assert not (provenance.BUILTIN_TRUST_DIR / "revoked.jsonl").exists()


# -- schema (FR-001) ------------------------------------------------------------------


def _schema_validator():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO / "schemas" / "capability-cert.v1.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _variants(base: dict) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = [("valid", base), ("kernel scope", {**base, "scope": "kernel"})]
    for key in base:
        out.append((f"missing {key}", {k: v for k, v in base.items() if k != key}))
    out += [
        ("extra", {**base, "extra": 1}),
        ("schema", {**base, "schema": "moeka.capcert.v2"}),
        ("kind", {**base, "kind": "tool"}),
        ("hash", {**base, "bundle_sha256": "ABC"}),
        ("io extra", {**base, "io": {**base["io"], "x": []}}),
        ("io dup", {**base, "io": {**base["io"], "outputs": ["a", "a"]}}),
        ("scores str", {**base, "scores": {"x": "1"}}),
        ("scores bool", {**base, "scores": {"x": True}}),
        ("lineage parent", {**base, "lineage": {"parent": "0123456789abcdef"}}),
        ("lineage bad", {**base, "lineage": {"parent": "xyz"}}),
        ("verifier", {**base, "verifier": {"version": "1"}}),
        ("issued local", {**base, "issued_at": "2026-10-03T12:00:00+02:00"}),
        ("issued frac", {**base, "issued_at": "2026-10-03T12:00:00.5+00:00"}),
        ("key id", {**base, "key_id": "XYZ"}),
        ("scope", {**base, "scope": "other"}),
    ]
    return out


def test_cert_schema_file_and_runtime_check_agree(tmp_path):
    validator = _schema_validator()
    bundle = make_skill(tmp_path, "alpha")
    base = make_cert(bundle, Key())
    for label, cert in _variants(base):
        schema_ok = not list(validator.iter_errors(cert))
        assert schema_ok == (cert_schema_problems(cert) == []), label
    assert cert_schema_problems(base) == []


def test_cert_schema_validates_with_schemas_validate(tmp_path):
    pytest.importorskip("jsonschema")
    sys.path.insert(0, str(REPO / "schemas"))
    try:
        import validate
    finally:
        sys.path.pop(0)
    cert = make_cert(make_skill(tmp_path, "alpha"), Key())
    assert validate.problems(cert, "capability-cert") == []
    assert validate.problems({**cert, "scope": "x"}, "capability-cert")


# -- verification (FR-002): one fixture per reason --------------------------------------


def _fixture(reason: str, tmp_path: Path) -> tuple[Path, TrustStore]:
    """A bundle whose verdict is exactly *reason* (and nothing earlier in the order)."""
    key, other = Key(), Key()
    workspace = tmp_path / "ws"
    bundle = make_skill(workspace, "alpha")
    trust_dir = write_trust(tmp_path / "trust", [key])
    if reason == "no-cert":
        pass
    elif reason == "bad-json":
        (bundle / "CERT.json").write_text("{not json")
    elif reason == "bad-schema":
        certify(bundle, key, mutate=lambda c: c.pop("scores"))
    elif reason == "unknown-key":
        certify(bundle, other)  # signed by a key the trust store does not hold
    elif reason == "bad-signature":
        certify(bundle, key, signer=other)
    elif reason == "name-mismatch":
        certify(bundle, key)
        renamed = bundle.with_name("beta")
        bundle.rename(renamed)
        bundle = renamed
    elif reason == "kind-mismatch":
        certify(bundle, key, kind="script-tool")
    elif reason == "scope-mismatch":
        certify(bundle, key, scope="kernel")
    elif reason == "hash-mismatch":
        certify(bundle, key)
        (bundle / "notes.txt").write_text("added after certification")
    elif reason == "io-mismatch":
        certify(bundle, key, io={"inputs_all": [], "inputs_any": [], "outputs": ["any"],
                                 "effects": ["exec"]})
    elif reason == "revoked":
        revoke(trust_dir, certify(bundle, key))
    elif reason == "bad-bundle":
        (bundle / "link").symlink_to("SKILL.md")
        certify(bundle, key, bundle_sha256="0" * 64)
    else:  # pragma: no cover
        raise AssertionError(reason)
    return bundle, TrustStore.load([trust_dir])


@pytest.mark.parametrize("reason", REASONS)
def test_every_reason_code_has_its_fixture(reason, tmp_path):
    bundle, trust = _fixture(reason, tmp_path)
    verdict = verify_bundle(bundle, trust)
    assert verdict == Verdict(False, reason, verdict.cert_id)
    assert not verdict.ok and not verdict
    if reason in ("no-cert", "bad-json", "bad-schema"):
        assert verdict.cert_id is None
    else:
        assert re.fullmatch(r"[0-9a-f]{16}", verdict.cert_id or "")


def test_reason_codes_are_exactly_the_spec_list():
    assert REASONS == (
        "no-cert", "bad-json", "bad-schema", "unknown-key", "bad-signature", "name-mismatch",
        "kind-mismatch", "scope-mismatch", "hash-mismatch", "io-mismatch", "revoked",
        "bad-bundle",
    )


def test_ok_verdict_and_cert_id(tmp_path):
    key = Key()
    bundle = make_skill(tmp_path / "ws", "alpha")
    cert_id = certify(bundle, key)
    verdict = verify_bundle(bundle, TrustStore.from_keys([key.public]))
    assert verdict == Verdict(True, "ok", cert_id)
    cert = json.loads((bundle / "CERT.json").read_text())["cert"]
    assert cert_id == cert_id_of(cert) == hashlib.sha256(canonical_json(cert)).hexdigest()[:16]


def test_script_tool_bundles_verify(tmp_path):
    key = Key()
    bundle = make_script(tmp_path / "ws", "echo", "#!/bin/sh\necho '{}'\n")
    certify(bundle, key)
    assert verify_bundle(bundle, TrustStore.from_keys([key.public])).ok


def test_order_first_failure_wins(tmp_path):
    key, other = Key(), Key()
    bundle = make_skill(tmp_path / "ws", "alpha")
    trust = TrustStore.from_keys([key.public])
    # Untrusted key AND wrong kind AND tampered: unknown-key is first.
    certify(bundle, other, kind="script-tool")
    (bundle / "x").write_text("x")
    assert verify_bundle(bundle, trust).reason == "unknown-key"
    # Wrong name and wrong scope: name-mismatch comes before scope-mismatch.
    certify(bundle, key, name="beta", scope="kernel")
    assert verify_bundle(bundle, trust).reason == "name-mismatch"
    # Tampered and revoked: hash-mismatch comes before revoked.
    cert_id = certify(bundle, key)
    (bundle / "y").write_text("y")
    assert verify_bundle(bundle, TrustStore.from_keys([key.public], {cert_id})).reason == \
        "hash-mismatch"


def test_unhashable_bundle_is_bad_bundle_at_the_hash_step(tmp_path):
    key = Key()
    bundle = make_skill(tmp_path / "ws", "alpha")
    cert_id = certify(bundle, key)  # a valid cert for the bundle as it was
    (bundle / "link").symlink_to("SKILL.md")
    assert verify_bundle(bundle, TrustStore.from_keys([key.public])).reason == "bad-bundle"
    # bad-bundle is reported where the hash is computed: before io-mismatch and revoked.
    revoked = TrustStore.from_keys([key.public], {cert_id})
    assert verify_bundle(bundle, revoked).reason == "bad-bundle"
    certify(bundle, key, bundle_sha256="0" * 64,
            io={"inputs_all": [], "inputs_any": [], "outputs": ["any"], "effects": ["exec"]})
    assert verify_bundle(bundle, TrustStore.from_keys([key.public])).reason == "bad-bundle"
    # Earlier checks still win over it.
    certify(bundle, key, bundle_sha256="0" * 64, scope="kernel")
    assert verify_bundle(bundle, TrustStore.from_keys([key.public])).reason == "scope-mismatch"


def test_sig_must_be_a_string_and_cert_an_object(tmp_path):
    key = Key()
    bundle = make_skill(tmp_path / "ws", "alpha")
    trust = TrustStore.from_keys([key.public])
    (bundle / "CERT.json").write_text(json.dumps({"cert": make_cert(bundle, key), "sig": 1}))
    assert verify_bundle(bundle, trust).reason == "bad-schema"
    (bundle / "CERT.json").write_text(json.dumps([1, 2]))
    assert verify_bundle(bundle, trust).reason == "bad-json"
    (bundle / "CERT.json").write_text(json.dumps({"cert": make_cert(bundle, key),
                                                  "sig": "!!notbase64"}))
    assert verify_bundle(bundle, trust).reason == "bad-signature"


def test_verification_of_100_files_is_fast(tmp_path):
    """NFR-002: a 100-file bundle verifies in < 50 ms (best of five)."""
    key = Key()
    bundle = make_skill(tmp_path / "ws", "big")
    for index in range(99):
        (bundle / f"f{index:03d}.txt").write_bytes(os.urandom(2048))
    certify(bundle, key)
    trust = TrustStore.from_keys([key.public])
    timings = []
    for _ in range(5):
        start = time.perf_counter()
        assert verify_bundle(bundle, trust).ok
        timings.append(time.perf_counter() - start)
    assert min(timings) < 0.05, timings


# -- FR-004: no signing in moeka ------------------------------------------------------


def test_no_private_key_handling_in_moeka_source():
    forbidden = ("Ed25519PrivateKey", "from_private_bytes", "load_pem_private_key", "generate(")
    roots = [REPO / "nanobot" / "capabilities"]
    hits = [
        f"{path.relative_to(REPO)}: {needle}"
        for root in roots for path in root.rglob("*.py")
        for needle in forbidden if needle in path.read_text(encoding="utf-8")
    ]
    assert hits == []
    private_markers = ("Ed25519PrivateKey", "from_private_bytes", "load_pem_private_key")
    wider = [
        str(path.relative_to(REPO))
        for root in (REPO / "nanobot", REPO / "moeka") for path in root.rglob("*.py")
        if any(m in path.read_text(encoding="utf-8", errors="ignore") for m in private_markers)
    ]
    assert wider == []


def test_public_api_has_no_sign_function():
    import nanobot.capabilities.enforce as enforce
    import nanobot.capabilities.script_tool as script_tool

    for module in (provenance, enforce, script_tool):
        assert not any(name == "sign" or name.startswith("sign_") for name in dir(module))
    assert not hasattr(TrustStore, "sign")
