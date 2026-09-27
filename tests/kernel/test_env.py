"""Host-contract types: Paths, CredentialResolver, ConfigSource, TraceSink, CoreEnvironment."""

import dataclasses
import os

import pytest

from nanobot.kernel.env import (
    CoreEnvironment,
    Paths,
    PathsOverlapError,
    StaticCredentialResolver,
)
from nanobot.kernel.trace import LoguruTraceSink, NullTraceSink, safe_emit


def _dirs(tmp_path):
    w = tmp_path / "work"
    s = tmp_path / "state"
    w.mkdir()
    s.mkdir()
    return w, s


def test_disjoint_dirs_ok(tmp_path):
    w, s = _dirs(tmp_path)
    p = Paths(work_dir=w, state_dir=s)
    assert p.overlaps is False


def test_equal_dirs_raise(tmp_path):
    w, _ = _dirs(tmp_path)
    with pytest.raises(PathsOverlapError):
        Paths(work_dir=w, state_dir=w)


def test_state_inside_work_raises(tmp_path):
    w, _ = _dirs(tmp_path)
    with pytest.raises(PathsOverlapError):
        Paths(work_dir=w, state_dir=w / "state")


def test_work_inside_state_raises(tmp_path):
    _, s = _dirs(tmp_path)
    with pytest.raises(PathsOverlapError):
        Paths(work_dir=s / "work", state_dir=s)


def test_symlink_alias_raises(tmp_path):
    w, _ = _dirs(tmp_path)
    link = tmp_path / "alias"
    link.symlink_to(w, target_is_directory=True)
    with pytest.raises(PathsOverlapError):
        Paths(work_dir=w, state_dir=link)


def test_dotdot_alias_raises(tmp_path):
    w, _ = _dirs(tmp_path)
    with pytest.raises(PathsOverlapError):
        Paths(work_dir=w, state_dir=w / "x" / "..")


@pytest.mark.skipif(
    os.path.normcase("A") == "A",
    reason="case-sensitive platform: case aliases are distinct directories",
)
def test_case_alias_raises(tmp_path):  # pragma: no cover - windows/macOS only
    w, _ = _dirs(tmp_path)
    with pytest.raises(PathsOverlapError):
        Paths(work_dir=w, state_dir=tmp_path / "WORK")


def test_overlap_ok_allows_and_flags(tmp_path):
    w, _ = _dirs(tmp_path)
    p = Paths(work_dir=w, state_dir=w, overlap_ok=True)
    assert p.overlaps is True


def test_derived_dirs(tmp_path):
    w, s = _dirs(tmp_path)
    p = Paths(work_dir=w, state_dir=s)
    assert p.sessions_root == p.state_dir / "sessions"
    assert p.data_dir == p.state_dir / "data"
    assert p.logs_dir == p.state_dir / "logs"
    assert p.media_dir == p.work_dir / "media"
    assert not p.sessions_root.exists()


def test_static_resolver_basic():
    r = StaticCredentialResolver({"a": "1"})
    assert r.resolve("a", "any") == "1"
    assert r.resolve("nope", "any") is None


def test_static_resolver_scopes():
    r = StaticCredentialResolver({"a": "1", "b": "2"}, scopes={"a": ["llm"]})
    assert r.resolve("a", "llm") == "1"
    assert r.resolve("a", "exec") is None
    assert r.resolve("b", "exec") == "2"


def test_safe_emit_swallows_and_logs(capsys):
    from loguru import logger

    msgs = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="WARNING")

    class Boom:
        def emit(self, event):
            raise RuntimeError("kaboom")

    try:
        safe_emit(Boom(), {"k": "v"})
    finally:
        logger.remove(hid)
    assert msgs and "kaboom" in "".join(msgs)


def test_null_and_loguru_sinks_do_not_raise():
    NullTraceSink().emit({"a": 1})
    LoguruTraceSink().emit({"a": 1})
    safe_emit(NullTraceSink(), {"a": 1})


def test_core_environment_defaults(tmp_path):
    w, s = _dirs(tmp_path)

    class Cfg:
        def section(self, name):
            return {}

    env = CoreEnvironment(
        config=Cfg(),
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=w, state_dir=s),
        trace=NullTraceSink(),
    )
    assert env.strict is False
    assert dict(env.exec_base_env) == {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        env.strict = True  # type: ignore[misc]


def test_kernel_reexports():
    import nanobot.kernel as k

    for name in ("Paths", "CoreEnvironment", "TraceSink", "StaticCredentialResolver"):
        assert name in k.__all__
        assert hasattr(k, name)


def test_strict_env_rejects_overlapping_paths_even_with_overlap_ok(tmp_path):
    """I2: overlap_ok=True is legacy-only; strict=True refuses it at construction."""
    from nanobot.kernel.env import (
        CoreEnvironment,
        Paths,
        PathsOverlapError,
        StaticCredentialResolver,
    )
    from nanobot.kernel.trace import NullTraceSink

    flat = Paths(work_dir=tmp_path, state_dir=tmp_path, overlap_ok=True)
    nested = Paths(work_dir=tmp_path, state_dir=tmp_path / "state", overlap_ok=True)
    for paths in (flat, nested):
        with pytest.raises(PathsOverlapError):
            CoreEnvironment(
                config=None,  # type: ignore[arg-type]
                credentials=StaticCredentialResolver({}),
                paths=paths,
                trace=NullTraceSink(),
                strict=True,
            )
        # Non-strict (legacy) envs still accept the flat layout.
        CoreEnvironment(
            config=None,  # type: ignore[arg-type]
            credentials=StaticCredentialResolver({}),
            paths=paths,
            trace=NullTraceSink(),
        )
