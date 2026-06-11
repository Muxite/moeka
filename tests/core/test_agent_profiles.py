"""Agent profiles: tool allowlists, in-memory persona/skills, limits, scoped lifecycle."""

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


def test_profile_persona_flows_in_memory(tmp_path):
    """The persona reaches the system prompt without touching the workspace."""
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
    core = MoekaCore.create(config_dict=data, workspace=ws, profile="research")
    assert "# research persona" in core.loop.context.build_system_prompt()
    assert not (ws / "AGENTS.md").exists()


def test_profile_persona_shadows_stale_workspace_file(tmp_path):
    """A pre-existing AGENTS.md no longer wins over the profile persona — the
    in-memory override shadows it in the prompt but the file stays untouched."""
    persona = tmp_path / "persona.md"
    persona.write_text("new persona", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("host-owned", encoding="utf-8")
    data = dict(_CONFIG_DATA)
    data["profiles"] = {
        "research": {"systemPromptFile": str(persona)},
    }
    core = MoekaCore.create(config_dict=data, workspace=ws, profile="research")
    prompt = core.loop.context.build_system_prompt()
    assert "new persona" in prompt
    assert "host-owned" not in prompt
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
    assert "# inline persona" in core.loop.context.build_system_prompt()
    assert not (ws / "AGENTS.md").exists()


def test_inline_profile_dict(tmp_path):
    data = {k: v for k, v in _CONFIG_DATA.items() if k != "profiles"}
    core = MoekaCore.create(
        config_dict=data, workspace=tmp_path,
        profile={"toolsAllow": ["web_fetch"]},
    )
    assert set(core.loop.tools.tool_names) <= {"web_fetch"}


# ---------------------------------------------------------------------------
# In-memory bootstrap and inline skills
# ---------------------------------------------------------------------------

def test_create_bootstrap_sections(tmp_path):
    core = MoekaCore.create(
        config_dict=dict(_CONFIG_DATA), workspace=tmp_path,
        bootstrap={"USER.md": "user-context-marker", "HOST.md": "host-section-marker"},
    )
    prompt = core.loop.context.build_system_prompt()
    assert "user-context-marker" in prompt
    assert "host-section-marker" in prompt
    assert not (tmp_path / "USER.md").exists()


def test_explicit_bootstrap_wins_over_profile_persona(tmp_path):
    core = MoekaCore.create(
        config_dict=dict(_CONFIG_DATA), workspace=tmp_path,
        profile={"systemPrompt": "profile persona"},
        bootstrap={"AGENTS.md": "explicit bootstrap persona"},
    )
    prompt = core.loop.context.build_system_prompt()
    assert "explicit bootstrap persona" in prompt
    assert "profile persona" not in prompt


def test_set_bootstrap_on_live_core(tmp_path):
    core = MoekaCore.create(config_dict=dict(_CONFIG_DATA), workspace=tmp_path)
    core.set_bootstrap("AGENTS.md", "late-bound persona")
    assert "late-bound persona" in core.loop.context.build_system_prompt()


def test_create_with_inline_skills(tmp_path):
    core = MoekaCore.create(
        config_dict=dict(_CONFIG_DATA), workspace=tmp_path,
        skills=[{"name": "host_howto", "content": "# How-to", "description": "host how-to"}],
    )
    skills = core.loop.context.skills
    assert skills.load_skill("host_howto") == "# How-to"
    assert "host_howto" in core.loop.context.build_system_prompt()


def test_profile_skills_inline(tmp_path):
    core = MoekaCore.create(
        config_dict=dict(_CONFIG_DATA), workspace=tmp_path,
        profile={
            "skillsInline": [
                {"name": "prof_skill", "content": "# P", "description": "profile skill"},
            ],
        },
    )
    assert core.loop.context.skills.load_skill("prof_skill") == "# P"


def test_add_skill_on_live_core(tmp_path):
    core = MoekaCore.create(config_dict=dict(_CONFIG_DATA), workspace=tmp_path)
    core.add_skill("late_skill", "# Late", description="added at runtime")
    assert core.loop.context.skills.load_skill("late_skill") == "# Late"


def test_skills_include_empty_drops_builtins_keeps_inline(tmp_path):
    """skills_include=[] is the opt-out from moeka's builtin skill catalog;
    inline skills are exempt (the host registered them explicitly)."""
    core = MoekaCore.create(
        config_dict=dict(_CONFIG_DATA), workspace=tmp_path,
        profile={"skillsInclude": []},
        skills=[{"name": "only_skill", "content": "# Only", "description": "the one"}],
    )
    entries = core.loop.context.skills.list_skills(filter_unavailable=False)
    assert [e["name"] for e in entries] == ["only_skill"]


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
