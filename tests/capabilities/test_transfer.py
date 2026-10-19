"""Certified capability distribution between independent workspaces."""

from __future__ import annotations

import pytest
from _certkit import Key, certify, make_script, make_skill, write_trust

from nanobot.capabilities.transfer import (
    TransferError,
    activate_pending,
    approve_activation,
    history,
    preview,
    rollback,
    stage,
)
from nanobot.config.schema import Config


def _config(workspace, trust, *, effects=None):
    return Config(
        agents={"defaults": {"workspace": str(workspace)}},
        capabilities={
            "trust_dirs": [str(trust)],
            "allowed_effects": effects or ["none", "read-workspace", "write-workspace"],
        },
    )


def test_stage_activate_and_rollback_certified_bundle(tmp_path):
    key = Key()
    src, dst = tmp_path / "src", tmp_path / "dst"
    source_bundle = make_skill(src, "portable", body="Version one.\n")
    certify(source_bundle, key)
    source_trust = write_trust(tmp_path / "src-trust", [key])
    dest_trust = write_trust(tmp_path / "dst-trust", [key])
    source, destination = _config(src, source_trust), _config(dst, dest_trust)

    old = make_skill(dst, "portable", body="Old destination copy.\n")
    # The prior local copy also needs a valid cert for rollback to be permitted.
    certify(old, key)
    assert preview(source, destination, "skill", "portable")["destination_compatible"]
    stage_id = stage(source, destination, "skill", "portable")
    approved = approve_activation(destination, stage_id)
    assert approved.is_file()
    assert "Old destination copy." in (dst / "skills/portable/SKILL.md").read_text()
    activated = activate_pending(destination)
    assert len(activated) == 1
    result = activated[0]
    assert "Version one." in (dst / "skills/portable/SKILL.md").read_text()
    assert len(history(dst)) == 1
    rollback(destination, str(result["activation_id"]))
    assert "Old destination copy." in (dst / "skills/portable/SKILL.md").read_text()
    assert history(dst) == []


def test_transfer_refuses_untrusted_revoked_or_overprivileged_bundle(tmp_path):
    key = Key()
    src, dst = tmp_path / "src", tmp_path / "dst"
    bundle = make_skill(src, "portable")
    cert_id = certify(bundle, key)
    source_trust = write_trust(tmp_path / "src-trust", [key], [cert_id])
    dest_trust = write_trust(tmp_path / "dst-trust", [key])
    source, destination = _config(src, source_trust), _config(dst, dest_trust)
    with pytest.raises(TransferError, match="revoked"):
        preview(source, destination, "skill", "portable")

    source_trust = write_trust(tmp_path / "source-trust-valid", [key])
    source = _config(src, source_trust)
    restrictive = _config(dst, dest_trust, effects=["none"])
    with pytest.raises(TransferError, match="disallows effects"):
        preview(source, restrictive, "skill", "portable")


def test_script_tool_waits_for_registry_restart_boundary(tmp_path):
    key = Key()
    src, dst = tmp_path / "src", tmp_path / "dst"
    bundle = make_script(src, "portable-tool", "#!/bin/sh\necho ok\n")
    certify(bundle, key)
    source_trust = write_trust(tmp_path / "src-trust", [key])
    dest_trust = write_trust(tmp_path / "dst-trust", [key])
    source = _config(src, source_trust)
    destination = Config(
        agents={"defaults": {"workspace": str(dst)}},
        capabilities={"trust_dirs": [str(dest_trust)], "allowed_effects": ["none"]},
        tools={"script": {"enabled": True}},
    )
    stage_id = stage(source, destination, "script-tool", "portable-tool")
    approve_activation(destination, stage_id)
    assert activate_pending(destination, allow_script_tools=False) == []
    assert not (dst / "tools-script/portable-tool").exists()
    assert len(activate_pending(destination)) == 1
    assert (dst / "tools-script/portable-tool/CERT.json").is_file()


def test_transfer_never_allows_sudo_effect(tmp_path):
    key = Key()
    src, dst = tmp_path / "src", tmp_path / "dst"
    bundle = make_skill(
        src, "sudo-capability",
        io={"inputs": ["text/query"], "outputs": ["text/markdown"], "effects": ["sudo"]},
    )
    certify(bundle, key)
    source_trust = write_trust(tmp_path / "src-trust", [key])
    dest_trust = write_trust(tmp_path / "dst-trust", [key])
    source, destination = _config(src, source_trust), _config(dst, dest_trust)
    destination.capabilities.allowed_effects.append("sudo")
    with pytest.raises(TransferError, match="sudo effects"):
        preview(source, destination, "skill", "sudo-capability")


def test_transfer_checks_exec_tool_permission_and_rejects_workspace_symlinks(tmp_path):
    key = Key()
    src, dst = tmp_path / "src", tmp_path / "dst"
    bundle = make_skill(
        src, "exec-capability",
        io={"inputs": ["text/query"], "outputs": ["text/markdown"], "effects": ["exec"]},
    )
    certify(bundle, key)
    source_trust = write_trust(tmp_path / "src-trust", [key])
    dest_trust = write_trust(tmp_path / "dst-trust", [key])
    source, destination = _config(src, source_trust), Config(
        agents={"defaults": {"workspace": str(dst)}},
        capabilities={"trust_dirs": [str(dest_trust)], "allowed_effects": ["exec"]},
        tools={"exec": {"enable": False}},
    )
    with pytest.raises(TransferError, match="execution tools are disabled"):
        preview(source, destination, "skill", "exec-capability")

    target = tmp_path / "external-skills"
    target.mkdir()
    dst.mkdir()
    (dst / "skills").symlink_to(target, target_is_directory=True)
    enabled = _config(dst, dest_trust)
    with pytest.raises(TransferError, match="symlink"):
        preview(source, enabled, "skill", "exec-capability")
