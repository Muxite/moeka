from types import SimpleNamespace

import pytest

from nanobot.agent.personality import activate, restore, stage, versions
from nanobot.agent.tools.sandbox import wrap_command
from nanobot.security.protected_paths import ProtectedFloor


def env(allowed):
    return SimpleNamespace(config=SimpleNamespace(section=lambda name: {
        "allow_self_edit": allowed, "max_words": 150, "name": "Wonny"}))


def test_revision_is_staged_versioned_and_restorable(tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("Original personality.\n")
    stage(tmp_path, "Revised personality.", env(True))
    assert soul.read_text() == "Original personality.\n"
    activate(tmp_path, env(True))
    assert soul.read_text() == "Revised personality.\n"
    import hashlib
    original = hashlib.sha256(b"Original personality.\n").hexdigest()
    assert original in versions(tmp_path)
    restore(tmp_path, original, env(True))
    assert soul.read_text() == "Revised personality.\n"
    activate(tmp_path, env(True))
    assert soul.read_text() == "Original personality.\n"


def test_main_permission_and_word_limit(tmp_path):
    with pytest.raises(PermissionError):
        stage(tmp_path, "Change policy", env(False))
    with pytest.raises(ValueError):
        stage(tmp_path, "word " * 151, env(True))
    (tmp_path / "SOUL.md").symlink_to(tmp_path / "outside")
    with pytest.raises(PermissionError):
        stage(tmp_path, "edit", env(True))


def test_fixed_policy_and_personality_protected_from_files_and_exec(tmp_path):
    for name in ("AGENTS.md", "SOUL.md", "config.json", "keys.env"):
        (tmp_path / name).write_text("owner content")
    floor = ProtectedFloor(data_dir=tmp_path, workspace=tmp_path,
                           config_files=[tmp_path / "config.json"])
    for name in ("AGENTS.md", "SOUL.md", "config.json", ".personality/pending"):
        assert floor.reason(tmp_path / name, write=True)
    command = wrap_command("bwrap", "true", str(tmp_path), str(tmp_path), media_dir=tmp_path / "media")
    assert "--unshare-pid" in command and "--cap-drop ALL" in command
    for name in ("AGENTS.md", "SOUL.md", "config.json", "keys.env"):
        assert str(tmp_path / name) in command
