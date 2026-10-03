"""Capability selection in the system prompt (spec 011 FR-010 to FR-012, FR-015, SC-001/2)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.capabilities.index import CapabilityIndex
from nanobot.config.schema import CapabilitiesConfig, Config
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.trace import EVENTS, MemoryTraceSink
from nanobot.kernel.variants import Variant, fingerprint

DATA = Path(__file__).parent / "data" / "capability_prompt"
BENCH_LIBRARY = Path(__file__).parents[1] / "capabilities" / "bench" / "library"
PROMPT = {"include_memory": False, "include_memory_recent_history": False}
_LISTED = re.compile(r"^- \*\*([^*]+)\*\*", re.MULTILINE)


class _Sections:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.values.get(name, {}))


def _env(tmp_path: Path, sink: Any = None, config: dict[str, Any] | None = None):
    return CoreEnvironment(
        config=_Sections(config or {}),
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state"),
        trace=sink if sink is not None else MemoryTraceSink(),
    )


@pytest.fixture
def fixture_ws(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "work"
    shutil.copytree(DATA / "workspace", workspace)
    builtin = tmp_path / "builtin"
    shutil.copytree(DATA / "builtin", builtin)
    return workspace, builtin


def _builder(workspace: Path, builtin: Path, **kwargs: Any) -> ContextBuilder:
    return ContextBuilder(workspace, variant=Variant(builtin_skills_dir=builtin), **kwargs)


def _listed(prompt: str) -> list[str]:
    section = prompt.split("# Skills\n", 1)[1] if "# Skills\n" in prompt else ""
    return _LISTED.findall(section)


def _sections(prompt: str) -> str:
    parts = prompt.split("\n\n---\n\n")
    return "\n\n---\n\n".join(p for p in parts if p.startswith(("# Active Skills", "# Skills")))


def _normalise_order(text: str) -> list[list[str]]:
    """Each skills group's lines as a sorted list (directory order is filesystem order)."""
    return [sorted(block.splitlines()) for block in text.split("\n\n")]


# -- SC-001: "all" changes nothing ---------------------------------------------------------


def test_all_mode_matches_the_pre_011_golden(fixture_ws):
    workspace, builtin = fixture_ws
    prompt = _builder(workspace, builtin).build_system_prompt(
        query="summarise this as markdown", **PROMPT,
    )
    golden = (DATA / "expected_skills_sections.md").read_text(encoding="utf-8")
    assert _normalise_order(_sections(prompt)) == _normalise_order(golden)


def test_all_mode_is_byte_identical_however_it_is_configured(fixture_ws, tmp_path):
    workspace, builtin = fixture_ws
    query = "summarise https://github.com/a/b as markdown"
    default = _builder(workspace, builtin).build_system_prompt(query=query, **PROMPT)
    variants = [
        _builder(workspace, builtin, capabilities=CapabilitiesConfig()),
        _builder(workspace, builtin, capabilities={"selection": "all", "topK": 1}),
        _builder(workspace, builtin, capabilities=Config().capabilities),
        _builder(workspace, builtin,
                 env=_env(tmp_path, config={"capabilities": {"selection": "all"}})),
    ]
    for builder in variants:
        assert builder.build_system_prompt(query=query, **PROMPT) == default


def test_small_library_fallback_is_byte_identical_to_all(fixture_ws):
    workspace, builtin = fixture_ws
    query = "summarise this as markdown"
    default = _builder(workspace, builtin).build_system_prompt(query=query, **PROMPT)
    filtered = _builder(
        workspace, builtin, capabilities=CapabilitiesConfig(selection="filtered", top_k=64),
    ).build_system_prompt(query=query, attachments=["/tmp/x.pdf"], **PROMPT)
    assert filtered == default


# -- FR-010: filtered listing -----------------------------------------------------------


def test_filtered_mode_lists_only_selected_skills(fixture_ws):
    workspace, builtin = fixture_ws
    builder = _builder(
        workspace, builtin, capabilities=CapabilitiesConfig(selection="filtered", top_k=2),
    )
    prompt = builder.build_system_prompt(
        query="summarise this pdf as markdown", attachments=["/tmp/report.pdf"], **PROMPT,
    )
    listed = _listed(prompt)
    assert "pdf-notes" in listed and "repo-tour" not in listed
    assert len(listed) == 2
    # always-on skills stay under "# Active Skills" and are not listed again
    assert "# Active Skills" in prompt and "house-rules" not in listed
    # the line format is unchanged
    assert "- **pdf-notes** — Summarise a PDF into markdown notes.  `pdf-notes/SKILL.md`" in prompt


def test_explicit_skill_reference_bypasses_the_filter_and_k(fixture_ws):
    workspace, builtin = fixture_ws
    builder = _builder(
        workspace, builtin, capabilities=CapabilitiesConfig(selection="filtered", top_k=2),
    )
    prompt = builder.build_system_prompt(
        query="summarise this pdf as markdown with $repo-tour", attachments=["/tmp/a.pdf"],
        **PROMPT,
    )
    listed = _listed(prompt)
    assert "repo-tour" in listed  # requires url/git: filtered out, but explicitly invoked
    assert len(listed) == 3  # k=2 selected + the explicit one


def test_sc002_120_capabilities_pdf_turn(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    builder = ContextBuilder(
        workspace, variant=Variant(builtin_skills_dir=BENCH_LIBRARY),
        capabilities=CapabilitiesConfig(selection="filtered"),
    )
    assert len(builder.skills.list_skills(filter_unavailable=False)) >= 120
    prompt = builder.build_system_prompt(
        query="summarise this as markdown", attachments=["/tmp/report.pdf"], **PROMPT,
    )
    listed = _listed(prompt)
    assert 1 <= len(listed) <= 8
    assert "pdf-summarize" in listed
    git_only = {"git-repo-analyze", "pr-review", "github-issues", "release-notes"}
    assert not git_only & set(listed)


def test_attachment_objects_are_accepted(fixture_ws):
    workspace, builtin = fixture_ws
    builder = _builder(
        workspace, builtin, capabilities=CapabilitiesConfig(selection="filtered", top_k=1),
    )
    prompt = builder.build_system_prompt(
        query="summarise", attachments=[SimpleNamespace(name="r.bin", mime="application/pdf")],
        **PROMPT,
    )
    assert _listed(prompt) == ["pdf-notes"]


# -- FR-011: trace ---------------------------------------------------------------------


def test_filtered_build_emits_capability_selected_next_to_skill_listed(fixture_ws, tmp_path):
    workspace, builtin = fixture_ws
    sink = MemoryTraceSink()
    builder = _builder(
        workspace, builtin, env=_env(tmp_path, sink),
        capabilities=CapabilitiesConfig(selection="filtered", top_k=2),
    )
    builder.build_system_prompt(
        query="summarise this as markdown", attachments=["/tmp/a.pdf"], **PROMPT,
    )
    names = [e["event"] for e in sink.events]
    assert names.index("skill.listed") < names.index("capability.selected")
    [event] = sink.of("capability.selected")
    assert event["mode"] == "filtered" and event["fallback"] is None
    assert event["task_types"] == ["file:pdf", "text/query", "workspace"]
    assert event["requested_output"] == "text/markdown"
    assert isinstance(event["survivors"], int) and event["survivors"] >= 2
    assert "pdf-notes" in event["selected"] and len(event["selected"]) == 2
    [listed] = sink.of("skill.listed")
    assert set(event["selected"]) <= set(listed["skills"])
    assert "capability.selected" in EVENTS


def test_no_capability_event_in_all_mode_or_without_trace(fixture_ws, tmp_path):
    workspace, builtin = fixture_ws
    sink = MemoryTraceSink()
    _builder(workspace, builtin, env=_env(tmp_path, sink)).build_system_prompt(**PROMPT)
    assert sink.of("skill.listed") and not sink.of("capability.selected")
    filtered = _builder(
        workspace, builtin, env=_env(tmp_path, sink),
        capabilities=CapabilitiesConfig(selection="filtered", top_k=2),
    )
    filtered.build_system_prompt(trace=False, **PROMPT)
    assert not sink.of("capability.selected")


def test_env_config_section_turns_on_filtering(fixture_ws, tmp_path):
    workspace, builtin = fixture_ws
    sink = MemoryTraceSink()
    env = _env(tmp_path, sink, {"capabilities": {"selection": "filtered", "top_k": 1}})
    prompt = _builder(workspace, builtin, env=env).build_system_prompt(
        query="pdf", attachments=["/tmp/a.pdf"], **PROMPT,
    )
    assert _listed(prompt) == ["pdf-notes"]
    assert sink.of("capability.selected")


# -- FR-015: index wiring --------------------------------------------------------------


def test_index_lives_in_the_state_data_dir_and_holds_tools(fixture_ws, tmp_path):
    workspace, builtin = fixture_ws
    builder = _builder(
        workspace, builtin, env=_env(tmp_path),
        capabilities=CapabilitiesConfig(selection="filtered", top_k=2),
    )
    registry = ToolRegistry()
    registry.register(ExecTool())
    builder.tool_registry = registry
    builder.build_system_prompt(query="hi", **PROMPT)
    db = tmp_path / "state" / "data" / "capabilities.db"
    assert db.is_file()
    assert not list((workspace / "skills").rglob("capabilities.db"))
    with CapabilityIndex(db) as index:
        kinds = {(e.kind, e.name) for e in index.entries()}
    assert ("tool", "exec") in kinds and ("skill", "pdf-notes") in kinds


def test_sync_runs_only_when_the_lists_change(fixture_ws, monkeypatch):
    workspace, builtin = fixture_ws
    calls: list[int] = []
    original = CapabilityIndex.sync

    def counting(self, entries):
        calls.append(1)
        return original(self, entries)

    monkeypatch.setattr(CapabilityIndex, "sync", counting)
    builder = _builder(
        workspace, builtin, capabilities=CapabilitiesConfig(selection="filtered", top_k=2),
    )
    builder.build_system_prompt(query="a", **PROMPT)
    builder.build_system_prompt(query="b", **PROMPT)
    assert len(calls) == 1
    (workspace / "skills" / "new-skill").mkdir()
    (workspace / "skills" / "new-skill" / "SKILL.md").write_text(
        "---\nname: new-skill\ndescription: fresh\n---\n\nx\n", encoding="utf-8",
    )
    builder.build_system_prompt(query="c", **PROMPT)
    assert len(calls) == 2


def test_index_failure_degrades_to_all_and_logs_once(fixture_ws, tmp_path, monkeypatch):
    workspace, builtin = fixture_ws
    from loguru import logger

    def boom(self, entries):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(CapabilityIndex, "sync", boom)
    errors: list[str] = []
    handler = logger.add(lambda m: errors.append(str(m)), level="ERROR", format="{message}")
    try:
        sink = MemoryTraceSink()
        builder = _builder(
            workspace, builtin, env=_env(tmp_path, sink),
            capabilities=CapabilitiesConfig(selection="filtered", top_k=1),
        )
        first = builder.build_system_prompt(query="q", **PROMPT)
        second = builder.build_system_prompt(query="q", **PROMPT)
    finally:
        logger.remove(handler)
    expected = _builder(workspace, builtin).build_system_prompt(query="q", **PROMPT)
    assert first == second == expected
    assert len([m for m in errors if "capabilities" in m]) == 1
    assert [e["mode"] for e in sink.of("capability.selected")] == ["all", "all"]


# -- FR-012: fingerprint ---------------------------------------------------------------


def _fp(builder: ContextBuilder, workspace: Path):
    loop = SimpleNamespace(workspace=workspace, context=builder, tools=ToolRegistry())
    return fingerprint(loop, model="m", sampling=None)


def test_capabilities_config_is_part_of_the_fingerprint(fixture_ws):
    workspace, builtin = fixture_ws
    base = _fp(_builder(workspace, builtin), workspace)
    same = _fp(_builder(workspace, builtin, capabilities=CapabilitiesConfig()), workspace)
    assert same.digest == base.digest
    assert set(base.components) == {"system_prompt", "tools", "model", "sampling", "skills"}
    for config in (
        CapabilitiesConfig(selection="filtered"),
        CapabilitiesConfig(top_k=9),
        CapabilitiesConfig(ambient_types=["workspace", "host"]),
        CapabilitiesConfig(allowed_effects=["none"]),
    ):
        changed = _fp(_builder(workspace, builtin, capabilities=config), workspace)
        assert changed.digest != base.digest, config
        assert changed.components["skills"] == base.components["skills"]


def test_config_validates_capabilities_block():
    config = Config.model_validate({"capabilities": {
        "selection": "filtered", "topK": 4, "ambientTypes": ["workspace", "host"],
        "allowedEffects": ["none", "read-workspace"],
    }})
    assert config.capabilities.top_k == 4
    assert config.model_dump(by_alias=True)["capabilities"]["topK"] == 4
    defaults = Config().capabilities
    assert defaults.selection == "all" and defaults.top_k == 8
    assert defaults.ambient_types == ["workspace"]
    assert set(defaults.allowed_effects) == {
        "none", "read-workspace", "write-workspace", "network", "exec", "sudo",
    }
    for bad in ({"topK": 0}, {"topK": 65}, {"ambientTypes": ["nope"]},
                {"allowedEffects": ["teleport"]}, {"selection": "some"}):
        with pytest.raises(ValueError):
            Config.model_validate({"capabilities": bad})
