"""Group L: isolation floor (FR-060..FR-062, FR-040, SC-008, US7)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import REPO, write_config


def _setup_instances(h):
    a = h.home / ".moeka-a"
    b = h.home / ".moeka-b"
    d = h.home / ".nanobot"
    reg = h.aux / "registered-r"
    for root in (a, b, d, reg):
        write_config(root)
        (root / "SOUL.md").write_text("soul\n")
        (root / "memory").mkdir(exist_ok=True)
        (root / "memory" / "MEMORY.md").write_text("mem\n")
        (root / "keys.env").write_text("X=1\n")
        (root / ".env").write_text("Y=2\n")
        sess = root.parent / f"{root.name}-sessions" / "abc"
        sess.mkdir(parents=True, exist_ok=True)
        (sess / "sessions.db").write_bytes(b"")
    registry = h.home / ".config" / "moeka" / "instances"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(f"{reg}\n")
    (a / "sub").mkdir()
    (a / "sub" / "keys.env").write_text("Z=3\n")
    (h.aux / "elsewhere").mkdir()
    (h.aux / "elsewhere" / "keys.env").write_text("Q=4\n")
    (a / "link-to-b").symlink_to(b / "SOUL.md")
    (a / "link-to-b-sessions").symlink_to(b.parent / ".moeka-b-sessions")
    return a, b, d, reg


def _other_matrix(a: Path, b: Path, d: Path, reg: Path, h) -> list[Path]:
    return [
        b, b / "SOUL.md", b / "config.json", b / "memory" / "MEMORY.md", b / "keys.env",
        b / ".env", b / "run" / "websocket.sock", b / "new-file.txt",
        b.parent / ".moeka-b-sessions" / "abc" / "sessions.db",
        d / "SOUL.md", d / "config.json", d / ".env", d / "keys.env",
        d.parent / ".nanobot-sessions" / "abc" / "sessions.db",
        reg / "SOUL.md", reg / ".env", reg.parent / "registered-r-sessions" / "abc" / "sessions.db",
        a / "link-to-b", a / "link-to-b-sessions" / "abc" / "sessions.db",
        a / ".." / ".moeka-b" / "SOUL.md",
    ]


def _keys_matrix(a: Path, h) -> list[Path]:
    return [a / "keys.env", a / "sub" / "keys.env", h.aux / "elsewhere" / "keys.env",
            Path("/tmp/heldout-nonexistent/keys.env")]


def _own_env(a: Path) -> list[Path]:
    return [a / ".env"]


def _controls(a: Path) -> list[Path]:
    return [a / "SOUL.md", a / "USER.md", a / "notes.txt", a / "memory" / "MEMORY.md",
            a / "skills" / "x" / "SKILL.md", a / "projects" / "app.py",
            a / "keys.env.example", a / "env.txt", a / "docs" / "readme.md", a / "data.json",
            a / "moeka-b-notes.md", a / "config.json"]


def _legacy_check(h, monkeypatch, a: Path):
    from nanobot.config.loader import set_config_path
    from nanobot.security.protected_paths import ProtectedPathError, check_protected

    monkeypatch.setenv("MOEKA_WORKSPACE", str(a))
    set_config_path(a / "config.json")

    def denied(path: Path, write: bool) -> bool:
        try:
            check_protected(path, write=write, workspace=a, data_dir=None)
        except ProtectedPathError as exc:
            from nanobot.security.protected_paths import PROTECTED_MARKER

            assert PROTECTED_MARKER in str(exc)
            return True
        return False

    return denied


# -- legacy hosts (discovery from $HOME + registry) -----------------------------------------------


@pytest.mark.fr("FR-061", "SC-008")
def test_legacy_floor_denies_other_instances(h, monkeypatch):
    a, b, d, reg = _setup_instances(h)
    denied = _legacy_check(h, monkeypatch, a)
    bad = [(str(p), w) for p in _other_matrix(a, b, d, reg, h) for w in (False, True)
           if not denied(p, w)]
    assert not bad, bad


@pytest.mark.fr("FR-060", "SC-008")
def test_legacy_floor_denies_keys_env_and_root_env(h, monkeypatch):
    a, b, d, reg = _setup_instances(h)
    denied = _legacy_check(h, monkeypatch, a)
    bad = [(str(p), w) for p in _keys_matrix(a, h) + _own_env(a) for w in (False, True)
           if not denied(p, w)]
    assert not bad, bad


@pytest.mark.fr("SC-008")
def test_legacy_floor_allows_controls(h, monkeypatch):
    a, b, d, reg = _setup_instances(h)
    denied = _legacy_check(h, monkeypatch, a)
    # config.json stays write-protected (existing floor); everything else is allowed.
    bad = [(str(p), w) for p in _controls(a) for w in (False, True)
           if denied(p, w) and not (w and p.name == "config.json")]
    assert not bad, bad
    assert len(_controls(a)) >= 10


@pytest.mark.fr("FR-040")
def test_legacy_floor_write_denies_lock_pid_run_files(h, monkeypatch):
    a, b, d, reg = _setup_instances(h)
    denied = _legacy_check(h, monkeypatch, a)
    for p in (a / ".instance.lock", a / ".instance.json", a / "moeka.pid", a / "gateway.lock",
              a / "run", a / "run" / "websocket.sock", a / "run" / "anything"):
        assert denied(p, True), p


# -- kernel-native hosts (roots passed in) --------------------------------------------------------


def _native_floor(h, others, **kw):
    from moeka import Paths
    from nanobot.security.protected_paths import ProtectedFloor

    paths = Paths(work_dir=h.aux / "own-work", state_dir=h.aux / "own-state")
    return ProtectedFloor.from_paths(paths, other_instance_roots=others, **kw), paths


@pytest.mark.fr("FR-061", "FR-060", "SC-008")
def test_native_floor_with_passed_roots(h):
    a, b, d, reg = _setup_instances(h)
    floor, paths = _native_floor(h, [b, d, reg])
    work = paths.work_dir
    work.mkdir(parents=True, exist_ok=True)
    (work / "keys.env").write_text("K=1\n")
    protected = [p for p in _other_matrix(a, b, d, reg, h)
                 if not str(p).startswith(str(a))] + [
        work / "keys.env", work / "deep" / "keys.env", h.aux / "elsewhere" / "keys.env",
    ]
    bad = [(str(p), w) for p in protected for w in (False, True) if not floor.matches(p, write=w)]
    assert not bad, bad
    controls = [work / n for n in ("a.txt", "b.md", "src/app.py", "notes/x.md", "data.json",
                                   "keys.env.example", "env.txt", "README.md", "img.png",
                                   "sub/dir/file.py")]
    bad = [(str(p), w) for p in controls for w in (False, True) if floor.matches(p, write=w)]
    assert not bad, bad


@pytest.mark.fr("FR-061")
def test_native_floor_never_discovers(h):
    a, b, d, reg = _setup_instances(h)
    floor, _ = _native_floor(h, [])
    assert not floor.matches(b / "SOUL.md", write=False), \
        "kernel-native floor must not discover instances from $HOME"


@pytest.mark.fr("FR-061")
def test_root_containing_own_work_dir_is_not_other(h):
    from moeka import Paths
    from nanobot.security.protected_paths import ProtectedFloor

    root = h.aux / "outer"
    work = root / "work"
    paths = Paths(work_dir=work, state_dir=h.aux / "st")
    floor = ProtectedFloor.from_paths(paths, other_instance_roots=[root])
    assert not floor.matches(work / "file.txt", write=True)


@pytest.mark.fr("FR-061")
def test_floor_constructor_accepts_other_roots(h):
    from nanobot.security.protected_paths import ProtectedFloor

    b = h.aux / "other-b"
    floor = ProtectedFloor(data_dir=None, workspace=h.aux / "w", other_instance_roots=[b])
    assert floor.matches(b / "SOUL.md", write=False)
    assert floor.matches(h.aux / "other-b-sessions" / "x" / "sessions.db", write=True)
    assert floor.matches(b / ".env", write=False)
    assert not floor.matches(h.aux / "w" / "ok.txt", write=True)


# -- docs (FR-062) --------------------------------------------------------------------------------


@pytest.mark.fr("FR-062")
def test_floor_limits_documented():
    import nanobot.security.protected_paths as pp

    doc = (pp.__doc__ or "") + (pp.ProtectedFloor.__doc__ or "")
    assert "exec" in doc and "file tools" in doc
    text = (REPO / "docs" / "multiple-instances.md").read_text()
    low = text.lower()
    assert "file tool" in low and "exec" in low
    assert re.search(r"\buids?\b", low) and "bwrap" in low and "container" in low
