"""Resolver-aware provider matching (Task 5, carry-forward from Task 4).

``Config.match_provider(model, has_credential=...)``: a kernel host that keeps
provider keys in its resolver (not in config) gets ``provider: "auto"`` matching
and per-run overrides; legacy configs match exactly as before.
"""

from __future__ import annotations

import pytest

from nanobot.config.schema import Config
from nanobot.kernel.legacy import LegacyEnvironment
from nanobot.providers.factory import (
    build_provider_snapshot,
    credential_predicate,
    make_provider,
)
from tests._kernel_env import credential_env

REF = "providers/deepseek/api_key"


def _config(tmp_path, **providers) -> Config:
    return Config.model_validate({
        "providers": providers,
        "agents": {"defaults": {"workspace": str(tmp_path), "model": "deepseek-chat"}},
    })


def _resolver_env(tmp_path):
    return credential_env(
        {REF: "host-deepseek-key"}, scopes={REF: ["provider:deepseek"]}, root=tmp_path / "host"
    )


def test_config_key_host_matches_as_before(tmp_path):
    config = _config(tmp_path, deepseek={"apiKey": "cfg-deepseek-key"})
    env = LegacyEnvironment.from_config(config)
    assert config.match_provider()[1] == "deepseek"
    assert config.match_provider(has_credential=credential_predicate(env))[1] == "deepseek"
    provider = make_provider(config, env=env)
    assert provider.api_key == "cfg-deepseek-key"


def test_legacy_predicate_adds_nothing(tmp_path):
    config = _config(tmp_path, deepseek={"apiKey": "cfg-deepseek-key"})
    has = credential_predicate(LegacyEnvironment.from_config(config))
    assert has is not None
    # Config-backed refs are not a second source: matching stays config-only.
    assert has("deepseek") is False


def test_legacy_hot_reload_does_not_resurrect_removed_key(tmp_path):
    startup = _config(tmp_path, deepseek={"apiKey": "cfg-deepseek-key"})
    env = LegacyEnvironment.from_config(startup)
    fresh = _config(tmp_path, openrouter={"apiKey": "cfg-openrouter-key"})
    assert fresh.match_provider()[1] == "openrouter"
    provider = make_provider(fresh, env=env)
    assert provider.api_key == "cfg-openrouter-key"


def test_resolver_only_key_host_matches_auto(tmp_path):
    config = _config(tmp_path)
    env = _resolver_env(tmp_path)
    assert config.match_provider()[1] is None  # config-only rule: nothing keyed
    assert config.match_provider(has_credential=credential_predicate(env))[1] == "deepseek"
    provider = make_provider(config, env=env)
    assert provider.api_key == "host-deepseek-key"
    assert provider.get_default_model() == "deepseek-chat"


def test_resolver_only_key_host_without_env_still_fails(tmp_path):
    with pytest.raises(ValueError, match="No provider is configured"):
        make_provider(_config(tmp_path))


def test_resolver_only_key_per_run_override_uses_auto(tmp_path):
    config = _config(tmp_path)
    env = _resolver_env(tmp_path)
    base = config.resolve_preset()
    preset = base.model_copy(update={"model": "deepseek-reasoner", "provider": "auto"})
    snapshot = build_provider_snapshot(config, preset=preset, env=env)
    assert snapshot.model == "deepseek-reasoner"
    assert snapshot.provider.api_key == "host-deepseek-key"
    # The signature names the provider that was actually built, not the key.
    assert snapshot.signature[2] == "deepseek"
    assert "host-deepseek-key" not in repr(snapshot.signature)


def test_resolver_key_in_other_scope_does_not_count(tmp_path):
    env = credential_env(
        {REF: "host-deepseek-key"}, scopes={REF: ["tool:web"]}, root=tmp_path / "host"
    )
    config = _config(tmp_path)
    assert config.match_provider(has_credential=credential_predicate(env))[1] is None
