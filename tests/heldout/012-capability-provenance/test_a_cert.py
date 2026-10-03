"""Part A: the certificate schema (FR-001), canonical JSON / bundle hash / BundleError (Terms,
used by FR-002), computed independently in ``_h012`` and compared with the implementation."""

from __future__ import annotations

import importlib.util
import json
import os
import socket

import pytest
from _h012 import (
    ISSUED_AT,
    RefBundleError,
    Signer,
    canonical,
    make_cert,
    prov,
    ref_bundle_sha256,
    ref_cert_id,
    ref_files,
    repo_root,
    sha_hex,
    write_script_tool,
    write_skill,
)

REPO = repo_root()
SCHEMA = REPO / "schemas" / "capability-cert.v1.schema.json"


# -- FR-001 schema ------------------------------------------------------------------------------


def _validate_module():
    path = REPO / "schemas" / "validate.py"
    spec = importlib.util.spec_from_file_location("h012_schema_validate", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _problems(cert, sig="c2ln"):
    assert SCHEMA.is_file(), "schemas/capability-cert.v1.schema.json missing"
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    wrapper = "cert" in (schema.get("properties") or {})
    doc = {"cert": cert, "sig": sig} if wrapper else cert
    return _validate_module().problems(doc, "capability-cert")


@pytest.fixture
def skill_cert(tmp_path):
    bundle = write_skill(tmp_path / "ws", "h012-schema")
    return make_cert(bundle, Signer("alpha"))


@pytest.mark.fr("FR-001")
def test_schema_file_is_json_schema():
    assert SCHEMA.is_file()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert isinstance(schema, dict) and schema


@pytest.mark.fr("FR-001")
def test_valid_skill_cert_has_no_problems(skill_cert):
    assert _problems(skill_cert) == []


@pytest.mark.fr("FR-001")
def test_valid_script_tool_cert_has_no_problems(tmp_path):
    bundle = write_script_tool(tmp_path / "ws", "h012-schema-tool")
    cert = make_cert(bundle, Signer("alpha"))
    assert cert["kind"] == "script-tool"
    assert _problems(cert) == []


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("over", [
    {"scope": "kernel"},
    {"lineage": {"parent": "0123456789abcdef"}},
    {"scores": {}},
    {"scores": {"pass_rate": 0.5, "latency_ms": 12, "cost": 0}},
], ids=["scope-kernel", "parent-cert-id", "empty-scores", "free-form-scores"])
def test_valid_variants_have_no_problems(skill_cert, over):
    assert _problems({**skill_cert, **over}) == []


REQUIRED = ("schema", "kind", "name", "bundle_sha256", "io", "scores", "lineage", "verifier",
            "issued_at", "key_id", "scope")


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("field", REQUIRED)
def test_every_field_is_required(skill_cert, field):
    cert = dict(skill_cert)
    del cert[field]
    assert _problems(cert) != []


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("over", [
    {"schema": "moeka.capcert.v2"},
    {"kind": "plugin"},
    {"kind": "tool"},
    {"bundle_sha256": "abc"},
    {"bundle_sha256": "g" * 64},
    {"key_id": "0123"},
    {"key_id": "z" * 16},
    {"scope": "global"},
    {"extra_field": 1},
    {"io": "untyped"},
], ids=["schema-v2", "kind-plugin", "kind-tool", "sha-short", "sha-nonhex", "keyid-short",
        "keyid-nonhex", "scope-other", "extra-property", "io-not-object"])
def test_invalid_variants_have_problems(skill_cert, over):
    assert _problems({**skill_cert, **over}) != []


# -- canonical JSON / ids (Terms) --------------------------------------------------------------


@pytest.mark.fr("FR-002")
def test_reference_canonical_form_is_the_spec_one():
    assert canonical({"b": 1, "a": ["é", 2]}) == '{"a":["é",2],"b":1}'.encode("utf-8")


# -- bundle hash --------------------------------------------------------------------------------


def _mixed_bundle(root):
    b = write_skill(root / "ws", "h012-hash", extra_files={
        "a-b.txt": "dash\n",
        "a/b.txt": "nested\n",
        "a0.txt": "zero\n",
        "B.txt": "upper\n",
        "deep/x/y/z.md": "deep\n",
        "é.txt": "unicode name\n",
        "empty.txt": "",
        "bin.dat": bytes(range(256)),
    })
    return b


@pytest.mark.fr("FR-002")
def test_bundle_sha256_matches_reference(tmp_path):
    b = _mixed_bundle(tmp_path)
    got = prov().bundle_sha256(b)
    assert got == ref_bundle_sha256(b)


@pytest.mark.fr("FR-002")
def test_reference_sort_is_plain_string_order(tmp_path):
    """Self-check of the reference: 'a-b.txt' < 'a/b.txt' < 'a0.txt' (string order, not
    directory-walk order), so an implementation sorting per directory differs."""
    rels = [r for r, _ in ref_files(_mixed_bundle(tmp_path))]
    assert rels.index("a-b.txt") < rels.index("a/b.txt") < rels.index("a0.txt")
    assert rels == sorted(rels)


@pytest.mark.fr("FR-002")
def test_bundle_sha256_is_lowercase_hex64(tmp_path):
    got = prov().bundle_sha256(write_skill(tmp_path / "ws", "h012-hex"))
    assert isinstance(got, str) and len(got) == 64 and got == got.lower()
    int(got, 16)


@pytest.mark.fr("FR-002")
def test_bundle_sha256_single_file_by_hand(tmp_path):
    b = tmp_path / "ws" / "skills" / "h012-one"
    b.mkdir(parents=True)
    (b / "SKILL.md").write_bytes(b"hello\n")
    expected = sha_hex(json.dumps([["SKILL.md", sha_hex(b"hello\n")]],
                                  separators=(",", ":")).encode())
    assert prov().bundle_sha256(b) == expected


@pytest.mark.fr("FR-002")
def test_cert_json_is_not_part_of_the_bundle(tmp_path):
    b = _mixed_bundle(tmp_path)
    before = prov().bundle_sha256(b)
    (b / "CERT.json").write_text('{"anything": 1}', encoding="utf-8")
    assert prov().bundle_sha256(b) == before
    (b / "CERT.json").write_text('{"anything": 2}', encoding="utf-8")
    assert prov().bundle_sha256(b) == before


@pytest.mark.fr("FR-002")
def test_any_content_change_changes_hash(tmp_path):
    b = _mixed_bundle(tmp_path)
    before = prov().bundle_sha256(b)
    (b / "deep" / "x" / "y" / "z.md").write_text("deeP\n", encoding="utf-8")
    assert prov().bundle_sha256(b) != before


@pytest.mark.fr("FR-002")
def test_file_rename_inside_bundle_changes_hash(tmp_path):
    b = _mixed_bundle(tmp_path)
    before = prov().bundle_sha256(b)
    os.rename(b / "a0.txt", b / "a1.txt")
    assert prov().bundle_sha256(b) != before


@pytest.mark.fr("FR-002")
def test_added_file_changes_hash(tmp_path):
    b = _mixed_bundle(tmp_path)
    before = prov().bundle_sha256(b)
    (b / "new.txt").write_text("", encoding="utf-8")
    assert prov().bundle_sha256(b) != before


@pytest.mark.fr("FR-002")
def test_hash_is_relative_to_bundle_root(tmp_path):
    b = _mixed_bundle(tmp_path)
    before = prov().bundle_sha256(b)
    moved = b.parent / "h012-moved"
    os.rename(b, moved)
    assert prov().bundle_sha256(moved) == before == ref_bundle_sha256(moved)


@pytest.mark.fr("FR-002")
def test_empty_directory_contributes_nothing(tmp_path):
    b = _mixed_bundle(tmp_path)
    before = prov().bundle_sha256(b)
    (b / "emptydir").mkdir()
    assert prov().bundle_sha256(b) == before


@pytest.mark.fr("FR-002")
def test_script_tool_bundle_hash_matches_reference(tmp_path):
    b = write_script_tool(tmp_path / "ws", "h012-hash-tool")
    assert prov().bundle_sha256(b) == ref_bundle_sha256(b)


def _special(kind, b, monkeypatch):
    if kind == "symlink-file":
        os.symlink("SKILL.md", b / "alias.md")
    elif kind == "symlink-dir":
        (b / "realdir").mkdir()
        os.symlink("realdir", b / "linkdir")
    elif kind == "symlink-dangling":
        os.symlink("nowhere.txt", b / "dangling")
    elif kind == "symlink-nested":
        (b / "sub").mkdir()
        os.symlink("../SKILL.md", b / "sub" / "up.md")
    elif kind == "fifo":
        os.mkfifo(b / "pipe")
    elif kind == "socket":
        monkeypatch.chdir(b)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind("sock")
        return s
    return None


SPECIALS = ["symlink-file", "symlink-dir", "symlink-dangling", "symlink-nested", "fifo",
            "socket"]


@pytest.mark.fr("FR-002")
@pytest.mark.parametrize("kind", SPECIALS)
def test_special_entries_raise_bundle_error(tmp_path, monkeypatch, kind):
    b = write_skill(tmp_path / "ws", "h012-special")
    sock = _special(kind, b, monkeypatch)
    try:
        with pytest.raises(RefBundleError):
            ref_bundle_sha256(b)
        with pytest.raises(prov().BundleError):
            prov().bundle_sha256(b)
    finally:
        if sock is not None:
            sock.close()


@pytest.mark.fr("FR-002")
def test_bundle_error_is_an_exception_class():
    assert issubclass(prov().BundleError, Exception)


# -- cert id / key id (reference self-checks used by FR-002 / FR-012) ------------------------------


@pytest.mark.fr("FR-002")
def test_reference_ids_shape(skill_cert):
    cid = ref_cert_id(skill_cert)
    assert len(cid) == 16 and int(cid, 16) >= 0
    assert skill_cert["key_id"] == Signer("alpha").key_id
    assert skill_cert["issued_at"] == ISSUED_AT
