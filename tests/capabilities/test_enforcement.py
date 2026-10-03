"""Spec 012 Part B: config, the loader gate, index provenance and the ranker boost."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _certkit import Key, certify, flip_byte, make_skill, revoke, write_trust

from nanobot.agent.skills import SkillsLoader
from nanobot.capabilities.enforce import CertGate
from nanobot.capabilities.index import CapabilityEntry, CapabilityIndex
from nanobot.capabilities.library import skill_entries
from nanobot.capabilities.select import lexical_ranker, select
from nanobot.capabilities.task_types import TaskTyping
from nanobot.config.schema import Config


def _caps(trust: Path, enforce: bool = True) -> dict:
    return {"enforce_certs": enforce, "trust_dirs": [str(trust)]}


@pytest.fixture
def lib(tmp_path):
    """A workspace with a certified, an uncertified and a tampered skill + one built-in."""
    key = Key()
    trust = write_trust(tmp_path / "trust", [key])
    work = tmp_path / "work"
    certified = make_skill(work, "good-skill")
    certify(certified, key)
    make_skill(work, "plain-skill")
    tampered = make_skill(work, "bad-skill")
    certify(tampered, key)
    flip_byte(tampered / "SKILL.md")
    builtin = tmp_path / "builtin"
    (builtin / "core-skill").mkdir(parents=True)
    (builtin / "core-skill" / "SKILL.md").write_text(
        "---\nname: core-skill\ndescription: A built-in skill.\n---\n\nBody.\n"
    )
    return {"key": key, "trust": trust, "work": work, "builtin": builtin}


def _names(loader: SkillsLoader) -> set[str]:
    return {entry["name"] for entry in loader.list_skills(filter_unavailable=False)}


# -- FR-005 config --------------------------------------------------------------------


def test_config_defaults_and_aliases(tmp_path, monkeypatch):
    config = Config()
    assert config.capabilities.enforce_certs is False
    assert config.capabilities.trust_dirs == []
    assert config.capabilities.certified_boost == pytest.approx(0.10)
    assert config.tools.script.enabled is False
    monkeypatch.setenv("HOME", str(tmp_path))
    loaded = Config.model_validate({
        "capabilities": {"enforceCerts": True, "trustDirs": ["~/keys"], "certifiedBoost": 0.5},
        "tools": {"script": {"enabled": True}},
    })
    assert loaded.capabilities.enforce_certs is True
    assert loaded.capabilities.trust_dirs == [str(tmp_path / "keys")]
    assert loaded.capabilities.certified_boost == 0.5
    assert loaded.tools.script.enabled is True
    for bad in (-0.1, 1.5):
        with pytest.raises(ValueError):
            Config(capabilities={"certified_boost": bad})


# -- FR-006 loader gate ---------------------------------------------------------------


def test_enforcement_off_loads_everything(lib):
    loader = SkillsLoader(lib["work"], builtin_skills_dir=lib["builtin"],
                          capabilities=_caps(lib["trust"], enforce=False))
    assert {"good-skill", "plain-skill", "bad-skill", "core-skill"} <= _names(loader)
    assert loader.skill_provenance("good-skill")[1] == "certified"
    assert loader.skill_provenance("plain-skill") == (None, "uncertified")
    assert loader.skill_provenance("bad-skill")[1] == "refused:hash-mismatch"
    assert loader.skill_provenance("core-skill") == (None, "builtin")


def test_default_loader_is_unenforced(lib):
    loader = SkillsLoader(lib["work"], builtin_skills_dir=lib["builtin"])
    assert {"good-skill", "plain-skill", "bad-skill"} <= _names(loader)


def test_enforcement_on_refuses_uncertified_and_invalid(lib, caplog_warnings):
    loader = SkillsLoader(lib["work"], builtin_skills_dir=lib["builtin"],
                          capabilities=_caps(lib["trust"]))
    names = _names(loader)
    assert "good-skill" in names and "core-skill" in names
    assert "plain-skill" not in names and "bad-skill" not in names
    assert loader.load_skill("plain-skill") is None
    assert loader.get_explicitly_invoked_skills("use $plain-skill and $good-skill") == [
        "good-skill"
    ]
    summary = loader.build_skills_summary()
    assert "good-skill" in summary and "plain-skill" not in summary
    loader.build_skills_summary()
    refusals = [m for m in caplog_warnings if "refusing workspace skill" in m]
    assert any("'plain-skill'" in m and "no-cert" in m for m in refusals)
    assert any("'bad-skill'" in m and "hash-mismatch" in m for m in refusals)
    # Logged once per process per (name, reason), however often the skills are listed.
    assert len([m for m in refusals if "'plain-skill'" in m]) == 1
    statuses = {name: p.status for name, _, p in loader.workspace_bundle_provenance()}
    assert statuses == {
        "good-skill": "certified", "plain-skill": "refused:no-cert",
        "bad-skill": "refused:hash-mismatch",
    }


def test_inline_skills_are_unaffected(lib):
    loader = SkillsLoader(
        lib["work"], builtin_skills_dir=lib["builtin"], capabilities=_caps(lib["trust"]),
        inline_skills=[{"name": "inline-one", "description": "d", "content": "c"}],
    )
    assert "inline-one" in _names(loader)
    assert loader.skill_provenance("inline-one") == (None, "builtin")


def test_trust_store_argument_pins_keys(lib):
    from nanobot.capabilities.provenance import TrustStore

    loader = SkillsLoader(lib["work"], builtin_skills_dir=lib["builtin"],
                          capabilities={"enforce_certs": True},
                          trust=TrustStore.from_keys([lib["key"].public]))
    assert "good-skill" in _names(loader)
    no_keys = SkillsLoader(lib["work"], builtin_skills_dir=lib["builtin"],
                           capabilities={"enforce_certs": True})
    assert "good-skill" not in _names(no_keys)  # the package trust dir ships empty


@pytest.mark.parametrize("change", ["flip", "rename", "io", "revoke"])
def test_sc002_any_change_removes_the_skill(lib, change):
    """SC-002: a byte flip, a rename, an io edit or a revocation drops it on the next load."""
    loader = SkillsLoader(lib["work"], builtin_skills_dir=lib["builtin"],
                          capabilities=_caps(lib["trust"]))
    assert "good-skill" in _names(loader)
    bundle = lib["work"] / "skills" / "good-skill"
    if change == "flip":
        flip_byte(bundle / "SKILL.md")
    elif change == "rename":
        bundle.rename(bundle.with_name("good-skill2"))
        text = (bundle.with_name("good-skill2") / "SKILL.md").read_text()
        (bundle.with_name("good-skill2") / "SKILL.md").write_text(
            text.replace("name: good-skill", "name: good-skill2")
        )
    elif change == "io":
        text = (bundle / "SKILL.md").read_text()
        (bundle / "SKILL.md").write_text(text.replace("text/markdown", "data/json"))
    else:
        cert_id = json.loads((bundle / "CERT.json").read_text())
        from nanobot.capabilities.provenance import cert_id_of

        revoke(lib["trust"], cert_id_of(cert_id["cert"]))
    for fresh in (loader, SkillsLoader(lib["work"], builtin_skills_dir=lib["builtin"],
                                       capabilities=_caps(lib["trust"]))):
        names = _names(fresh)
        assert "good-skill" not in names and "good-skill2" not in names
        assert "good-skill" not in fresh.build_skills_summary()


def test_kernel_env_config_enforces(lib, tmp_path):
    """The gate reads ``capabilities`` from a kernel env's config (moeka.Environment)."""
    from moeka import Environment
    from moeka.trace import MemoryTraceSink
    from moeka.variants import Variant
    from nanobot.agent.context import ContextBuilder

    env = Environment.from_config(Config(capabilities=_caps(lib["trust"])),
                                  state_dir=tmp_path / "state", work_dir=lib["work"],
                                  trace=MemoryTraceSink())
    assert "plain-skill" not in _names(SkillsLoader(lib["work"], env=env.core))
    ctx = ContextBuilder(lib["work"], env=env.core,
                         variant=Variant(builtin_skills_dir=lib["builtin"]))
    prompt = ctx.build_system_prompt(include_memory=False, include_memory_recent_history=False)
    assert "**good-skill**" in prompt and "**plain-skill**" not in prompt
    assert "**core-skill**" in prompt


def test_kernel_agent_prompt_refuses_uncertified(lib, tmp_path):
    from moeka import Environment, Kernel, ModelSpec
    from moeka.agents import AgentSpec
    from moeka.testing import FakeProvider
    from moeka.trace import MemoryTraceSink
    from moeka.variants import Variant

    env = Environment.from_config(Config(capabilities=_caps(lib["trust"])),
                                  state_dir=tmp_path / "kstate", work_dir=lib["work"],
                                  trace=MemoryTraceSink())
    kernel = Kernel(env, variant=Variant(builtin_skills_dir=lib["builtin"]))
    try:
        fake = FakeProvider(default="ok")
        kernel.llm.register_provider(
            "default", fake, ModelSpec(name="default", model="fake", provider="openai"),
        )
        agent = kernel.agent(AgentSpec(name="t012"))
        agent.run_sync("hello")
        system = str(fake.calls[-1].messages[0]["content"])
        assert "**good-skill**" in system and "**plain-skill**" not in system
    finally:
        kernel.close()


def test_cert_gate_caches_until_a_change(lib):
    gate = CertGate(enforce=True, trust_dirs=[lib["trust"]])
    bundle = lib["work"] / "skills" / "good-skill"
    first = gate.verdict(bundle)
    assert first.ok and gate.verdict(bundle) is first
    (bundle / "new.txt").write_text("x")
    assert gate.verdict(bundle).reason == "hash-mismatch"


# -- FR-007 index provenance and the boost ---------------------------------------------


def test_skill_entries_carry_provenance(lib):
    loader = SkillsLoader(lib["work"], builtin_skills_dir=lib["builtin"],
                          capabilities=_caps(lib["trust"], enforce=False))
    by_name = {entry.name: entry for entry in skill_entries(loader)}
    assert by_name["good-skill"].cert_status == "certified"
    assert len(by_name["good-skill"].cert_id or "") == 16
    assert by_name["plain-skill"].cert_status == "uncertified"
    assert by_name["bad-skill"].cert_status == "refused:hash-mismatch"
    assert by_name["core-skill"].cert_status == "builtin"
    assert by_name["core-skill"].cert_id is None


def _entry(name: str, status: str = "builtin", cert_id: str | None = None,
           description: str = "convert files") -> CapabilityEntry:
    return CapabilityEntry(kind="skill", name=name, description=description, io=None,
                           content_sha256=name * 4, cert_id=cert_id, cert_status=status)


def test_index_stores_and_updates_provenance(tmp_path):
    index = CapabilityIndex(tmp_path / "caps.db")
    index.sync([_entry("a", "certified", "0123456789abcdef"), _entry("b")])
    stored = {entry.name: entry for entry in index.entries()}
    assert stored["a"].cert_id == "0123456789abcdef"
    assert stored["a"].cert_status == "certified"
    assert stored["b"].cert_id is None and stored["b"].cert_status == "builtin"
    result = index.sync([_entry("a", "refused:revoked", "0123456789abcdef"), _entry("b")])
    assert (result.updated, result.unchanged) == (1, 1)
    assert {e.name: e.cert_status for e in index.entries()}["a"] == "refused:revoked"
    index.close()
    # Reopening keeps the columns (schema version 2).
    reopened = CapabilityIndex(tmp_path / "caps.db")
    assert {e.name: e.cert_status for e in reopened.entries()}["a"] == "refused:revoked"


def test_old_schema_index_is_rebuilt(tmp_path):
    import sqlite3

    path = tmp_path / "caps.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE capabilities (id INTEGER PRIMARY KEY, kind TEXT)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    index = CapabilityIndex(path)
    index.sync([_entry("a", "certified")])
    assert index.entries()[0].cert_status == "certified"


def _ranked(entries: list[CapabilityEntry], **kwargs) -> tuple[str, ...]:
    index = CapabilityIndex(":memory:")
    index.sync(entries)
    pad = [_entry(f"pad-{i}", description="unrelated words") for i in range(10)]
    index.sync(entries + pad)
    return select(index, TaskTyping(frozenset(), None), "convert files", k=3, **kwargs).survivors


def test_certified_capability_wins_a_tie():
    entries = [_entry("aaa"), _entry("zzz", "certified", "0123456789abcdef")]
    assert _ranked(entries)[:2] == ("zzz", "aaa")
    assert _ranked(entries, certified_boost=0.0)[:2] == ("aaa", "zzz")
    assert _ranked(entries, ranker=lexical_ranker)[:2] == ("zzz", "aaa")


def test_boost_multiplies_the_score_and_custom_rankers_are_untouched():
    entries = [_entry("aaa"), _entry("zzz", "certified", "0123456789abcdef")]
    index = CapabilityIndex(":memory:")
    index.sync(entries + [_entry(f"pad-{i}", description="nothing") for i in range(10)])
    typing = TaskTyping(frozenset(), None)
    plain = select(index, typing, "convert files", certified_boost=0.0)
    boosted = select(index, typing, "convert files", certified_boost=0.25)
    assert boosted.scores["zzz"] == pytest.approx(plain.scores["zzz"] * 1.25)
    assert boosted.scores["aaa"] == pytest.approx(plain.scores["aaa"])
    custom = select(index, typing, "x", ranker=lambda q, names: {n: 1.0 for n in names})
    assert custom.survivors[:2] == ("aaa", "pad-0")


def test_context_builder_passes_the_configured_boost(lib, tmp_path):
    from moeka import Environment
    from moeka.trace import MemoryTraceSink
    from nanobot.agent.context import ContextBuilder

    caps = {**_caps(lib["trust"], enforce=False), "selection": "filtered", "top_k": 1,
            "certified_boost": 1.0}
    env = Environment.from_config(Config(capabilities=caps), state_dir=tmp_path / "s",
                                  work_dir=lib["work"], trace=MemoryTraceSink())
    from moeka.variants import Variant

    ctx = ContextBuilder(lib["work"], env=env.core,
                         variant=Variant(builtin_skills_dir=lib["builtin"]))
    assert ctx.capabilities_config.certified_boost == 1.0
    # "skill" matches every workspace skill's description equally; the certified wins.
    prompt = ctx.build_system_prompt(query="the skill", include_memory=False,
                                     include_memory_recent_history=False)
    assert "**good-skill**" in prompt and "**bad-skill**" not in prompt
