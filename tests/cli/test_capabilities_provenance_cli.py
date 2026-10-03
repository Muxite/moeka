"""``nanobot capabilities verify|hash`` and the ``list`` status column (spec 012 FR-012)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "capabilities"))
from _certkit import Key, certify, make_script, make_skill, write_trust  # noqa: E402

from nanobot.capabilities.provenance import bundle_sha256  # noqa: E402


def _run(tmp_path: Path, *args: str, config: dict | None = None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    extra: list[str] = []
    if config is not None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config))
        extra = ["--config", str(path)]
    env = {
        **os.environ, "HOME": str(home), "MOEKA_WORKSPACE": str(tmp_path / "unused"),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(Path.cwd()),
                                                    os.environ.get("PYTHONPATH")])),
    }
    env.pop("NANOBOT_HOME", None)
    return subprocess.run(
        [sys.executable, "-m", "nanobot", "capabilities", *args, *extra],
        capture_output=True, text=True, env=env, timeout=120,
    )


@pytest.fixture
def signed(tmp_path):
    key = Key()
    trust = write_trust(tmp_path / "trust", [key])
    bundle = make_skill(tmp_path / "ws", "good-skill")
    cert_id = certify(bundle, key)
    return trust, bundle, cert_id, key


def test_verify_ok(tmp_path, signed):
    trust, bundle, cert_id, _ = signed
    result = _run(tmp_path, "verify", str(bundle), "--trust-dir", str(trust))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ok {cert_id}"


def test_verify_refused(tmp_path, signed):
    trust, bundle, _, _ = signed
    (bundle / "extra.txt").write_text("x")
    result = _run(tmp_path, "verify", str(bundle), "--trust-dir", str(trust))
    assert result.returncode == 1
    assert result.stdout.strip() == "refused hash-mismatch"
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run(tmp_path, "verify", str(bundle), "--trust-dir", str(empty))
    assert (result.returncode, result.stdout.strip()) == (1, "refused unknown-key")


def test_verify_uses_config_trust_dirs(tmp_path, signed):
    trust, bundle, cert_id, _ = signed
    result = _run(tmp_path, "verify", str(bundle),
                  config={"capabilities": {"trustDirs": [str(trust)]}})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ok {cert_id}"


def test_hash(tmp_path, signed):
    _, bundle, _, _ = signed
    result = _run(tmp_path, "hash", str(bundle))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == bundle_sha256(bundle)
    (bundle / "link").symlink_to("SKILL.md")
    result = _run(tmp_path, "hash", str(bundle))
    assert result.returncode == 1 and "symlink" in result.stderr


@pytest.mark.parametrize("enforce", [False, True])
def test_list_shows_status_for_every_workspace_bundle(tmp_path, signed, enforce):
    trust, bundle, cert_id, key = signed
    workspace = bundle.parent.parent
    make_skill(workspace, "plain-skill")
    tampered = make_skill(workspace, "bad-skill")
    certify(tampered, key)
    (tampered / "x.txt").write_text("x")
    certify(make_script(workspace, "echo", "#!/bin/sh\necho 1\n"), key)
    config = {"capabilities": {"trustDirs": [str(trust)], "enforceCerts": enforce}}
    result = _run(tmp_path, "list", "--json", "--workspace", str(workspace), config=config)
    assert result.returncode == 0, result.stderr
    rows = {(row["kind"], row["name"]): row for row in json.loads(result.stdout)}
    assert rows[("skill", "good-skill")]["cert_status"] == "certified"
    assert rows[("skill", "good-skill")]["cert_id"] == cert_id
    assert rows[("skill", "plain-skill")]["cert_status"] == (
        "refused:no-cert" if enforce else "uncertified"
    )
    assert rows[("skill", "bad-skill")]["cert_status"] == "refused:hash-mismatch"
    assert rows[("skill", "weather")]["cert_status"] == "builtin"
    assert rows[("tool", "exec")]["cert_status"] == "builtin"
    assert rows[("tool", "script_echo")]["cert_status"] == "certified"
    assert rows[("tool", "script_echo")]["available"] is False  # tools.script.enabled off
    if enforce:
        assert rows[("skill", "plain-skill")]["available"] is False

    text = _run(tmp_path, "list", "--workspace", str(workspace), config=config)
    assert text.returncode == 0, text.stderr
    lines = {line.split("\t")[1]: line.split("\t") for line in text.stdout.splitlines()}
    assert lines["good-skill"][-1] == "certified"
    assert lines["bad-skill"][-1] == "refused:hash-mismatch"
