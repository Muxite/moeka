"""LegacyEnvironment: ambient Config -> CoreEnvironment adapter and the env seam."""

from unittest.mock import MagicMock

import pytest
from loguru import logger

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import Config
from nanobot.kernel import legacy
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.legacy import LegacyEnvironment
from nanobot.kernel.trace import NullTraceSink

SECRET = "sk-test-SECRET-value"
SEARCH_SECRET = "brave-SECRET-value"


def _config(tmp_path):
    return Config.model_validate({
        "providers": {"openrouter": {"apiKey": SECRET}},
        "tools": {"web": {"search": {"provider": "brave", "apiKey": SEARCH_SECRET}}},
        "agents": {"defaults": {"workspace": str(tmp_path)}},
    })


@pytest.fixture
def messages():
    out: list[str] = []
    hid = logger.add(lambda m: out.append(str(m)), level="DEBUG")
    yield out
    logger.remove(hid)


@pytest.fixture(autouse=True)
def _reset_flag():
    legacy._OVERLAP_WARNED = False
    yield
    legacy._OVERLAP_WARNED = False


def test_keys_land_under_refs(tmp_path):
    env = LegacyEnvironment.from_config(_config(tmp_path))
    assert isinstance(env, CoreEnvironment)
    assert env.credentials.resolve("providers/openrouter/api_key", "provider:openrouter") == SECRET
    assert env.credentials.resolve("web/brave", "tool:web") == SEARCH_SECRET
    assert env.credentials.resolve("providers/openai/api_key", "provider:openai") is None
    assert env.strict is False


def test_no_key_value_in_repr_or_logs(tmp_path, messages):
    env = LegacyEnvironment.from_config(_config(tmp_path))
    blob = repr(env) + repr(env.credentials) + "".join(messages)
    assert SECRET not in blob
    assert SEARCH_SECRET not in blob
    assert "providers/openrouter/api_key" in repr(env.credentials)


def test_config_source_sections(tmp_path):
    env = LegacyEnvironment.from_config(_config(tmp_path))
    assert "openrouter" in env.config.section("providers")
    assert "web" in env.config.section("tools")
    assert env.config.section("nope") == {}


def test_exec_base_env_is_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNEL_T2_PROBE", "1")
    env = LegacyEnvironment.from_config(_config(tmp_path))
    monkeypatch.setenv("KERNEL_T2_PROBE", "2")
    assert env.exec_base_env["KERNEL_T2_PROBE"] == "1"


def test_flat_layout_overlap_warns_once(tmp_path, monkeypatch, messages):
    env1 = LegacyEnvironment.from_config(_config(tmp_path))
    env2 = LegacyEnvironment.from_config(_config(tmp_path))
    assert env1.paths.overlaps and env2.paths.overlaps
    assert sum("legacy flat layout" in m for m in messages) == 1


def test_overlap_trace_event(tmp_path, monkeypatch):
    events: list[dict] = []

    class Sink:
        def emit(self, event):
            events.append(event)

    monkeypatch.setattr(legacy, "LoguruTraceSink", Sink)
    LegacyEnvironment.from_config(_config(tmp_path))
    assert any(e.get("event") == "paths.overlap_allowed" for e in events)


def _provider():
    p = MagicMock()
    p.get_default_model.return_value = "test-model"
    return p


def test_from_config_builds_default_env(tmp_path):
    loop = AgentLoop.from_config(_config(tmp_path), tool_registry=ToolRegistry(),
                                 provider=_provider())
    assert isinstance(loop.env, CoreEnvironment)
    assert loop.env.credentials.resolve(
        "providers/openrouter/api_key", "provider:openrouter"
    ) == SECRET
    loop.sessions.close()


def test_explicit_env_stored_and_reaches_tool_context(tmp_path, monkeypatch):
    from nanobot.agent.tools import loader as loader_mod

    seen = []
    orig = loader_mod.ToolLoader.load

    def spy(self, ctx, *a, **kw):
        seen.append(ctx)
        return orig(self, ctx, *a, **kw)

    monkeypatch.setattr(loader_mod.ToolLoader, "load", spy)
    env = CoreEnvironment(
        config=LegacyEnvironment.from_config(_config(tmp_path)).config,
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=tmp_path / "w", state_dir=tmp_path / "s"),
        trace=NullTraceSink(),
    )
    loop = AgentLoop.from_config(_config(tmp_path), tool_registry=ToolRegistry(),
                                 provider=_provider(), env=env)
    assert loop.env is env
    assert seen and all(c.env is env for c in seen)
    sub_ctx_env = [c.env for c in seen]
    assert sub_ctx_env[0] is env
    loop.sessions.close()


def test_process_env_secret_not_in_repr(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNEL_T2_FAKE_SECRET", "s3cr3t-env-value")
    env = LegacyEnvironment.from_config(_config(tmp_path))
    assert env.exec_base_env["KERNEL_T2_FAKE_SECRET"] == "s3cr3t-env-value"
    assert "s3cr3t-env-value" not in repr(env)
    assert "s3cr3t-env-value" not in str(env)


def test_direct_env_repr_hides_exec_base_env(tmp_path):
    env = CoreEnvironment(
        config=LegacyEnvironment.from_config(_config(tmp_path)).config,
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=tmp_path / "w", state_dir=tmp_path / "s"),
        trace=NullTraceSink(),
        exec_base_env={"TOKEN": "hidden-value"},
    )
    assert "hidden-value" not in repr(env)


def test_empty_search_provider_makes_no_web_ref(tmp_path):
    cfg = _config(tmp_path)
    cfg.tools.web.search.provider = ""
    env = LegacyEnvironment.from_config(cfg)
    assert env.credentials.resolve("web/", "tool:web") is None
