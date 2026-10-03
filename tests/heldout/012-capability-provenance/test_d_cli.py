"""Part D: the CLI (FR-012) ``capabilities verify|hash|list`` and the status column (FR-006)."""

from __future__ import annotations

import json
import os

import pytest
from _h012 import (
    Signer,
    certify,
    cli,
    flip_byte,
    has_token,
    line_for,
    make_cert,
    ref_bundle_sha256,
    repo_root,
    revoke,
    trust_dir,
    write_cert,
    write_config,
    write_script_tool,
    write_skill,
)

REPO = repo_root()


def _last(stdout: str) -> str:
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


# -- verify -----------------------------------------------------------------------------------------


@pytest.mark.fr("FR-012")
def test_verify_ok(tmp_path):
    alpha = Signer("alpha")
    b = write_skill(tmp_path / "ws", "h012-cli")
    cid = certify(b, alpha)
    t = trust_dir(tmp_path / "trust", alpha)
    proc = cli(tmp_path, "verify", str(b), "--trust-dir", str(t))
    assert proc.returncode == 0, proc.stdout + proc.stderr[-2000:]
    assert _last(proc.stdout) == f"ok {cid}"


@pytest.mark.fr("FR-012")
def test_verify_ok_script_tool(tmp_path):
    alpha = Signer("alpha")
    b = write_script_tool(tmp_path / "ws", "h012-cli-tool")
    cid = certify(b, alpha)
    proc = cli(tmp_path, "verify", str(b), "--trust-dir", str(trust_dir(tmp_path / "t", alpha)))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _last(proc.stdout) == f"ok {cid}"


@pytest.mark.fr("FR-012")
def test_verify_repeated_trust_dir(tmp_path):
    alpha = Signer("alpha")
    b = write_skill(tmp_path / "ws", "h012-cli")
    cid = certify(b, alpha)
    t1 = trust_dir(tmp_path / "t1", Signer("beta"))
    t2 = trust_dir(tmp_path / "t2", alpha)
    proc = cli(tmp_path, "verify", str(b), "--trust-dir", str(t1), "--trust-dir", str(t2))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _last(proc.stdout) == f"ok {cid}"


@pytest.mark.fr("FR-012")
def test_verify_without_trust_dir_is_unknown_key(tmp_path):
    alpha = Signer("alpha")
    b = write_skill(tmp_path / "ws", "h012-cli")
    certify(b, alpha)
    proc = cli(tmp_path, "verify", str(b))
    assert proc.returncode == 1, proc.stdout + proc.stderr[-2000:]
    assert _last(proc.stdout) == "refused unknown-key"


@pytest.mark.fr("FR-012")
@pytest.mark.parametrize("code", ["no-cert", "bad-json", "bad-signature", "hash-mismatch",
                                  "revoked", "scope-mismatch"])
def test_verify_refused(tmp_path, code):
    alpha, beta = Signer("alpha"), Signer("beta")
    b = write_skill(tmp_path / "ws", "h012-cli")
    t = trust_dir(tmp_path / "trust", alpha)
    if code == "bad-json":
        (b / "CERT.json").write_text("{nope", encoding="utf-8")
    elif code == "bad-signature":
        cert = make_cert(b, alpha)
        write_cert(b, cert, alpha, sig=beta.sig_b64(cert))
    elif code == "hash-mismatch":
        certify(b, alpha)
        flip_byte(b / "SKILL.md")
    elif code == "revoked":
        revoke(t, certify(b, alpha))
    elif code == "scope-mismatch":
        certify(b, alpha, scope="kernel")
    proc = cli(tmp_path, "verify", str(b), "--trust-dir", str(t))
    assert proc.returncode == 1, proc.stdout + proc.stderr[-2000:]
    assert _last(proc.stdout) == f"refused {code}"


# -- hash -------------------------------------------------------------------------------------------


@pytest.mark.fr("FR-012")
def test_hash_prints_bundle_hash(tmp_path):
    b = write_skill(tmp_path / "ws", "h012-cli", extra_files={"a/b.txt": "x", "a-b.txt": "y"})
    (b / "CERT.json").write_text("{}", encoding="utf-8")
    proc = cli(tmp_path, "hash", str(b))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _last(proc.stdout) == ref_bundle_sha256(b)


@pytest.mark.fr("FR-012")
def test_hash_script_tool(tmp_path):
    b = write_script_tool(tmp_path / "ws", "h012-cli-tool")
    proc = cli(tmp_path, "hash", str(b))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _last(proc.stdout) == ref_bundle_sha256(b)


@pytest.mark.fr("FR-012")
def test_hash_of_bundle_with_symlink_fails(tmp_path):
    b = write_skill(tmp_path / "ws", "h012-cli")
    os.symlink("SKILL.md", b / "alias")
    proc = cli(tmp_path, "hash", str(b))
    assert proc.returncode != 0
    assert len(_last(proc.stdout)) != 64


# -- list status column (FR-006 / FR-012) -------------------------------------------------------------


def _list_fixture(tmp_path, enforce):
    alpha, beta = Signer("alpha"), Signer("beta")
    ws = tmp_path / "ws"
    t = trust_dir(tmp_path / "trust", alpha)
    certify(write_skill(ws, "h012-l-cert"), alpha)
    write_skill(ws, "h012-l-plain")
    b = write_skill(ws, "h012-l-badsig")
    cert = make_cert(b, alpha)
    write_cert(b, cert, alpha, sig=beta.sig_b64(cert))
    revoke(t, certify(write_skill(ws, "h012-l-revoked"), alpha))
    certify(write_script_tool(ws, "h012-l-tcert"), alpha)
    write_script_tool(ws, "h012-l-tplain")
    cfg = write_config(tmp_path / "cfg" / "config.json",
                       caps={"enforceCerts": enforce, "trustDirs": [str(t)]},
                       script_enabled=True, workspace=ws)
    return ws, cfg


def _builtin_skill_name():
    names = sorted(p.parent.name for p in (REPO / "nanobot" / "skills").glob("*/SKILL.md"))
    assert names, "no bundled skills"
    return names[0]


EXPECTED = {
    False: {"h012-l-cert": "certified", "h012-l-plain": "uncertified",
            "h012-l-badsig": "refused:bad-signature", "h012-l-revoked": "refused:revoked",
            "h012-l-tcert": "certified", "h012-l-tplain": "uncertified"},
    True: {"h012-l-cert": "certified", "h012-l-plain": "refused:no-cert",
           "h012-l-badsig": "refused:bad-signature", "h012-l-revoked": "refused:revoked",
           "h012-l-tcert": "certified", "h012-l-tplain": "refused:no-cert"},
}


@pytest.mark.fr("FR-012", "FR-006")
@pytest.mark.parametrize("enforce", [False, True], ids=["off", "on"])
def test_list_status_column(tmp_path, enforce):
    ws, cfg = _list_fixture(tmp_path, enforce)
    proc = cli(tmp_path, "list", "--config", str(cfg), "--workspace", str(ws))
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = proc.stdout
    for name, status in EXPECTED[enforce].items():
        line = line_for(out, name, f"script_{name}")
        assert line is not None, f"{name} missing from list:\n{out[-3000:]}"
        assert has_token(line, status), (name, status, line)
    builtin_line = line_for(out, _builtin_skill_name())
    assert builtin_line is not None and has_token(builtin_line, "builtin"), builtin_line
    tool_line = line_for(out, "read_file")
    assert tool_line is not None and has_token(tool_line, "builtin"), tool_line


@pytest.mark.fr("FR-012", "FR-006")
@pytest.mark.parametrize("enforce", [False, True], ids=["off", "on"])
def test_list_json_status(tmp_path, enforce):
    ws, cfg = _list_fixture(tmp_path, enforce)
    proc = cli(tmp_path, "list", "--json", "--config", str(cfg), "--workspace", str(ws))
    assert proc.returncode == 0, proc.stderr[-3000:]
    data = json.loads(proc.stdout)
    records = data if isinstance(data, list) else next(v for v in data.values()
                                                       if isinstance(v, list))

    def status_of(rec):
        return rec.get("cert_status", rec.get("status"))

    by_name = {}
    for rec in records:
        by_name.setdefault(rec["name"], rec)
        if str(rec["name"]).startswith("script_"):
            by_name.setdefault(rec["name"][len("script_"):], rec)
    for name, status in EXPECTED[enforce].items():
        assert name in by_name, f"{name} missing"
        assert status_of(by_name[name]) == status, (name, by_name[name])
    assert status_of(by_name["read_file"]) == "builtin"
    assert status_of(by_name[_builtin_skill_name()]) == "builtin"
