"""Web search hot reload through the host ConfigSource (Task 5).

Carry-forward from Task 4: with a hot-reloading config, a search key the user
clears in config must stop being used; the resolver's startup copy of the config
key is not a fallback. Keys from the process env (legacy) still are.
"""

from __future__ import annotations

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.web import WebSearchTool
from nanobot.config.loader import load_config, save_config
from nanobot.kernel.legacy import LegacyEnvironment
from tests._kernel_env import DictConfigSource, credential_env


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr("nanobot.config.loader._current_config_path", path)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    return path


def _save_search(tmp_path, *, provider: str, api_key: str) -> None:
    config = load_config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    config.tools.web.search.provider = provider
    config.tools.web.search.api_key = api_key
    save_config(config)


def _tool(config, env) -> WebSearchTool:
    ctx = ToolContext(
        config=config.tools,
        workspace=str(config.workspace_path),
        provider_snapshot_loader=lambda **_kw: None,  # host opted into runtime refresh
        env=env,
    )
    tool = WebSearchTool.create(ctx)
    assert isinstance(tool, WebSearchTool)
    return tool


def test_cleared_config_key_is_not_resurrected_from_resolver(tmp_path, config_path):
    _save_search(tmp_path, provider="brave", api_key="cfg-brave-key")
    config = load_config()
    env = LegacyEnvironment.from_config(config)
    tool = _tool(config, env)
    assert tool._effective_provider() == "brave"
    assert tool._credential("brave") == "cfg-brave-key"

    _save_search(tmp_path, provider="brave", api_key="")
    assert tool._effective_provider() == "duckduckgo"
    assert tool._credential("brave") == ""
    # The resolver still holds the startup mirror; only the tool ignores it.
    assert env.credentials.resolve("web/brave", "tool:web") == "cfg-brave-key"


def test_env_var_key_survives_clearing_the_config_key(tmp_path, config_path, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "env-brave-key")
    _save_search(tmp_path, provider="brave", api_key="cfg-brave-key")
    config = load_config()
    tool = _tool(config, LegacyEnvironment.from_config(config))
    assert tool._credential("brave") == "cfg-brave-key"

    _save_search(tmp_path, provider="brave", api_key="")
    assert tool._effective_provider() == "brave"
    assert tool._credential("brave") == "env-brave-key"


def test_changed_provider_is_picked_up(tmp_path, config_path):
    _save_search(tmp_path, provider="brave", api_key="cfg-brave-key")
    config = load_config()
    tool = _tool(config, LegacyEnvironment.from_config(config))
    _save_search(tmp_path, provider="tavily", api_key="cfg-tavily-key")
    assert tool._effective_provider() == "tavily"
    assert tool._credential("tavily") == "cfg-tavily-key"


def test_kernel_env_reloads_from_host_source_with_resolver_key(tmp_path, config_path):
    _save_search(tmp_path, provider="tavily", api_key="ambient-file-key")
    source = DictConfigSource({"tools": {"web": {"search": {"provider": "brave"}}}})
    env = credential_env(
        {"web/brave": "host-brave-key"},
        scopes={"web/brave": ["tool:web"]},
        root=tmp_path / "host",
        config=source,
    )
    from nanobot.config.loader import snapshot_config

    tool = _tool(snapshot_config(source), env)
    assert tool._effective_provider() == "brave"
    assert tool._credential("brave") == "host-brave-key"

    source.sections["tools"] = {"web": {"search": {"provider": "duckduckgo"}}}
    assert tool._effective_provider() == "duckduckgo"
