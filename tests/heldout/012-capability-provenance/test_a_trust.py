"""Part A: the trust store (FR-003) and no signing in moeka (FR-004, SC-004)."""

from __future__ import annotations

import base64
import inspect
import json
import re

import pytest
from _h012 import (
    Signer,
    certify,
    cli,
    prov,
    repo_root,
    revoke,
    trust_dir,
    trust_load,
    verify,
    write_skill,
)

REPO = repo_root()


def _reason(v) -> str:
    return "ok" if v.ok else v.reason


@pytest.fixture
def signed(tmp_path):
    """(bundle, cert_id, alpha) for a skill signed by the fake key ``alpha``."""
    alpha = Signer("alpha")
    bundle = write_skill(tmp_path / "ws", "h012-trust")
    return bundle, certify(bundle, alpha), alpha


# -- load ------------------------------------------------------------------------------------------


@pytest.mark.fr("FR-003")
def test_load_reads_pub_files(tmp_path, signed):
    bundle, cid, alpha = signed
    v = verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha)))
    assert v.ok and v.cert_id == cid


@pytest.mark.fr("FR-003")
def test_load_returns_a_trust_store(tmp_path, signed):
    _, _, alpha = signed
    assert isinstance(trust_load(trust_dir(tmp_path / "t", alpha)), prov().TrustStore)


@pytest.mark.fr("FR-003")
def test_empty_dir_trusts_nothing(tmp_path, signed):
    bundle, _, _ = signed
    (tmp_path / "empty").mkdir()
    assert _reason(verify(bundle, trust_load(tmp_path / "empty"))) == "unknown-key"


@pytest.mark.fr("FR-003")
def test_load_of_no_dirs_trusts_nothing(signed):
    bundle, _, _ = signed
    assert _reason(verify(bundle, prov().TrustStore.load([]))) == "unknown-key"


@pytest.mark.fr("FR-003")
def test_key_in_any_listed_dir_is_trusted(tmp_path, signed):
    bundle, _, alpha = signed
    d1 = trust_dir(tmp_path / "t1", Signer("beta"))
    d2 = trust_dir(tmp_path / "t2", alpha)
    assert verify(bundle, trust_load(d1, d2)).ok


@pytest.mark.fr("FR-003")
def test_only_pub_suffix_is_read(tmp_path, signed):
    bundle, _, alpha = signed
    d = tmp_path / "t"
    d.mkdir()
    (d / "alpha.key").write_text(alpha.pub_b64 + "\n", encoding="utf-8")
    (d / "alpha.pub.bak").write_text(alpha.pub_b64 + "\n", encoding="utf-8")
    assert _reason(verify(bundle, trust_load(d))) == "unknown-key"


@pytest.mark.fr("FR-003")
def test_pub_without_trailing_newline(tmp_path, signed):
    bundle, _, alpha = signed
    d = tmp_path / "t"
    d.mkdir()
    (d / "a.pub").write_text(alpha.pub_b64, encoding="utf-8")
    assert verify(bundle, trust_load(d)).ok


@pytest.mark.fr("FR-003")
def test_duplicate_keys_are_fine(tmp_path, signed):
    bundle, _, alpha = signed
    d = trust_dir(tmp_path / "t", alpha)
    alpha.write_pub(d, "alpha-copy")
    assert verify(bundle, trust_load(d)).ok
    d2 = trust_dir(tmp_path / "t2", alpha)
    assert verify(bundle, trust_load(d, d2)).ok


MALFORMED = {
    "not-base64": "!!!! this is not base64 ****\n",
    "short-key": base64.b64encode(b"\1" * 31).decode() + "\n",
    "long-key": base64.b64encode(b"\1" * 33).decode() + "\n",
    "empty": "",
    "pem-like": "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA\n-----END PUBLIC KEY-----\n",
}


@pytest.mark.fr("FR-003")
@pytest.mark.parametrize("kind", sorted(MALFORMED))
def test_malformed_pub_is_skipped_with_warning(tmp_path, signed, warned, kind):
    bundle, _, alpha = signed
    d = trust_dir(tmp_path / "t", alpha)
    (d / f"broken-{kind}.pub").write_text(MALFORMED[kind], encoding="utf-8")
    trust = trust_load(d)
    assert verify(bundle, trust).ok, "the good key next to a malformed one must still load"
    assert warned(), "a malformed .pub must be reported with a warning"


@pytest.mark.fr("FR-003")
def test_malformed_only_dir_trusts_nothing(tmp_path, signed, warned):
    bundle, _, _ = signed
    d = tmp_path / "t"
    d.mkdir()
    (d / "bad.pub").write_text("@@@\n", encoding="utf-8")
    assert _reason(verify(bundle, trust_load(d))) == "unknown-key"
    assert warned()


# -- revocations ---------------------------------------------------------------------------------


@pytest.mark.fr("FR-003")
def test_revoked_jsonl_revokes(tmp_path, signed):
    bundle, cid, alpha = signed
    v = verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha, revoked=[cid])))
    assert _reason(v) == "revoked"


@pytest.mark.fr("FR-003")
def test_other_revocations_do_not_affect(tmp_path, signed):
    bundle, cid, alpha = signed
    other = "f" * 16 if cid != "f" * 16 else "e" * 16
    assert verify(bundle, trust_load(trust_dir(tmp_path / "t", alpha, revoked=[other]))).ok


@pytest.mark.fr("FR-003")
def test_revocation_in_another_dir_applies(tmp_path, signed):
    bundle, cid, alpha = signed
    d1 = trust_dir(tmp_path / "keys", alpha)
    d2 = tmp_path / "revs"
    revoke(d2, cid)
    assert _reason(verify(bundle, trust_load(d1, d2))) == "revoked"


@pytest.mark.fr("FR-003")
@pytest.mark.parametrize("bad", ["{not json", "[1, 2", "cert_id=abc"],
                         ids=["brace", "array", "plain"])
def test_bad_revoked_line_is_skipped_with_warning(tmp_path, signed, warned, bad):
    bundle, cid, alpha = signed
    good = json.dumps({"cert_id": cid, "reason": "r", "at": "2026-10-03T00:00:00Z"})
    d = trust_dir(tmp_path / "t", alpha, extra_lines=[bad, good])
    trust = trust_load(d)
    assert _reason(verify(bundle, trust)) == "revoked", "the valid line after a bad one counts"
    assert warned()


@pytest.mark.fr("FR-003")
def test_bad_line_alone_revokes_nothing(tmp_path, signed, warned):
    bundle, _, alpha = signed
    d = trust_dir(tmp_path / "t", alpha, extra_lines=["{oops"])
    assert verify(bundle, trust_load(d)).ok
    assert warned()


# -- from_keys -----------------------------------------------------------------------------------


@pytest.mark.fr("FR-003")
def test_from_keys_trusts_given_keys(signed):
    bundle, cid, alpha = signed
    v = verify(bundle, prov().TrustStore.from_keys([alpha.pub]))
    assert v.ok and v.cert_id == cid


@pytest.mark.fr("FR-003")
def test_from_keys_with_revoked(signed):
    bundle, cid, alpha = signed
    assert _reason(verify(bundle, prov().TrustStore.from_keys([alpha.pub], {cid}))) == "revoked"


@pytest.mark.fr("FR-003")
def test_from_keys_other_key_is_unknown(signed):
    bundle, _, _ = signed
    store = prov().TrustStore.from_keys([Signer("beta").pub])
    assert _reason(verify(bundle, store)) == "unknown-key"


@pytest.mark.fr("FR-003")
def test_from_keys_empty(signed):
    bundle, _, _ = signed
    assert _reason(verify(bundle, prov().TrustStore.from_keys([]))) == "unknown-key"


@pytest.mark.fr("FR-003")
def test_from_keys_duplicates_fine(signed):
    bundle, _, alpha = signed
    assert verify(bundle, prov().TrustStore.from_keys([alpha.pub, alpha.pub])).ok


# -- shipped trust dir ---------------------------------------------------------------------------


@pytest.mark.fr("FR-003")
def test_package_trust_dir_ships_empty():
    d = REPO / "nanobot" / "capabilities" / "trust"
    assert d.is_dir(), "nanobot/capabilities/trust/ missing"
    assert list(d.glob("*.pub")) == []


# -- FR-004 / SC-004: no signing in moeka --------------------------------------------------------

PRIVATE_MARKERS = ("Ed25519PrivateKey", "from_private_bytes", "load_pem_private_key")


def _py_files(*roots):
    for root in roots:
        yield from sorted((REPO / root).rglob("*.py"))


@pytest.mark.fr("FR-004", "SC-004")
def test_capabilities_package_has_no_private_key_handling():
    hits = []
    for path in _py_files("nanobot/capabilities"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in (*PRIVATE_MARKERS, "generate("):
            if marker in text:
                hits.append((path.relative_to(REPO).as_posix(), marker))
    assert hits == []


@pytest.mark.fr("FR-004", "SC-004")
def test_moeka_source_has_no_private_key_handling():
    hits = []
    for path in _py_files("nanobot", "moeka"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in PRIVATE_MARKERS:
            if marker in text:
                hits.append((path.relative_to(REPO).as_posix(), marker))
    assert hits == []


@pytest.mark.fr("FR-004", "SC-004")
def test_capabilities_package_never_imports_private_key_classes():
    pat = re.compile(r"ed25519\.Ed25519PrivateKey|PrivateFormat|private_bytes\(")
    hits = [p.name for p in _py_files("nanobot/capabilities")
            if pat.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert hits == []


def _public_callables(mod):
    names = getattr(mod, "__all__", None) or [n for n in dir(mod) if not n.startswith("_")]
    return {n for n in names if callable(getattr(mod, n, None))}


@pytest.mark.fr("FR-004", "SC-004")
@pytest.mark.parametrize("module", ["provenance", "script_tool"])
def test_public_api_has_no_sign_function(module):
    from _h012 import cap

    mod = cap(module)
    names = {n for n in dir(mod) if not n.startswith("_")
             and not inspect.ismodule(getattr(mod, n, None))}
    assert not [n for n in names if re.match(r"^sign($|_|[A-Z])", n)]
    for name in _public_callables(mod):
        obj = getattr(mod, name)
        if inspect.isclass(obj):
            assert not [m for m in dir(obj)
                        if not m.startswith("_") and (m == "sign" or m.startswith("sign_"))], name


@pytest.mark.fr("FR-004", "SC-004")
def test_trust_store_has_no_private_key_constructor():
    ts = prov().TrustStore
    bad = [m for m in dir(ts) if "private" in m.lower() or m.lower().startswith("sign")]
    assert bad == []


@pytest.mark.fr("FR-004", "SC-004")
def test_cli_has_no_sign_command(tmp_path):
    proc = cli(tmp_path, "--help")
    assert proc.returncode == 0, proc.stderr[-2000:]
    commands = re.findall(r"^\s*[│|]?\s*([a-z][a-z-]*)\s{2,}", proc.stdout, re.MULTILINE)
    assert "sign" not in commands
    assert not re.search(r"(^|\s)sign(\s|$)", proc.stdout.replace("Sign", ""))
