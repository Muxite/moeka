"""Task 3 (I2): ``env.paths`` drives sessions, the file-tool floor, media and data dirs.

(a) a strict separated kernel keeps its private state under ``state_dir`` and its
    media under ``work_dir``; the agent's file tools may not touch ``state_dir``;
(b) the legacy adapter keeps today's on-disk locations exactly and building it for
    an in-memory config touches nothing under ``~/.nanobot``;
(c) ``Paths`` override semantics.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config import loader as config_loader
from nanobot.config import paths as config_paths
from nanobot.config.schema import Config, ToolsConfig
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.legacy import LegacyEnvironment
from nanobot.kernel.trace import NullTraceSink
from nanobot.session.sqlite_store import default_sessions_root

DENIAL = "protected internal path (not configurable)"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdacd\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _provider():
    p = MagicMock()
    p.get_default_model.return_value = "test-model"
    return p


def _config(workspace: Path) -> Config:
    return Config.model_validate({"agents": {"defaults": {"workspace": str(workspace)}}})


def _strict_env(tmp_path: Path) -> CoreEnvironment:
    work = tmp_path / "work"
    state = tmp_path / "state"
    work.mkdir()
    state.mkdir()
    return CoreEnvironment(
        config=LegacyEnvironment.from_config(_config(work)).config,
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=work, state_dir=state),
        trace=NullTraceSink(),
        strict=True,
    )


# ---------------------------------------------------------------------------
# (a) strict separated dirs
# ---------------------------------------------------------------------------


def test_strict_sessions_db_lands_under_state_dir(tmp_path):
    env = _strict_env(tmp_path)
    loop = AgentLoop.from_config(
        _config(env.paths.work_dir), tool_registry=ToolRegistry(), provider=_provider(), env=env,
    )
    session = loop.sessions.get_or_create("cli:direct")
    loop.sessions.save(session)
    assert env.paths.sessions_root == env.paths.state_dir / "sessions"
    assert list(env.paths.sessions_root.rglob("sessions.db"))
    # The legacy sibling heuristic is not used when an env is given.
    assert not default_sessions_root(env.paths.work_dir).exists()
    assert not any(env.paths.work_dir.rglob("sessions.db"))


def test_strict_auth_files_land_under_state_dir(tmp_path):
    from nanobot.agent.tools.mcp_oauth import MCPOAuthStorage, delete_mcp_oauth_credentials
    from nanobot.providers.xai_oauth import XAIToken, _write_token, get_xai_oauth_storage_path

    env = _strict_env(tmp_path)
    data_dir = env.paths.data_dir
    assert data_dir.is_relative_to(env.paths.state_dir)

    _write_token(XAIToken(access="a", refresh="r", expires=1), data_dir=data_dir)
    xai_path = get_xai_oauth_storage_path(data_dir)
    assert xai_path.exists()
    assert xai_path.is_relative_to(env.paths.state_dir)

    delete_mcp_oauth_credentials("srv", data_dir=data_dir)
    mcp_store = data_dir / "auth" / "mcp.json"
    assert mcp_store.exists()
    storage = MCPOAuthStorage("srv", "https://mcp.example.com/mcp", data_dir=data_dir)
    assert storage.has_credentials() is False
    assert not any(env.paths.work_dir.rglob("*.json"))


def test_strict_xai_provider_gets_data_dir_from_factory(tmp_path):
    from nanobot.providers.factory import make_provider

    env = _strict_env(tmp_path)
    cfg = Config.model_validate({
        "agents": {"defaults": {
            "workspace": str(env.paths.work_dir), "model": "xai-grok/grok-4.5",
            "provider": "xai_grok",
        }},
    })
    provider = make_provider(cfg, data_dir=env.paths.data_dir)
    assert provider._data_dir == env.paths.data_dir


def test_strict_llm_usage_db_lands_under_state_dir(tmp_path):
    from nanobot.llm_usage import get_llm_usage_store, llm_usage_store_path

    env = _strict_env(tmp_path)
    path = llm_usage_store_path(env.paths.data_dir)
    assert path.is_relative_to(env.paths.state_dir)
    store = get_llm_usage_store(data_dir=env.paths.data_dir)
    store.usage_payload(days=1)
    assert path.exists()


def test_strict_media_lands_under_work_dir(tmp_path):
    from nanobot.utils.artifacts import store_generated_image_artifact

    env = _strict_env(tmp_path)
    data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()
    artifact = store_generated_image_artifact(
        data_url, prompt="p", model="m", media_dir=env.paths.media_dir,
    )
    stored = Path(artifact["path"])
    assert stored.is_relative_to(env.paths.work_dir / "media")
    assert not any(env.paths.state_dir.rglob("*.png"))


@pytest.mark.parametrize("restrict", [False, True])
async def test_strict_file_tools_deny_state_dir(tmp_path, restrict):
    env = _strict_env(tmp_path)
    secret = env.paths.state_dir / "notes.txt"
    secret.write_text("kernel private", encoding="utf-8")
    ctx = ToolContext(
        config=ToolsConfig(restrict_to_workspace=restrict),
        workspace=str(env.paths.work_dir),
        env=env,
    )
    read = ReadFileTool.create(ctx)
    write = WriteFileTool.create(ctx)

    # Unrestricted tools hit the floor; restricted ones are stopped even earlier
    # by the workspace boundary. Either way state_dir is unreachable.
    denial = DENIAL if not restrict else "Error"
    out = await read.execute(path=str(secret))
    assert denial in out
    assert "kernel private" not in out
    out = await write.execute(path=str(env.paths.state_dir / "new.txt"), content="x")
    assert denial in out
    assert not (env.paths.state_dir / "new.txt").exists()
    # The floor itself denies all of state_dir in the separated layout.
    assert read._protected_floor().matches(secret, write=False)
    assert read._protected_floor().matches(env.paths.state_dir / "x" / "y", write=True)

    # The work dir stays usable.
    ok = env.paths.work_dir / "ok.txt"
    out = await write.execute(path=str(ok), content="hello")
    assert DENIAL not in out
    assert ok.read_text(encoding="utf-8") == "hello"


def test_strict_image_reference_under_state_dir_denied(tmp_path):
    from nanobot.agent.tools.image_generation import ImageGenerationTool
    from nanobot.providers.image_generation import ImageGenerationError

    env = _strict_env(tmp_path)
    ref = env.paths.state_dir / "ref.png"
    ref.write_bytes(PNG_BYTES)
    tool = ImageGenerationTool.create(
        ToolContext(config=ToolsConfig(), workspace=str(env.paths.work_dir), env=env)
    )
    assert tool._protected_floor().matches(ref, write=False)
    with pytest.raises(ImageGenerationError):
        tool._resolve_reference_image(str(ref))
    # Media under work_dir is the allowed extra root.
    assert tool._media_dir() == env.paths.work_dir / "media"


# ---------------------------------------------------------------------------
# (b) legacy: exactly today's locations; in-memory never touches ~/.nanobot
# ---------------------------------------------------------------------------


def test_legacy_on_disk_config_keeps_todays_locations(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    home.mkdir()
    cfg_path = home / "config.json"
    cfg_path.write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(workspace)}}}), encoding="utf-8",
    )
    monkeypatch.setattr(config_loader, "_current_config_path", None)
    config_loader.set_config_path(cfg_path)
    config = config_loader.load_config(cfg_path)

    env = LegacyEnvironment.from_config(config)

    assert env.paths.data_dir == config_paths.get_data_dir().resolve()
    assert env.paths.media_dir == config_paths.get_media_dir().resolve()
    assert env.paths.logs_dir == config_paths.get_logs_dir().resolve()
    assert env.paths.sessions_root == default_sessions_root(workspace.resolve())
    assert env.paths.data_dir == home.resolve()


def test_legacy_loop_sessions_root_unchanged(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    loop = AgentLoop.from_config(
        _config(workspace), tool_registry=ToolRegistry(), provider=_provider(),
    )
    loop.sessions.save(loop.sessions.get_or_create("cli:direct"))
    assert loop.env.paths.sessions_root == default_sessions_root(workspace.resolve())
    assert list(default_sessions_root(workspace.resolve()).rglob("sessions.db"))


def test_legacy_loop_sessions_root_unchanged_for_symlinked_workspace(tmp_path):
    """Pre-kernel code used ``default_sessions_root(<unresolved workspace>)``."""
    real = tmp_path / "real" / "ws"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    os.symlink(real, link)
    old_root = default_sessions_root(link)  # what the pre-kernel loop used
    assert old_root.resolve() != default_sessions_root(real.resolve())  # the two differ

    loop = AgentLoop.from_config(
        _config(link), tool_registry=ToolRegistry(), provider=_provider(),
    )
    loop.sessions.save(loop.sessions.get_or_create("cli:direct"))
    assert loop.env.paths.sessions_root == old_root.resolve()
    assert list(old_root.rglob("sessions.db"))
    assert not default_sessions_root(real).exists()
    # The default legacy env keeps the loop workspace as configured.
    assert loop.workspace == _config(link).workspace_path


def test_facade_keeps_configured_workspace_for_symlink(tmp_path):
    from unittest.mock import patch

    from nanobot.nanobot import Nanobot

    real = tmp_path / "real" / "ws"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    os.symlink(real, link)
    config = _config(link)
    with patch("nanobot.config.loader.load_config", return_value=config), \
         patch("nanobot.providers.factory.make_provider", return_value=_provider()):
        bot = Nanobot.from_config()
    assert bot._loop.workspace == config.workspace_path
    assert bot._loop.env.paths.sessions_root == default_sessions_root(link).resolve()


def test_legacy_in_memory_env_touches_nothing_under_home(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("LegacyEnvironment touched the state home for an in-memory config")

    monkeypatch.setattr(config_loader, "get_state_home", _boom)
    monkeypatch.setattr(config_paths, "get_state_home", _boom)
    monkeypatch.setattr(config_paths, "get_data_dir", _boom)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: _boom()))

    real_mkdir = Path.mkdir

    def guarded_mkdir(self, *a, **k):
        if ".nanobot" in Path(self).parts:
            raise AssertionError(f"mkdir under ~/.nanobot: {self}")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)

    workspace = tmp_path / "ws"
    env = LegacyEnvironment.from_config(_config(workspace))
    # Even reading the dirs of an in-memory config stays under its own state dir.
    for p in (env.paths.data_dir, env.paths.logs_dir, env.paths.media_dir,
              env.paths.sessions_root):
        assert ".nanobot" not in p.parts
    assert env.paths.data_dir.is_relative_to(workspace.resolve())
    assert env.paths.sessions_root == default_sessions_root(workspace.resolve())


def test_legacy_on_disk_env_is_lazy(tmp_path, monkeypatch):
    calls: list[int] = []
    cfg_path = tmp_path / "cfg" / "config.json"
    cfg_path.parent.mkdir()
    cfg_path.write_text("{}", encoding="utf-8")
    config = config_loader.load_config(cfg_path)
    config.agents.defaults.workspace = str(tmp_path / "ws")

    real = config_loader.get_config_path

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(config_loader, "get_config_path", counting)
    env = LegacyEnvironment.from_config(config)
    assert calls == []
    _ = env.paths.data_dir
    _ = env.paths.data_dir
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# (c) Paths override semantics
# ---------------------------------------------------------------------------


def test_paths_derived_defaults(tmp_path):
    p = Paths(work_dir=tmp_path / "w", state_dir=tmp_path / "s")
    assert p.sessions_root == (tmp_path / "s" / "sessions").resolve()
    assert p.data_dir == (tmp_path / "s" / "data").resolve()
    assert p.logs_dir == (tmp_path / "s" / "logs").resolve()
    assert p.media_dir == (tmp_path / "w" / "media").resolve()


def test_paths_explicit_overrides_are_resolved(tmp_path):
    p = Paths(
        work_dir=tmp_path / "w",
        state_dir=tmp_path / "s",
        sessions_root_override=tmp_path / "x" / ".." / "sess",
        data_dir_override=tmp_path / "d",
        logs_dir_override=tmp_path / "l",
        media_dir_override=tmp_path / "m",
    )
    assert p.sessions_root == (tmp_path / "sess").resolve()
    assert p.data_dir == (tmp_path / "d").resolve()
    assert p.logs_dir == (tmp_path / "l").resolve()
    assert p.media_dir == (tmp_path / "m").resolve()


def test_paths_callable_override_is_lazy_and_cached(tmp_path):
    calls: list[int] = []

    def data() -> Path:
        calls.append(1)
        return tmp_path / "lazy"

    p = Paths(work_dir=tmp_path / "w", state_dir=tmp_path / "s", data_dir_override=data)
    assert calls == []
    assert p.data_dir == (tmp_path / "lazy").resolve()
    assert p.data_dir == (tmp_path / "lazy").resolve()
    assert calls == [1]


def test_paths_stays_frozen(tmp_path):
    p = Paths(work_dir=tmp_path / "w", state_dir=tmp_path / "s")
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.data_dir_override = tmp_path  # type: ignore[misc]
