"""Agent profiles: tool allowlists, persona seeding, limits, scoped lifecycle."""

from __future__ import annotations

import pytest

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.core import Config, MoekaCore

_CONFIG_DATA = {
    "providers": {"openrouter": {"apiKey": "sk-test-key"}},
    "agents": {"defaults": {"model": "openai/gpt-4.1", "vec": {"enable": False}}},
    "profiles": {
        "research": {
            "toolsAllow": ["web_search", "web_fetch"],
            "limits": {"maxEmptyRetries": 5},
        },
    },
}


class _AlphaTool(Tool):
    @property
    def name(self):
        return "alpha"

    @property
    def description(self):
        return "..."

    @property
    def parameters(self):
        return {"type": "object"}

    async def execute(self, **_):
        return "ok"


class _BetaTool(Tool):
    @property
    def name(self):
        return "beta"

    @property
    def description(self):
        return "..."

    @property
    def parameters(self):
        return {"type": "object"}

    async def execute(self, **_):
        return "ok"


# ---------------------------------------------------------------------------
# Loader-level allow/deny
# ---------------------------------------------------------------------------

def test_loader_allowlist_excludes_new_tools():
    """A tool class the allowlist never named is not registered — the awork
    guarantee that newly added moeka tools can't leak into a scoped agent."""
    loader = ToolLoader(test_classes=[_AlphaTool, _BetaTool])
    registry = ToolRegistry()
    ctx = ToolContext(config={}, workspace="/tmp")
    loader.load(ctx, registry, allow=["alpha"])
    assert registry.has("alpha")
    assert not registry.has("beta")


def test_loader_deny_wins_over_allow():
    loader = ToolLoader(test_classes=[_AlphaTool, _BetaTool])
    registry = ToolRegistry()
    ctx = ToolContext(config={}, workspace="/tmp")
    loader.load(ctx, registry, allow=["alpha", "beta"], deny=["beta"])
    assert registry.has("alpha")
    assert not registry.has("beta")


def test_loader_default_allows_everything():
    loader = ToolLoader(test_classes=[_AlphaTool, _BetaTool])
    registry = ToolRegistry()
    ctx = ToolContext(config={}, workspace="/tmp")
    loader.load(ctx, registry)
    assert registry.has("alpha") and registry.has("beta")


# ---------------------------------------------------------------------------
# Profile resolution and application
# ---------------------------------------------------------------------------

def test_profile_scopes_core_tools(tmp_path):
    core = MoekaCore.create(
        config_dict=dict(_CONFIG_DATA), workspace=tmp_path, profile="research",
    )
    names = set(core.loop.tools.tool_names)
    assert names <= {"web_search", "web_fetch"}
    assert not core.loop.tools.has("exec")
    assert not core.loop.tools.has("read_file")
    assert not core.loop.tools.has("my")


def test_profile_limits_reach_runner(tmp_path):
    core = MoekaCore.create(
        config_dict=dict(_CONFIG_DATA), workspace=tmp_path, profile="research",
    )
    assert core.loop.runner_limits.max_empty_retries == 5
    # Untouched fields keep their defaults.
    assert core.loop.runner_limits.max_injection_cycles == 5


def test_no_profile_keeps_full_toolset(tmp_path):
    core = MoekaCore.create(config_dict=dict(_CONFIG_DATA), workspace=tmp_path)
    assert core.loop.tools.has("read_file")
    assert core.loop.tools.has("exec")
    assert core.loop.runner_limits.max_empty_retries == 2


def test_profile_does_not_mutate_caller_config(tmp_path):
    config = Config.model_validate(dict(_CONFIG_DATA))
    MoekaCore.create(config=config, workspace=tmp_path, profile="research")
    assert config.agents.defaults.tools_allow is None


def test_unknown_profile_raises(tmp_path):
    with pytest.raises(KeyError):
        MoekaCore.create(
            config_dict=dict(_CONFIG_DATA), workspace=tmp_path, profile="nope",
        )


def test_profile_persona_seeded(tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("# research persona\n", encoding="utf-8")
    data = dict(_CONFIG_DATA)
    data["profiles"] = {
        "research": {
            "toolsAllow": ["web_search"],
            "systemPromptFile": str(persona),
        },
    }
    ws = tmp_path / "ws"
    MoekaCore.create(config_dict=data, workspace=ws, profile="research")
    assert (ws / "AGENTS.md").read_text(encoding="utf-8") == "# research persona\n"


def test_profile_persona_never_overwrites(tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("new persona", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("host-owned", encoding="utf-8")
    data = dict(_CONFIG_DATA)
    data["profiles"] = {
        "research": {"systemPromptFile": str(persona)},
    }
    MoekaCore.create(config_dict=data, workspace=ws, profile="research")
    assert (ws / "AGENTS.md").read_text(encoding="utf-8") == "host-owned"


def test_inline_profile_object(tmp_path):
    """Hosts can pass an AgentProfileConfig (or dict) without naming it in config."""
    from nanobot.config.schema import AgentProfileConfig

    prof = AgentProfileConfig(
        tools_allow=["web_search"],
        system_prompt="# inline persona\n",
    )
    data = {k: v for k, v in _CONFIG_DATA.items() if k != "profiles"}
    ws = tmp_path / "ws"
    core = MoekaCore.create(config_dict=data, workspace=ws, profile=prof)
    assert set(core.loop.tools.tool_names) <= {"web_search"}
    assert core.profile_name == "inline"
    assert (ws / "AGENTS.md").read_text(encoding="utf-8") == "# inline persona\n"


def test_inline_profile_dict(tmp_path):
    data = {k: v for k, v in _CONFIG_DATA.items() if k != "profiles"}
    core = MoekaCore.create(
        config_dict=data, workspace=tmp_path,
        profile={"toolsAllow": ["web_fetch"]},
    )
    assert set(core.loop.tools.tool_names) <= {"web_fetch"}


# ---------------------------------------------------------------------------
# Scoped lifecycle
# ---------------------------------------------------------------------------

def test_scoped_removes_ephemeral_workspace():
    with MoekaCore.scoped(config_dict=dict(_CONFIG_DATA), profile="research") as core:
        ws = core.workspace
        assert ws.exists()
        assert set(core.loop.tools.tool_names) <= {"web_search", "web_fetch"}
    assert not ws.exists()


def test_scoped_cleans_up_on_exception():
    ws = None
    with pytest.raises(RuntimeError):
        with MoekaCore.scoped(config_dict=dict(_CONFIG_DATA)) as core:
            ws = core.workspace
            raise RuntimeError("boom")
    assert ws is not None and not ws.exists()


def test_scoped_keeps_host_workspace(tmp_path):
    ws = tmp_path / "persistent"
    with MoekaCore.scoped(config_dict=dict(_CONFIG_DATA), workspace=ws) as core:
        assert core.workspace == ws
    assert ws.exists()


async def test_scoped_async_removes_workspace():
    async with MoekaCore.scoped_async(config_dict=dict(_CONFIG_DATA)) as core:
        ws = core.workspace
        assert ws.exists()
    assert not ws.exists()
