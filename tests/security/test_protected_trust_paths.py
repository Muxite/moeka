"""Spec 012 FR-008: the file tools never write certificates, revocations or trust dirs."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.capabilities.provenance import BUILTIN_TRUST_DIR
from nanobot.security.protected_paths import (
    PROTECTED_MARKER,
    ProtectedFloor,
    configured_trust_dirs,
)


def _floor(tmp_path: Path, trust: Path | None = None) -> ProtectedFloor:
    return ProtectedFloor(data_dir=tmp_path / "data", workspace=tmp_path / "ws",
                          trust_dirs=[trust] if trust else ())


@pytest.mark.parametrize("name", ["CERT.json", "revoked.jsonl"])
def test_cert_and_revocation_files_are_write_denied_anywhere(tmp_path, name):
    floor = _floor(tmp_path)
    target = tmp_path / "ws" / "skills" / "x" / name
    assert floor.matches(target, write=True)
    assert not floor.matches(target, write=False)  # reading stays allowed
    assert not floor.matches(tmp_path / "ws" / "skills" / "x" / "SKILL.md", write=True)


def test_configured_and_builtin_trust_dirs_are_write_denied(tmp_path):
    trust = tmp_path / "trust"
    trust.mkdir()
    floor = _floor(tmp_path, trust)
    assert floor.matches(trust / "evil.pub", write=True)
    assert floor.matches(trust, write=True)
    assert not floor.matches(trust / "evil.pub", write=False)
    assert floor.matches(BUILTIN_TRUST_DIR / "new.pub", write=True)
    assert not _floor(tmp_path).matches(trust / "evil.pub", write=True)


def test_symlink_into_a_trust_dir_is_denied(tmp_path):
    trust = tmp_path / "trust"
    trust.mkdir()
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "alias").symlink_to(trust)
    assert _floor(tmp_path, trust).matches(tmp_path / "ws" / "alias" / "k.pub", write=True)


def test_transfer_staging_and_approval_state_are_internal(tmp_path):
    floor = _floor(tmp_path)
    target = tmp_path / "ws" / ".moeka-transfer" / "promotions" / "pending" / "id.json"
    assert floor.matches(target, write=True)
    assert floor.matches(target, write=False)


def test_configured_trust_dirs_reads_the_env_config(tmp_path):
    from moeka import Environment
    from nanobot.config.schema import Config

    env = Environment.from_config(
        Config(capabilities={"trust_dirs": [str(tmp_path / "t")]}),
        state_dir=tmp_path / "state", work_dir=tmp_path / "ws",
    )
    assert configured_trust_dirs(env.core) == [tmp_path / "t"]
    assert configured_trust_dirs(None) == []


async def test_write_file_tool_refuses_trust_writes(tmp_path):
    from moeka import Environment
    from nanobot.agent.tools.context import ToolContext
    from nanobot.agent.tools.filesystem import WriteFileTool
    from nanobot.config.schema import Config

    work = tmp_path / "ws"
    work.mkdir()
    trust = work / "trust"
    trust.mkdir()
    config = Config(capabilities={"trust_dirs": [str(trust)]})
    env = Environment.from_config(config, state_dir=tmp_path / "state", work_dir=work)
    tool = WriteFileTool.create(ToolContext(config=config.tools, workspace=str(work),
                                            env=env.core))
    for target in (trust / "k.pub", work / "skills" / "a" / "CERT.json",
                   work / "revoked.jsonl"):
        result = await tool.execute(path=str(target), content="x")
        assert PROTECTED_MARKER in str(result), target
        assert not target.exists()
    ok = await tool.execute(path=str(work / "notes.txt"), content="x")
    assert PROTECTED_MARKER not in str(ok)
    assert (work / "notes.txt").read_text() == "x"


def test_gate_floor_includes_configured_trust_dirs(tmp_path):
    from moeka import Environment
    from nanobot.config.schema import Config
    from nanobot.kernel.gate import protected_floor

    trust = tmp_path / "trust"
    env = Environment.from_config(Config(capabilities={"trust_dirs": [str(trust)]}),
                                  state_dir=tmp_path / "state", work_dir=tmp_path / "ws")
    floor = protected_floor(env.core, tmp_path / "ws")
    assert floor.matches(trust / "k.pub", write=True)
