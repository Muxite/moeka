"""Spec 005 FR-040, FR-060, FR-061 (SC-008): the file-tool floor between instances."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moeka import Paths
from nanobot.security.protected_paths import (
    PROTECTED_MARKER,
    ProtectedFloor,
    ProtectedPathError,
    check_protected,
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    home = tmp_path / "home"
    for name in (".nanobot", ".moeka-a", ".moeka-b"):
        root = home / name
        root.mkdir(parents=True)
        (root / "config.json").write_text(json.dumps({}))
        (root / "SOUL.md").write_text("soul")
        (root / "keys.env").write_text("SECRET=1")
        (root / ".env").write_text("X=1")
        (home / f"{name}-sessions").mkdir()
    custom = tmp_path / "srv" / "box"
    custom.mkdir(parents=True)
    (custom / "config.json").write_text("{}")
    (home / ".config" / "moeka").mkdir(parents=True)
    (home / ".config" / "moeka" / "instances").write_text(f"{custom}\n")
    monkeypatch.setenv("HOME", str(home))
    return {"home": home, "a": home / ".moeka-a", "b": home / ".moeka-b",
            "default": home / ".nanobot", "custom": custom, "tmp": tmp_path}


def _protected(world: dict[str, Path]) -> list[Path]:
    a, b, d, c = world["a"], world["b"], world["default"], world["custom"]
    home = world["home"]
    return [
        b / "SOUL.md", b / "config.json", b / "memory" / "MEMORY.md", b,
        home / ".moeka-b-sessions" / "x" / "sessions.db", d / "SOUL.md", d / "config.json",
        home / ".nanobot-sessions" / "id" / "sessions.db", c / "config.json", c / "notes.md",
        a / "keys.env", b / "keys.env", d / "keys.env", world["tmp"] / "anywhere" / "keys.env",
        a / "sub" / "keys.env", a / ".env", b / ".env", d / ".env",
        home / ".moeka-a-sessions" / "id" / "sessions.db",
        Path("/proc/self/environ"),
    ]


def _control(world: dict[str, Path]) -> list[Path]:
    a = world["a"]
    return [
        a / "SOUL.md", a / "USER.md", a / "notes" / "todo.md", a / "skills" / "x" / "SKILL.md",
        a / "memory" / "MEMORY.md", a / "projects" / "app" / "main.py", a / "keys.env.example",
        a / "sub" / ".env", a / "data.json", a / "cron" / "jobs.json",
    ]


def _write_only(world: dict[str, Path]) -> list[Path]:
    a = world["a"]
    return [a / ".instance.lock", a / ".instance.json", a / "moeka.pid", a / "gateway.lock",
            a / "run" / "websocket.sock", a / "run" / "x"]


def test_legacy_floor_matrix(world: dict[str, Path]) -> None:
    a = world["a"]
    protected = _protected(world)
    assert len(protected) >= 20
    for path in protected:
        for write in (False, True):
            with pytest.raises(ProtectedPathError) as info:
                check_protected(path, write=write, workspace=a)
            assert PROTECTED_MARKER in str(info.value)
    control = _control(world)
    assert len(control) >= 10
    for path in control:
        for write in (False, True):
            check_protected(path, write=write, workspace=a)
    for path in _write_only(world):
        check_protected(path, write=False, workspace=a)
        with pytest.raises(ProtectedPathError):
            check_protected(path, write=True, workspace=a)


def test_kernel_native_floor_takes_roots_from_the_host(world: dict[str, Path]) -> None:
    tmp = world["tmp"]
    paths = Paths(work_dir=tmp / "k-work", state_dir=tmp / "k-state")
    plain = ProtectedFloor.from_paths(paths)
    assert not plain.matches(world["b"] / "SOUL.md", write=False)  # the kernel never discovers
    assert plain.matches(world["b"] / "keys.env", write=False)  # keys.env: always
    floor = ProtectedFloor.from_paths(paths, other_instance_roots=[world["b"], world["custom"]])
    for path in (world["b"] / "SOUL.md", world["b"],
                 world["home"] / ".moeka-b-sessions" / "x", world["custom"] / "config.json"):
        assert floor.matches(path, write=False) and floor.matches(path, write=True)
    for name in (".instance.lock", ".instance.json"):
        assert floor.matches(paths.state_dir / name, write=True)
    assert not floor.matches(paths.work_dir / "file.txt", write=True)
    direct = ProtectedFloor(data_dir=None, workspace=paths.work_dir,
                            other_instance_roots=[world["b"]])
    assert direct.reason(world["b"] / "SOUL.md", write=False) is not None


def test_own_root_containing_the_work_dir_is_not_other(world: dict[str, Path]) -> None:
    work = world["a"] / "projects" / "app"
    check_protected(work / "main.py", write=True, workspace=work)
    with pytest.raises(ProtectedPathError):
        check_protected(world["b"] / "SOUL.md", write=False, workspace=work)


def test_symlink_into_another_instance_is_denied(world: dict[str, Path]) -> None:
    a = world["a"]
    link = a / "peek"
    link.symlink_to(world["b"])
    with pytest.raises(ProtectedPathError):
        check_protected(link / "SOUL.md", write=False, workspace=a)
