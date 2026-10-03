"""Part A: ``verify_bundle`` (FR-002) — one fixture per reason code (SC-001), the check order
(pairwise precedence), positive cases, and io normalisation."""

from __future__ import annotations

import itertools
import os
from pathlib import Path

import pytest
from _h012 import (
    DELETE,
    VERIFY_CODES,
    Signer,
    flip_byte,
    make_cert,
    prov,
    ref_cert_id,
    ref_io,
    revoke,
    trust_dir,
    trust_load,
    verify,
    write_cert,
    write_script_tool,
    write_skill,
)

NAME = "h012-fx"


def _bundle(root: Path, kind: str) -> Path:
    ws = root / "ws"
    if kind == "skill":
        return write_skill(ws, NAME, extra_files={"ref/notes.txt": "MARK notes\n"})
    return write_script_tool(ws, NAME)


def _main_file(bundle: Path) -> Path:
    return bundle / ("SKILL.md" if (bundle / "SKILL.md").exists() else "run")


def build(root: Path, kind: str, codes: tuple[str, ...]) -> tuple[Path, object]:
    """A bundle + trust store carrying every failure in *codes* at once ("ok" for none)."""
    alpha, beta = Signer("alpha"), Signer("beta")
    tdir = trust_dir(root / "trust", alpha)
    bundle = _bundle(root, kind)
    over: dict = {}
    if "bad-schema" in codes:
        over["scores"] = DELETE
    if "name-mismatch" in codes:
        over["name"] = "h012-other"
    if "kind-mismatch" in codes:
        over["kind"] = "script-tool" if kind == "skill" else "skill"
    if "scope-mismatch" in codes:
        over["scope"] = "kernel"
    if "io-mismatch" in codes:
        io = ref_io(None)
        io.update(inputs_all=["file:pdf"], outputs=["text/markdown"], effects=["sudo"])
        over["io"] = io
    signer = beta if "unknown-key" in codes else alpha
    cert = make_cert(bundle, signer, **over)
    sig = beta.sig_b64(cert) if "bad-signature" in codes else None
    cid = write_cert(bundle, cert, signer, sig=sig)
    if "hash-mismatch" in codes:
        flip_byte(_main_file(bundle))
    if "revoked" in codes:
        revoke(tdir, cid)
    if "bad-bundle" in codes:
        os.symlink(_main_file(bundle).name, bundle / "alias")
    if "bad-json" in codes:
        (bundle / "CERT.json").write_text('{"cert": {not json', encoding="utf-8")
    if "no-cert" in codes:
        (bundle / "CERT.json").unlink()
    return bundle, trust_load(tdir)


def _reason(v) -> str:
    return "ok" if v.ok else v.reason


# -- positive -------------------------------------------------------------------------------------


@pytest.mark.fr("FR-002")
@pytest.mark.parametrize("kind", ["skill", "script-tool"])
def test_valid_cert_verifies_ok_with_cert_id(tmp_path, kind):
    bundle, trust = build(tmp_path, kind, ())
    v = verify(bundle, trust)
    assert v.ok is True
    cert = __import__("json").loads((bundle / "CERT.json").read_text())["cert"]
    assert v.cert_id == ref_cert_id(cert)


@pytest.mark.fr("FR-002")
def test_verdict_type_and_fields(tmp_path):
    bundle, trust = build(tmp_path, "skill", ("no-cert",))
    v = verify(bundle, trust)
    assert isinstance(v, prov().Verdict)
    assert v.ok is False and v.reason == "no-cert"
    assert hasattr(v, "cert_id")


@pytest.mark.fr("FR-002")
def test_verify_accepts_str_path(tmp_path):
    bundle, trust = build(tmp_path, "skill", ())
    assert verify(str(bundle), trust).ok is True


@pytest.mark.fr("FR-002")
@pytest.mark.parametrize("io", [
    {"inputs": ["file:pdf"]},
    {"inputs": {"all": ["path"], "any": ["file:pdf", "file:office"]},
     "outputs": ["text/markdown"], "effects": ["write-workspace", "read-workspace"]},
    {},
    {"outputs": ["none"], "effects": ["none"]},
], ids=["shorthand-defaults", "dict-form-unsorted", "empty-block", "outputs-none"])
def test_io_is_compared_after_normalisation(tmp_path, io):
    alpha = Signer("alpha")
    bundle = write_skill(tmp_path / "ws", "h012-io", io=io)
    write_cert(bundle, make_cert(bundle, alpha, io=ref_io(io)), alpha)
    assert verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha))).ok is True


@pytest.mark.fr("FR-002")
def test_script_tool_io_comes_from_tool_yaml(tmp_path):
    alpha = Signer("alpha")
    io = {"inputs": {"any": ["data/csv", "data/json"]}, "effects": ["network"]}
    bundle = write_script_tool(tmp_path / "ws", "h012-tio", io=io)
    write_cert(bundle, make_cert(bundle, alpha, io=ref_io(io)), alpha)
    trust = trust_load(trust_dir(tmp_path / "t", alpha))
    assert verify(bundle, trust).ok is True
    other = ref_io(io)
    other["effects"] = ["exec"]
    write_cert(bundle, make_cert(bundle, alpha, io=other), alpha)
    assert _reason(verify(bundle, trust)) == "io-mismatch"


@pytest.mark.fr("FR-002")
def test_parent_lineage_and_scores_do_not_matter(tmp_path):
    alpha = Signer("alpha")
    bundle = write_skill(tmp_path / "ws", "h012-lin")
    write_cert(bundle, make_cert(bundle, alpha, lineage={"parent": "0123456789abcdef"},
                                 scores={"x": 0.25}), alpha)
    assert verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha))).ok is True


@pytest.mark.fr("FR-002")
def test_cert_json_rewrite_with_same_content_still_ok(tmp_path):
    """CERT.json formatting is irrelevant: the signature covers the canonical cert."""
    alpha = Signer("alpha")
    bundle = write_skill(tmp_path / "ws", "h012-fmt")
    cert = make_cert(bundle, alpha)
    doc = {"sig": alpha.sig_b64(cert), "cert": dict(reversed(list(cert.items())))}
    (bundle / "CERT.json").write_text(__import__("json").dumps(doc), encoding="utf-8")
    assert verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha))).ok is True


@pytest.mark.fr("FR-002")
def test_tampered_scores_break_signature(tmp_path):
    alpha = Signer("alpha")
    bundle = write_skill(tmp_path / "ws", "h012-tamper")
    cert = make_cert(bundle, alpha)
    sig = alpha.sig_b64(cert)
    cert["scores"] = {"pass_rate": 0.0}
    write_cert(bundle, cert, alpha, sig=sig)
    assert _reason(verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha)))) == \
        "bad-signature"


@pytest.mark.fr("FR-002")
def test_garbage_signature_is_bad_signature(tmp_path):
    alpha = Signer("alpha")
    bundle = write_skill(tmp_path / "ws", "h012-garbage")
    import base64

    write_cert(bundle, make_cert(bundle, alpha), alpha,
               sig=base64.b64encode(b"\0" * 64).decode())
    assert _reason(verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha)))) == \
        "bad-signature"


@pytest.mark.fr("FR-002")
def test_second_trusted_key_verifies(tmp_path):
    alpha, gamma = Signer("alpha"), Signer("gamma")
    bundle = write_skill(tmp_path / "ws", "h012-gamma")
    write_cert(bundle, make_cert(bundle, gamma), gamma)
    assert verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha, gamma))).ok is True


# -- one fixture per code (SC-001) ---------------------------------------------------------------


@pytest.mark.fr("FR-002", "SC-001")
@pytest.mark.parametrize("kind", ["skill", "script-tool"])
@pytest.mark.parametrize("code", VERIFY_CODES)
def test_each_code_from_its_own_fixture(tmp_path, code, kind):
    bundle, trust = build(tmp_path, kind, (code,))
    v = verify(bundle, trust)
    assert v.ok is False
    assert v.reason == code


@pytest.mark.fr("SC-001")
@pytest.mark.parametrize("kind", ["skill", "script-tool"])
def test_fixture_to_code_is_a_bijection(tmp_path, kind):
    produced = {}
    for code in ("ok", *VERIFY_CODES):
        root = tmp_path / code
        bundle, trust = build(root, kind, () if code == "ok" else (code,))
        produced[code] = _reason(verify(bundle, trust))
    assert produced == {c: c for c in ("ok", *VERIFY_CODES)}
    assert len(set(produced.values())) == len(produced)


@pytest.mark.fr("FR-002", "SC-001")
@pytest.mark.parametrize("variant", ["symlink-dir", "fifo", "nested-symlink"])
def test_bad_bundle_variants(tmp_path, variant):
    alpha = Signer("alpha")
    bundle = write_skill(tmp_path / "ws", NAME)
    write_cert(bundle, make_cert(bundle, alpha), alpha)
    if variant == "symlink-dir":
        (tmp_path / "elsewhere").mkdir()
        os.symlink(tmp_path / "elsewhere", bundle / "linked")
    elif variant == "fifo":
        os.mkfifo(bundle / "pipe")
    else:
        (bundle / "sub").mkdir()
        os.symlink("../SKILL.md", bundle / "sub" / "x.md")
    assert _reason(verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha)))) == "bad-bundle"


@pytest.mark.fr("FR-002", "SC-001")
@pytest.mark.parametrize("over", [
    {"scores": DELETE}, {"extra_field": True}, {"schema": "moeka.capcert.v0"},
    {"kind": "plugin"}, {"bundle_sha256": "short"}, {"key_id": "abc"}, {"scope": "global"},
    {"io": "untyped"},
], ids=["missing", "extra", "schema", "kind", "sha", "keyid", "scope", "io"])
def test_bad_schema_variants(tmp_path, over):
    alpha = Signer("alpha")
    bundle = write_skill(tmp_path / "ws", NAME)
    write_cert(bundle, make_cert(bundle, alpha, **over), alpha)
    assert _reason(verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha)))) == "bad-schema"


@pytest.mark.fr("FR-002", "SC-001")
@pytest.mark.parametrize("text", ["", "not json", "{", '{"cert": }'],
                         ids=["empty", "text", "open-brace", "broken"])
def test_bad_json_variants(tmp_path, text):
    alpha = Signer("alpha")
    bundle = write_skill(tmp_path / "ws", NAME)
    (bundle / "CERT.json").write_text(text, encoding="utf-8")
    assert _reason(verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha)))) == "bad-json"


@pytest.mark.fr("FR-002", "SC-001")
@pytest.mark.parametrize("where", ["main", "nested", "added", "removed"])
def test_hash_mismatch_variants(tmp_path, where):
    alpha = Signer("alpha")
    bundle = write_skill(tmp_path / "ws", NAME, extra_files={"ref/notes.txt": "MARK notes\n"})
    write_cert(bundle, make_cert(bundle, alpha), alpha)
    if where == "main":
        flip_byte(bundle / "SKILL.md")
    elif where == "nested":
        flip_byte(bundle / "ref" / "notes.txt")
    elif where == "added":
        (bundle / "ref" / "extra.txt").write_text("x", encoding="utf-8")
    else:
        (bundle / "ref" / "notes.txt").unlink()
    assert _reason(verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha)))) == \
        "hash-mismatch"


@pytest.mark.fr("FR-002", "SC-001")
def test_renamed_dir_is_name_mismatch(tmp_path):
    alpha = Signer("alpha")
    bundle = write_skill(tmp_path / "ws", NAME)
    write_cert(bundle, make_cert(bundle, alpha), alpha)
    moved = bundle.parent / "h012-renamed"
    os.rename(bundle, moved)
    assert _reason(verify(moved, trust_load(trust_dir(tmp_path / "t", alpha)))) == "name-mismatch"


# -- check order: pairwise precedence ------------------------------------------------------------

# bad-bundle is listed last, but a bundle that cannot be hashed fails at the hash step: the
# order relative to hash-mismatch / io-mismatch / revoked is ambiguous, so those pairs are not
# asserted (SPEC-MAP interpretation 3).
_AMBIGUOUS = {("hash-mismatch", "bad-bundle"), ("io-mismatch", "bad-bundle"),
              ("revoked", "bad-bundle")}
PAIRS = [p for p in itertools.combinations(VERIFY_CODES, 2) if p not in _AMBIGUOUS]


@pytest.mark.fr("FR-002")
@pytest.mark.parametrize("pair", PAIRS, ids=["+".join(p) for p in PAIRS])
def test_first_failure_in_listed_order_wins(tmp_path, pair):
    bundle, trust = build(tmp_path, "skill", pair)
    assert _reason(verify(bundle, trust)) == pair[0]


@pytest.mark.fr("FR-002")
def test_everything_wrong_but_a_cert_is_bad_schema_or_earlier(tmp_path):
    codes = tuple(c for c in VERIFY_CODES if c not in ("no-cert", "bad-json"))
    bundle, trust = build(tmp_path, "script-tool", codes)
    assert _reason(verify(bundle, trust)) == "bad-schema"


@pytest.mark.fr("FR-002")
def test_everything_after_signature_reports_name_mismatch(tmp_path):
    codes = ("name-mismatch", "kind-mismatch", "scope-mismatch", "hash-mismatch", "io-mismatch",
             "revoked", "bad-bundle")
    bundle, trust = build(tmp_path, "skill", codes)
    assert _reason(verify(bundle, trust)) == "name-mismatch"
