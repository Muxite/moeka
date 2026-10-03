"""Part B: the loader gate (FR-006): workspace skills (listed / loadable / ``$``-invocable) and
script tools (registered) with enforcement on and off; built-in, inline and plugin skills and
built-in tools unaffected; one log record per (name, reason)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from _h012 import (
    Signer,
    agent_of,
    caps_for,
    certify,
    listed,
    make_cert,
    revoke,
    skills_loader,
    summary_names,
    system_prompt_of,
    tool_names,
    trust_dir,
    user_message_of,
    write_cert,
    write_script_tool,
    write_skill,
)

WS_SKILLS = ("h012-cert", "h012-nocert", "h012-badsig", "h012-revoked")
SCRIPTS = ("h012-tcert", "h012-tnocert", "h012-tbad")


def gate_fixture(root: Path) -> dict:
    """A workspace with certified / uncertified / refused skills and script tools, a fixture
    built-in skills dir, and a trust dir holding the fake key ``alpha``."""
    alpha, beta = Signer("alpha"), Signer("beta")
    work = root / "work"
    tdir = trust_dir(root / "trust", alpha)
    certify(write_skill(work, "h012-cert"), alpha)
    write_skill(work, "h012-nocert")
    b = write_skill(work, "h012-badsig")
    cert = make_cert(b, alpha)
    write_cert(b, cert, alpha, sig=beta.sig_b64(cert))
    revoke(tdir, certify(write_skill(work, "h012-revoked"), alpha))
    certify(write_script_tool(work, "h012-tcert"), alpha)
    write_script_tool(work, "h012-tnocert")
    certify(write_script_tool(work, "h012-tbad"), beta)
    builtin = root / "bskills"
    write_skill(builtin, "h012-builtin", io=None)  # -> bskills/skills/h012-builtin/
    return {"work": work, "trust": tdir, "builtin": builtin / "skills", "alpha": alpha}


def _caps(fx, enforce):
    return caps_for(enforce, fx["trust"])


# -- kernel: prompt summary ---------------------------------------------------------------------


@pytest.mark.fr("FR-006")
def test_enforced_prompt_lists_only_certified_workspace_skills(kmaker, tmp_path):
    fx = gate_fixture(tmp_path)
    kernel, fake = kmaker(work=fx["work"], caps=_caps(fx, True), builtin=fx["builtin"])
    agent_of(kernel).run_sync("hello")
    names = summary_names(system_prompt_of(fake))
    assert "h012-cert" in names
    for refused in ("h012-nocert", "h012-badsig", "h012-revoked"):
        assert refused not in names
    assert "h012-builtin" in names


@pytest.mark.fr("FR-006")
def test_unenforced_prompt_lists_every_workspace_skill(kmaker, tmp_path):
    fx = gate_fixture(tmp_path)
    kernel, fake = kmaker(work=fx["work"], caps=_caps(fx, False), builtin=fx["builtin"])
    agent_of(kernel).run_sync("hello")
    names = summary_names(system_prompt_of(fake))
    for name in (*WS_SKILLS, "h012-builtin"):
        assert name in names


@pytest.mark.fr("FR-006")
def test_default_config_refuses_nothing(kmaker, tmp_path):
    fx = gate_fixture(tmp_path)
    kernel, fake = kmaker(work=fx["work"], builtin=fx["builtin"])
    agent_of(kernel).run_sync("hello")
    names = summary_names(system_prompt_of(fake))
    for name in WS_SKILLS:
        assert name in names


@pytest.mark.fr("FR-006")
def test_builtin_skill_with_garbage_cert_unaffected(kmaker, tmp_path):
    fx = gate_fixture(tmp_path)
    (fx["builtin"] / "h012-builtin").joinpath("CERT.json").write_text("{bad", encoding="utf-8")
    kernel, fake = kmaker(work=fx["work"], caps=_caps(fx, True), builtin=fx["builtin"])
    agent_of(kernel).run_sync("hello")
    assert "h012-builtin" in summary_names(system_prompt_of(fake))


# -- kernel: $name invocation --------------------------------------------------------------------


@pytest.mark.fr("FR-006")
@pytest.mark.parametrize("enforce,name,loaded", [
    (True, "h012-cert", True),
    (True, "h012-nocert", False),
    (True, "h012-badsig", False),
    (True, "h012-revoked", False),
    (False, "h012-nocert", True),
    (False, "h012-badsig", True),
])
def test_dollar_invocation(kmaker, tmp_path, enforce, name, loaded):
    fx = gate_fixture(tmp_path)
    kernel, fake = kmaker(work=fx["work"], caps=_caps(fx, enforce), builtin=fx["builtin"])
    agent_of(kernel).run_sync(f"Please use ${name} on this")
    assert (f"BODY-MARK-{name}" in user_message_of(fake)) is loaded


# -- kernel: script tool registration -------------------------------------------------------------


@pytest.mark.fr("FR-006", "FR-010")
def test_enforced_registers_only_certified_script_tools(kmaker, tmp_path):
    fx = gate_fixture(tmp_path)
    kernel, _ = kmaker(work=fx["work"], caps=_caps(fx, True), script_enabled=True)
    names = tool_names(agent_of(kernel))
    assert "script_h012-tcert" in names
    assert "script_h012-tnocert" not in names
    assert "script_h012-tbad" not in names
    assert {"read_file", "write_file", "exec"} <= set(names), "built-in tools unaffected"


@pytest.mark.fr("FR-006", "FR-010")
def test_unenforced_registers_every_valid_script_tool(kmaker, tmp_path):
    fx = gate_fixture(tmp_path)
    kernel, _ = kmaker(work=fx["work"], caps=_caps(fx, False), script_enabled=True)
    names = tool_names(agent_of(kernel))
    for name in SCRIPTS:
        assert f"script_{name}" in names


@pytest.mark.fr("FR-005", "FR-010")
@pytest.mark.parametrize("enforce", [True, False])
def test_script_tools_disabled_registers_none(kmaker, tmp_path, enforce):
    fx = gate_fixture(tmp_path)
    kernel, _ = kmaker(work=fx["work"], caps=_caps(fx, enforce), script_enabled=False)
    assert not [n for n in tool_names(agent_of(kernel)) if n.startswith("script_")]


@pytest.mark.fr("FR-005", "FR-010")
def test_script_tools_off_by_default(kmaker, tmp_path):
    fx = gate_fixture(tmp_path)
    kernel, _ = kmaker(work=fx["work"])
    assert not [n for n in tool_names(agent_of(kernel)) if n.startswith("script_")]


@pytest.mark.fr("FR-006")
def test_enforced_builtin_tools_match_unenforced(kmaker, tmp_path):
    fx = gate_fixture(tmp_path)
    k_on, _ = kmaker(work=fx["work"], caps=_caps(fx, True), script_enabled=True)
    k_off, _ = kmaker(work=fx["work"], caps=_caps(fx, False), script_enabled=True)
    on = {n for n in tool_names(agent_of(k_on)) if not n.startswith("script_")}
    off = {n for n in tool_names(agent_of(k_off)) if not n.startswith("script_")}
    assert on == off


@pytest.mark.fr("FR-006")
def test_inline_action_tool_unaffected(kmaker, tmp_path):
    from moeka.agents import AgentSpec

    def h012_action(text: str) -> str:
        """Echo the text."""
        return text

    fx = gate_fixture(tmp_path)
    kernel, _ = kmaker(work=fx["work"], caps=_caps(fx, True), script_enabled=True)
    agent = kernel.agent(AgentSpec(name="h012-inline", actions=[h012_action]))
    assert "h012_action" in tool_names(agent)


# -- SkillsLoader -------------------------------------------------------------------------------


@pytest.mark.fr("FR-006")
@pytest.mark.parametrize("enforce", [True, False])
def test_loader_lists(tmp_path, enforce):
    fx = gate_fixture(tmp_path)
    loader = skills_loader(fx["work"], tmp_path / "st", caps=_caps(fx, enforce),
                           builtin=fx["builtin"])
    names = listed(loader)
    expected = set(WS_SKILLS) if not enforce else {"h012-cert"}
    assert {n for n in names if n in WS_SKILLS} == expected
    assert "h012-builtin" in names


@pytest.mark.fr("FR-006")
@pytest.mark.parametrize("enforce,name,loadable", [
    (True, "h012-cert", True), (True, "h012-nocert", False), (True, "h012-badsig", False),
    (True, "h012-revoked", False), (False, "h012-nocert", True), (False, "h012-revoked", True),
])
def test_loader_load_skill(tmp_path, enforce, name, loadable):
    fx = gate_fixture(tmp_path)
    loader = skills_loader(fx["work"], tmp_path / "st", caps=_caps(fx, enforce))
    body = loader.load_skill(name)
    assert (body is not None and f"BODY-MARK-{name}" in body) is loadable


@pytest.mark.fr("FR-006")
@pytest.mark.parametrize("enforce", [True, False])
def test_loader_explicit_invocation(tmp_path, enforce):
    fx = gate_fixture(tmp_path)
    loader = skills_loader(fx["work"], tmp_path / "st", caps=_caps(fx, enforce))
    text = " ".join(f"${n}" for n in WS_SKILLS)
    got = set(loader.get_explicitly_invoked_skills(text))
    assert got == ({"h012-cert"} if enforce else set(WS_SKILLS))


@pytest.mark.fr("FR-006")
def test_loader_summary_hides_refused(tmp_path):
    fx = gate_fixture(tmp_path)
    loader = skills_loader(fx["work"], tmp_path / "st", caps=_caps(fx, True))
    summary = loader.build_skills_summary()
    assert "h012-cert" in summary
    for refused in ("h012-nocert", "h012-badsig", "h012-revoked"):
        assert refused not in summary


@pytest.mark.fr("FR-006")
def test_inline_skill_unaffected(tmp_path):
    fx = gate_fixture(tmp_path)
    inline = [{"name": "h012-inline", "description": "Inline skill",
               "content": "INLINE-BODY-MARK"}]
    loader = skills_loader(fx["work"], tmp_path / "st", caps=_caps(fx, True), inline=inline)
    assert "h012-inline" in listed(loader)
    assert "INLINE-BODY-MARK" in (loader.load_skill("h012-inline") or "")
    assert "h012-inline" in loader.get_explicitly_invoked_skills("use $h012-inline")


@pytest.mark.fr("FR-006")
def test_plugin_skill_unaffected(tmp_path):
    from nanobot.agent.plugins import (
        AGENT_PLUGIN_SCHEMA,
        plugin_data_root,
        set_agent_plugin_enabled,
    )

    fx = gate_fixture(tmp_path)
    work = fx["work"]
    root = work / "plugins" / "h012plug"
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(json.dumps({"$schema": AGENT_PLUGIN_SCHEMA,
                                                  "name": "h012plug"}), encoding="utf-8")
    sk = root / "skills" / "h012-plugin-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: h012-plugin-skill\ndescription: Plugin skill.\n"
                                 "---\n\nPLUGIN-BODY-MARK\n", encoding="utf-8")
    loader = skills_loader(work, tmp_path / "st", caps=_caps(fx, True))
    set_agent_plugin_enabled(work, "h012plug", True, data_root=plugin_data_root(loader._env))
    baseline = skills_loader(work, tmp_path / "st", caps=_caps(fx, False))
    if "h012-plugin-skill" not in listed(baseline):
        pytest.fail("fixture: the plugin skill is not listed even without enforcement")
    assert "h012-plugin-skill" in listed(loader)
    assert "PLUGIN-BODY-MARK" in (loader.load_skill("h012-plugin-skill") or "")


# -- refused logged once per (name, reason) --------------------------------------------------------


def _mentions(logs, name):
    return [(lvl, msg) for no, lvl, msg in logs if no >= 20 and name in msg]


@pytest.mark.fr("FR-006")
def test_refused_skill_logged_once_per_name_and_reason(tmp_path, logs):
    alpha, beta = Signer("alpha"), Signer("beta")
    name = f"h012-once-{uuid.uuid4().hex[:8]}"
    work = tmp_path / "work"
    tdir = trust_dir(tmp_path / "trust", alpha)
    bundle = write_skill(work, name)
    caps = caps_for(True, tdir)
    for i in range(2):
        loader = skills_loader(work, tmp_path / f"st{i}", caps=caps)
        for _ in range(3):
            assert name not in listed(loader)
            loader.build_skills_summary()
            loader.load_skill(name)
    first = _mentions(logs, name)
    assert len(first) == 1, first
    assert "no-cert" in first[0][1]
    certify(bundle, beta)  # now a different reason: unknown-key
    for i in range(2):
        loader = skills_loader(work, tmp_path / f"su{i}", caps=caps)
        assert name not in listed(loader)
    second = _mentions(logs, name)[1:]
    assert len(second) == 1, second
    assert "unknown-key" in second[0][1]


@pytest.mark.fr("FR-006")
def test_refused_script_tool_logged_once(kmaker, tmp_path, logs):
    alpha = Signer("alpha")
    name = f"h012-tonce-{uuid.uuid4().hex[:8]}"
    work = tmp_path / "work"
    tdir = trust_dir(tmp_path / "trust", alpha)
    write_script_tool(work, name)
    for _ in range(2):
        kernel, _ = kmaker(work=work, caps=caps_for(True, tdir), script_enabled=True)
        assert f"script_{name}" not in tool_names(agent_of(kernel))
    hits = _mentions(logs, name)
    assert len(hits) == 1, hits
    assert "no-cert" in hits[0][1]


@pytest.mark.fr("FR-006")
def test_unenforced_does_not_log_refusals_as_errors(tmp_path, logs):
    fx = gate_fixture(tmp_path)
    loader = skills_loader(fx["work"], tmp_path / "st", caps=_caps(fx, False))
    listed(loader)
    assert not [m for no, _l, m in logs if no >= 40 and "h012-nocert" in m]
