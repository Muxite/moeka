"""Part B: config (FR-005) and the certified_boost setting (FR-007)."""

from __future__ import annotations

import pytest
from _h012 import (
    Signer,
    agent_of,
    certify,
    config,
    summary_names,
    system_prompt_of,
    trust_dir,
    write_skill,
)


@pytest.mark.fr("FR-005")
def test_capabilities_defaults():
    caps = config().capabilities
    assert caps.enforce_certs is False
    assert list(caps.trust_dirs) == []


@pytest.mark.fr("FR-005")
def test_tools_script_default_disabled():
    assert config().tools.script.enabled is False


@pytest.mark.fr("FR-005")
def test_tools_script_enable():
    assert config(script_enabled=True).tools.script.enabled is True


@pytest.mark.fr("FR-005")
def test_snake_case_keys(tmp_path):
    caps = config({"enforce_certs": True, "trust_dirs": [str(tmp_path)]}).capabilities
    assert caps.enforce_certs is True
    assert [str(p) for p in caps.trust_dirs] == [str(tmp_path)]


@pytest.mark.fr("FR-005")
def test_camel_case_keys(tmp_path):
    from nanobot.config.schema import Config

    cfg = Config.model_validate({
        "capabilities": {"enforceCerts": True, "trustDirs": [str(tmp_path)]},
        "tools": {"script": {"enabled": True}},
    })
    assert cfg.capabilities.enforce_certs is True
    assert [str(p) for p in cfg.capabilities.trust_dirs] == [str(tmp_path)]
    assert cfg.tools.script.enabled is True


@pytest.mark.fr("FR-005")
def test_existing_capabilities_settings_untouched():
    caps = config({"enforce_certs": True}).capabilities
    assert caps.selection == "all" and caps.top_k == 8


@pytest.mark.fr("FR-005", "FR-006")
def test_trust_dir_tilde_is_expanded(kmaker, tmp_path, home):
    """``~`` in ``capabilities.trust_dirs`` means $HOME: a key there certifies a skill."""
    alpha = Signer("alpha")
    trust_dir(home / "h012-trust", alpha)
    work = tmp_path / "work"
    certify(write_skill(work, "h012-tilde"), alpha)
    write_skill(work, "h012-tilde-plain")
    kernel, fake = kmaker(work=work, caps={"enforce_certs": True,
                                           "trust_dirs": ["~/h012-trust"]})
    agent_of(kernel).run_sync("hello")
    names = summary_names(system_prompt_of(fake))
    assert "h012-tilde" in names
    assert "h012-tilde-plain" not in names


# -- FR-007 certified_boost --------------------------------------------------------------------


@pytest.mark.fr("FR-007", "FR-005")
def test_certified_boost_default():
    assert config().capabilities.certified_boost == pytest.approx(0.10)


@pytest.mark.fr("FR-007", "FR-005")
@pytest.mark.parametrize("value", [0, 0.0, 0.5, 1, 1.0])
def test_certified_boost_in_range(value):
    assert config({"certified_boost": value}).capabilities.certified_boost == pytest.approx(value)


@pytest.mark.fr("FR-007", "FR-005")
@pytest.mark.parametrize("value", [-0.01, 1.01, 5])
def test_certified_boost_out_of_range_rejected(value):
    with pytest.raises(ValueError):
        config({"certified_boost": value})


@pytest.mark.fr("FR-007", "FR-005")
def test_certified_boost_camel_case():
    from nanobot.config.schema import Config

    cfg = Config.model_validate({"capabilities": {"certifiedBoost": 0.3}})
    assert cfg.capabilities.certified_boost == pytest.approx(0.3)
