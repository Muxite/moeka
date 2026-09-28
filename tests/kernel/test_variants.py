"""Variants (Task 8): per-loop overrides of what the model sees, fingerprints, registries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.kernel import solvers as solvers_mod
from nanobot.kernel.baselines import BaselineRegistry
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel
from nanobot.kernel.sampling import Sampling
from nanobot.kernel.variants import Fingerprint, Variant, fingerprint
from nanobot.providers.base import GenerationSettings, LLMResponse
from nanobot.utils.prompt_templates import render_template

_BUILT: list = []


@pytest.fixture(autouse=True)
def _close_built_loops():
    # Loops built directly (not by a Kernel) own a SQLite session store: close it so
    # it is not left for the GC (an "unclosed database" ResourceWarning).
    yield
    while _BUILT:
        _BUILT.pop().sessions.close()


def _loop(workspace: Path, variant: Variant | None = None, **kwargs) -> AgentLoop:
    workspace.mkdir(parents=True, exist_ok=True)
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (0, "test-counter")
    provider.chat_stream_with_retry = AsyncMock(
        return_value=LLMResponse(content="done", tool_calls=[]),
    )
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=workspace, model="test-model",
        variant=variant, **kwargs,
    )
    _BUILT.append(loop)
    return loop


def _fp(loop: AgentLoop, *, model: str = "m", sampling: Sampling | None = None) -> Fingerprint:
    return fingerprint(loop, model=model, sampling=sampling)


def _description(loop: AgentLoop, name: str) -> str:
    for schema in loop.tools.get_definitions():
        if schema["function"]["name"] == name:
            return schema["function"]["description"]
    raise KeyError(name)


def _prompt(loop: AgentLoop) -> str:
    return loop.context.build_system_prompt(include_memory=False)


# -- Variant value -------------------------------------------------------------------


def test_variant_is_frozen_and_hashable(tmp_path) -> None:
    a = Variant(name="v", tool_descriptions={"grep": "x"}, templates_dir=str(tmp_path))
    b = Variant(name="v", tool_descriptions={"grep": "x"}, templates_dir=tmp_path)
    assert a == b and hash(a) == hash(b)
    assert isinstance(a.templates_dir, Path)
    with pytest.raises(AttributeError):
        a.name = "w"  # type: ignore[misc]
    with pytest.raises(TypeError):
        Variant(tool_descriptions={"grep": 1})  # type: ignore[dict-item]
    assert Variant().name == "base"


def test_description_precedence(tmp_path) -> None:
    (tmp_path / "grep.txt").write_text("from dir\n", encoding="utf-8")
    (tmp_path / "read_file.txt").write_text("dir read\n", encoding="utf-8")
    v = Variant(tool_descriptions_dir=tmp_path, tool_descriptions={"grep": "from map"})
    assert v.description_for("grep") == "from map"
    assert v.description_for("read_file") == "dir read"  # one trailing newline dropped
    assert v.description_for("list_dir") is None
    assert v.description_for("../grep") is None


# -- base variant is byte-identical --------------------------------------------------


def test_base_variant_is_byte_identical(tmp_path) -> None:
    none_loop = _loop(tmp_path / "a")
    base_loop = _loop(tmp_path / "b", Variant())
    assert none_loop.tools.get_definitions() == base_loop.tools.get_definitions()
    assert _fp(none_loop).digest == _fp(base_loop).digest
    assert render_template("agent/tool_contract.md") == render_template(
        "agent/tool_contract.md", roots=(),
    )


# -- tool descriptions ---------------------------------------------------------------


def test_mapping_override_changes_schema_and_digest(tmp_path) -> None:
    base = _loop(tmp_path / "base")
    loop = _loop(tmp_path / "v", Variant(name="v", tool_descriptions={"grep": "GREP V"}))
    assert _description(loop, "grep") == "GREP V"
    assert loop.tools.get("grep").description == "GREP V"
    assert _description(base, "grep") != "GREP V"
    assert _fp(loop).digest != _fp(base).digest
    assert _fp(loop).components["tools"] != _fp(base).components["tools"]
    assert _fp(loop).components["system_prompt"] == _fp(base).components["system_prompt"]


def test_dir_override_changes_schema_and_digest(tmp_path) -> None:
    descs = tmp_path / "descs"
    descs.mkdir()
    (descs / "read_file.txt").write_text("READ V\n", encoding="utf-8")
    (descs / "exec.txt").write_text("EXEC V\n", encoding="utf-8")  # a Python-text tool
    base = _loop(tmp_path / "base")
    loop = _loop(tmp_path / "v", Variant(name="v", tool_descriptions_dir=descs))
    assert _description(loop, "read_file") == "READ V"
    assert _description(loop, "exec") == "EXEC V"
    assert _description(loop, "grep") == _description(base, "grep")
    assert _fp(loop).digest != _fp(base).digest


def test_my_tool_override(tmp_path) -> None:
    loop = _loop(tmp_path / "v", Variant(tool_descriptions={"my": "MY V"}))
    assert _description(loop, "my") == "MY V"


def test_unreadable_variant_file_fails_the_load(tmp_path) -> None:
    from nanobot.agent.tools.loader import LoadError

    descs = tmp_path / "descs"
    descs.mkdir()
    (descs / "grep.txt").write_bytes(b"\xff\xfe")
    with pytest.raises(LoadError, match="UTF-8"):
        _loop(tmp_path / "v", Variant(tool_descriptions_dir=descs))


def test_override_is_per_instance_not_class_or_cache(tmp_path) -> None:
    from nanobot.agent.tools.base import builtin_description
    from nanobot.agent.tools.search import GrepTool

    builtin = builtin_description("grep")
    _loop(tmp_path / "v", Variant(tool_descriptions={"grep": "GREP V"}))
    assert builtin_description("grep") == builtin
    base = _loop(tmp_path / "base")
    assert _description(base, "grep") == builtin
    assert "_description_override" not in GrepTool.__dict__


# -- templates, skills, bootstrap ----------------------------------------------------


def test_templates_dir_shadows_and_falls_back(tmp_path) -> None:
    tpl = tmp_path / "tpl" / "agent"
    tpl.mkdir(parents=True)
    (tpl / "tool_contract.md").write_text("# Tool contract V\n", encoding="utf-8")
    base = _loop(tmp_path / "base")
    loop = _loop(tmp_path / "v", Variant(name="v", templates_dir=tmp_path / "tpl"))
    prompt = _prompt(loop)
    assert "# Tool contract V" in prompt
    assert "# Tool contract V" not in _prompt(base)
    # identity.md is not in the variant dir: it falls back to the built-in.
    assert "## Runtime" in prompt
    fp, base_fp = _fp(loop), _fp(base)
    assert fp.digest != base_fp.digest
    assert fp.components["system_prompt"] != base_fp.components["system_prompt"]
    assert fp.components["tools"] == base_fp.components["tools"]


def test_templates_dir_shadows_includes(tmp_path) -> None:
    snip = tmp_path / "tpl" / "agent" / "_snippets"
    snip.mkdir(parents=True)
    (snip / "untrusted_content.md").write_text("SNIPPET V\n", encoding="utf-8")
    loop = _loop(tmp_path / "v", Variant(templates_dir=tmp_path / "tpl"))
    assert "SNIPPET V" in _prompt(loop)


def _write_skill(root: Path, name: str, description: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody of {name}.\n",
        encoding="utf-8",
    )


def test_builtin_skills_dir_changes_listed_skills(tmp_path) -> None:
    skills = tmp_path / "skills-v"
    _write_skill(skills, "variant-skill", "Only in the variant.")
    base = _loop(tmp_path / "base")
    loop = _loop(tmp_path / "v", Variant(builtin_skills_dir=skills))
    names = {s["name"] for s in loop.context.skills.list_skills(filter_unavailable=False)}
    base_names = {s["name"] for s in base.context.skills.list_skills(filter_unavailable=False)}
    assert names == {"variant-skill"}
    assert "variant-skill" not in base_names and base_names
    assert "variant-skill" in _prompt(loop)
    assert _fp(loop).digest != _fp(base).digest


async def test_read_file_maps_relative_skill_path_onto_variant_dir(tmp_path) -> None:
    skills = tmp_path / "skills-v"
    _write_skill(skills, "variant-skill", "Only in the variant.")
    loop = _loop(tmp_path / "v", Variant(builtin_skills_dir=skills))
    result = await loop.tools.get("read_file").execute(path="skills/variant-skill/SKILL.md")
    assert "Body of variant-skill." in str(result)


def test_bootstrap_merge_explicit_wins(tmp_path) -> None:
    variant = Variant(bootstrap={"SOUL.md": "variant soul", "EXTRA.md": "variant extra"})
    loop = _loop(tmp_path / "v", variant)
    prompt = _prompt(loop)
    assert "## SOUL.md\n\nvariant soul" in prompt and "## EXTRA.md\n\nvariant extra" in prompt
    explicit = _loop(tmp_path / "e", variant, bootstrap_overrides={"SOUL.md": "explicit soul"})
    prompt = _prompt(explicit)
    assert "explicit soul" in prompt and "variant soul" not in prompt
    assert "variant extra" in prompt


# -- isolation -----------------------------------------------------------------------


async def test_concurrent_loops_with_different_variants_do_not_interfere(tmp_path) -> None:
    variants = [
        Variant(name=f"v{i}", tool_descriptions={"grep": f"GREP {i}"},
                bootstrap={"SOUL.md": f"soul {i}"})
        for i in range(4)
    ]

    def build(i: int) -> AgentLoop:
        return _loop(tmp_path / f"w{i}", variants[i])

    loops = await asyncio.gather(*(asyncio.to_thread(build, i) for i in range(4)))
    for i, loop in enumerate(loops):
        assert _description(loop, "grep") == f"GREP {i}"
        prompt = _prompt(loop)
        assert f"soul {i}" in prompt
        assert all(f"soul {j}" not in prompt for j in range(4) if j != i)
    assert len({_fp(loop).digest for loop in loops}) == 4


def test_subagent_inherits_variant(tmp_path) -> None:
    tpl = tmp_path / "tpl" / "agent"
    tpl.mkdir(parents=True)
    (tpl / "subagent_system.md").write_text("SUBAGENT V {{ workspace }}\n", encoding="utf-8")
    skills = tmp_path / "skills-v"
    _write_skill(skills, "variant-skill", "Only in the variant.")
    variant = Variant(
        tool_descriptions={"grep": "GREP V"}, templates_dir=tmp_path / "tpl",
        builtin_skills_dir=skills,
    )
    loop = _loop(tmp_path / "v", variant)
    assert loop.subagents.variant is variant
    registry = loop.subagents._build_tools()
    grep = next(s for s in registry.get_definitions() if s["function"]["name"] == "grep")
    assert grep["function"]["description"] == "GREP V"
    assert loop.subagents._build_subagent_prompt().startswith("SUBAGENT V")


# -- fingerprint ---------------------------------------------------------------------


def test_fingerprint_stable_across_builds_and_workspaces(tmp_path) -> None:
    variant = Variant(name="v", tool_descriptions={"grep": "GREP V"})
    a = _fp(_loop(tmp_path / "a", variant), sampling=Sampling(temperature=0.2))
    b = _fp(_loop(tmp_path / "elsewhere" / "b", variant), sampling=Sampling(temperature=0.2))
    assert a == b
    assert set(a.components) == {"system_prompt", "tools", "model", "sampling"}
    assert len(a.digest) == 64


async def test_fingerprint_stable_across_turns(tmp_path) -> None:
    loop = _loop(tmp_path / "w", Variant(name="v"))
    before = _fp(loop)
    await loop.process_direct("hello", session_key="api:t1")
    loop.context.memory.write_memory("remembered fact")
    await loop.process_direct("again", session_key="api:t1")
    assert _fp(loop) == before


def test_model_and_sampling_change_the_digest(tmp_path) -> None:
    loop = _loop(tmp_path / "w")
    base = _fp(loop, model="m", sampling=None)
    assert _fp(loop, model="other", sampling=None).digest != base.digest
    assert _fp(loop, model="m", sampling=Sampling()).digest != base.digest
    t1 = _fp(loop, model="m", sampling=Sampling(temperature=0.1))
    t2 = _fp(loop, model="m", sampling=Sampling(temperature=0.2))
    assert t1.digest != t2.digest
    assert t1.components["system_prompt"] == t2.components["system_prompt"]
    lb = Sampling(logit_bias={1: 1.0, "x": 2.0}, stop=["a"])
    assert _fp(loop, sampling=lb) == _fp(loop, sampling=lb)


# -- per-kernel registries -----------------------------------------------------------


def _env(tmp_path: Path) -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / "state", work_dir=tmp_path / "work", credentials={},
        providers=[ProviderSpec(name="vllm", api_base="http://127.0.0.1:9/v1")],
        models=[ModelSpec(name="m", model="qwen", provider="vllm")],
        default_model="m",
    )


def test_kernel_defaults_to_global_registries(tmp_path) -> None:
    from nanobot.kernel import baselines as baselines_mod

    with Kernel(_env(tmp_path)) as kernel:
        assert kernel.variant is None
        assert kernel.solvers is solvers_mod.default_registry()
        assert kernel.baselines is baselines_mod.default_registry()


def test_kernel_stores_variant_and_rejects_bad_types(tmp_path) -> None:
    variant = Variant(name="v")
    with Kernel(_env(tmp_path), variant=variant) as kernel:
        assert kernel.variant is variant
    with pytest.raises(TypeError):
        Kernel(_env(tmp_path), variant="v")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Kernel(_env(tmp_path), solvers=object())  # type: ignore[arg-type]


async def test_per_kernel_solver_registry_is_isolated(tmp_path) -> None:
    from moeka.testing import FakeProvider

    mine = solvers_mod.SolverRegistry()
    mine.register("sum", lambda p: solvers_mod.Solved({"a": p["x"] + 1}, "adder"))
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    spec = ModelSpec(name="m", model="qwen", provider="vllm")
    async with Kernel(_env(tmp_path / "k1"), solvers=mine, baselines=BaselineRegistry()) as k1, \
            Kernel(_env(tmp_path / "k2")) as k2:
        assert k1.solvers is mine and not solvers_mod.default_registry().has("sum")
        solved = await k1.llm.complete_json(
            "sum", schema=schema, task_type="sum", task_payload={"x": 1},
        )
        assert solved.parsed == {"a": 2} and solved.provider == "solver"
        fake = FakeProvider(['{"a": 0}'])
        k2.llm.register_provider("m", fake, spec)
        other = await k2.llm.complete_json(
            "sum", schema=schema, task_type="sum", task_payload={"x": 1},
        )
        assert other.parsed == {"a": 0} and len(fake.calls) == 1


async def test_router_accepts_a_solver_registry() -> None:
    from nanobot.kernel.router import route

    mine = solvers_mod.SolverRegistry()
    mine.register("t", lambda p: solvers_mod.Solved(42, "const"))
    result = await route("slot", "t", {"prompt": "x"}, solvers=mine, config=object())
    assert result.value == 42 and result.solved_by == "const"
