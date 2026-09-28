"""Environment.for_host / from_config: explicit host inputs only (I1, I2)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from nanobot.config.schema import Config
from nanobot.kernel.env import PathsOverlapError, StaticCredentialResolver
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.sampling import Sampling
from nanobot.kernel.trace import NullTraceSink
from nanobot.providers.factory import make_provider
from tests.kernel.test_fake_home import (
    SCRUBBED_HOME_VARS,
    SENTINEL,
    _poisoned_names,
    _RecordingEnviron,
    _tree,
)

VLLM_BASE = "http://127.0.0.1:8000/v1"


def _vllm_env(work: Path, state: Path, **kwargs) -> Environment:
    defaults = dict(
        state_dir=state,
        work_dir=work,
        credentials={"vllm-key": "sk-vllm-host"},
        providers=[ProviderSpec(name="vllm", api_base=VLLM_BASE, credential="vllm-key")],
        models=[
            ModelSpec(
                name="local",
                model="Qwen/Qwen3-8B",
                provider="vllm",
                tier="local",
                context_window=32_768,
                max_tokens=2048,
                sampling=Sampling(temperature=0.7, reasoning_effort="low", seed=7),
            ),
            ModelSpec(
                name="big",
                model="Qwen/Qwen3-235B",
                provider="vllm",
                tier="frontier",
                price_in=1.5,
                price_out=6.0,
                price_cache_read=0.15,
            ),
        ],
        default_model="local",
    )
    defaults.update(kwargs)
    return Environment.for_host(**defaults)


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    work, state = tmp_path / "work", tmp_path / "state"
    work.mkdir()
    state.mkdir()
    return work, state


def test_for_host_under_poisoned_home_reads_and_writes_nothing_ambient(
    tmp_path, monkeypatch
) -> None:
    home, work, state, cwd, tmp_root = (
        tmp_path / n for n in ("home", "work", "state", "cwd", "tmp")
    )
    for d in (home, cwd, tmp_root):
        d.mkdir()
    for name in SCRUBBED_HOME_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_root))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_root))
    poisoned = _poisoned_names() | {"HOSTED_VLLM_API_KEY", "NANOBOT_AGENTS__DEFAULTS__MODEL"}
    for name in poisoned:
        monkeypatch.setenv(name, SENTINEL)
    monkeypatch.chdir(cwd)

    environ_before = dict(os.environ)
    recorder = _RecordingEnviron(os.environ)
    try:
        monkeypatch.setattr(os, "environ", recorder)
        # work/state are not created by the host here: construction must not mkdir.
        env = _vllm_env(work, state)
        provider = make_provider(env.config, env=env.core)
    finally:
        monkeypatch.setattr(os, "environ", recorder._inner)

    assert _tree(home) == set()
    assert _tree(cwd) == set()
    assert not work.exists()
    assert not state.exists()
    assert dict(os.environ) == environ_before
    assert recorder.writes == []
    assert sorted(recorder.looked_up & poisoned) == []
    assert env.config.agents.defaults.model == "Qwen/Qwen3-8B"
    assert provider.api_key == "sk-vllm-host"
    assert SENTINEL not in env.config.model_dump_json()


async def test_vllm_provider_spec_reaches_the_client(dirs) -> None:
    env = _vllm_env(*dirs)
    provider = make_provider(env.config, env=env.core)
    assert provider.get_default_model() == "Qwen/Qwen3-8B"
    client = await provider._ensure_client()
    assert str(client.base_url).rstrip("/") == VLLM_BASE
    assert client.api_key == "sk-vllm-host"
    # The key came from the resolver, never from the config.
    assert env.config.providers.vllm.api_key is None
    assert "sk-vllm-host" not in env.config.model_dump_json()


def test_host_resolver_object_and_direct_provider_ref(dirs) -> None:
    resolver = StaticCredentialResolver(
        {"providers/vllm/api_key": "sk-direct"}, scopes={"providers/vllm/api_key": ["provider:vllm"]}
    )
    env = _vllm_env(
        *dirs,
        credentials=resolver,
        providers=[ProviderSpec(name="vllm", api_base=VLLM_BASE)],
    )
    assert make_provider(env.config, env=env.core).api_key == "sk-direct"


def test_mapping_credential_is_scoped_to_its_provider(dirs) -> None:
    env = _vllm_env(*dirs)
    creds = env.core.credentials
    assert creds.resolve("providers/vllm/api_key", "provider:vllm") == "sk-vllm-host"
    assert creds.resolve("vllm-key", "provider:vllm") == "sk-vllm-host"
    assert creds.resolve("vllm-key", "tool:web") is None


def test_preset_fields_round_trip(dirs) -> None:
    env = _vllm_env(*dirs)
    cfg = env.config
    assert cfg.agents.defaults.model_preset == "local"
    local = cfg.resolve_preset()
    assert (local.model, local.provider, local.tier) == ("Qwen/Qwen3-8B", "vllm", "local")
    assert (local.max_tokens, local.context_window_tokens) == (2048, 32_768)
    assert (local.temperature, local.reasoning_effort) == (0.7, "low")
    big = cfg.resolve_preset("big")
    assert big.tier == "frontier"
    assert (big.price_in_per_mtok, big.price_out_per_mtok, big.price_cache_read_per_mtok) == (
        1.5, 6.0, 0.15,
    )
    assert cfg.workspace_path == dirs[0].resolve()
    assert env.default_model == "local"
    assert set(env.models) == {"local", "big"}
    assert env.models["local"].sampling.seed == 7
    assert env.paths.work_dir == dirs[0].resolve()
    assert env.paths.state_dir == dirs[1].resolve()
    assert env.core.strict is True
    assert isinstance(env.trace, NullTraceSink)
    assert env.offline is False


def test_provider_extras_and_tools_section(dirs) -> None:
    env = _vllm_env(
        *dirs,
        providers=[
            ProviderSpec(
                name="vllm",
                api_base=VLLM_BASE,
                extra_headers={"X-Team": "a"},
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
        ],
        tools={"exec": {"enable": False}},
        exec_base_env={"PATH": "/usr/bin"},
        offline=True,
    )
    vllm = env.config.providers.vllm
    assert vllm.extra_headers == {"X-Team": "a"}
    assert vllm.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
    assert env.config.tools.exec.enable is False
    assert env.core.config.section("tools")["exec"]["enable"] is False
    assert dict(env.core.exec_base_env) == {"PATH": "/usr/bin"}
    assert env.offline is True


async def test_custom_provider_name_becomes_openai_compat(dirs) -> None:
    env = _vllm_env(
        *dirs,
        credentials={"k": "sk-custom"},
        providers=[ProviderSpec(name="labproxy", api_base="http://10.0.0.5:9000/v1", credential="k")],
        models=[ModelSpec(name="m", model="some-model", provider="labproxy")],
        default_model="m",
    )
    provider = make_provider(env.config, env=env.core)
    client = await provider._ensure_client()
    assert str(client.base_url).rstrip("/") == "http://10.0.0.5:9000/v1"
    assert client.api_key == "sk-custom"


def test_strict_overlap_raises(tmp_path) -> None:
    with pytest.raises(PathsOverlapError):
        _vllm_env(tmp_path, tmp_path)
    with pytest.raises(PathsOverlapError):
        _vllm_env(tmp_path / "state" / "work", tmp_path / "state")
    env = _vllm_env(tmp_path, tmp_path, strict=False)
    assert env.paths.overlaps and env.core.strict is False


def test_unknown_provider_raises(dirs) -> None:
    with pytest.raises(ValueError, match="unknown provider 'nope'"):
        _vllm_env(*dirs, models=[ModelSpec(name="x", model="m", provider="nope")],
                  default_model="x")


def test_unknown_default_model_raises(dirs) -> None:
    with pytest.raises(ValueError, match="default_model 'missing'"):
        _vllm_env(*dirs, default_model="missing")


def test_reserved_and_duplicate_names_raise(dirs) -> None:
    with pytest.raises(ValueError, match="reserved"):
        _vllm_env(*dirs, models=[ModelSpec(name="default", model="m", provider="vllm")],
                  default_model="default")
    with pytest.raises(ValueError, match="duplicate ModelSpec"):
        _vllm_env(*dirs, models=[ModelSpec(name="a", model="m", provider="vllm")] * 2,
                  default_model="a")


def test_sampling_stop_normalised_to_tuple() -> None:
    assert Sampling(stop=["a", "b"]).stop == ("a", "b")
    assert Sampling(stop="x").stop == ("x",)
    assert Sampling().stop == ()


def test_from_config_wraps_legacy_config_without_ambient_reads(dirs) -> None:
    legacy = Config.model_validate({
        "agents": {"defaults": {"workspace": "/somewhere/else", "model": "gpt-4.1",
                                "provider": "openai"}},
        "providers": {"openai": {"api_key": "sk-cfg"}},
        "model_presets": {"cheap": {"model": "gpt-4.1-mini", "provider": "openai",
                                    "tier": "fast"}},
    })
    env = Environment.from_config(legacy, state_dir=dirs[1], work_dir=dirs[0])
    assert env.config is not legacy
    assert legacy.agents.defaults.workspace == "/somewhere/else"
    assert env.config.workspace_path == dirs[0].resolve()
    assert env.default_model == "default"
    assert set(env.models) == {"default", "cheap"}
    assert env.models["cheap"].tier == "fast"
    assert env.core.strict is False
    assert make_provider(env.config, env=env.core).api_key == "sk-cfg"

    from_dict = Environment.from_config(
        {"agents": {"defaults": {"model": "gpt-4.1", "provider": "openai"}}},
        state_dir=dirs[1], work_dir=dirs[0], credentials={"providers/openai/api_key": "sk-res"},
    )
    assert make_provider(from_dict.config, env=from_dict.core).api_key == "sk-res"


def test_from_config_dict_leaves_the_callers_dict_alone(dirs) -> None:
    import copy

    raw = {
        "agents": {"defaults": {"model": "gpt-4.1", "provider": "openai"}},
        "tools": {"exec": {"restrictToWorkspace": True}, "myEnabled": True},
    }
    before = copy.deepcopy(raw)
    env = Environment.from_config(raw, state_dir=dirs[1], work_dir=dirs[0])
    assert raw == before  # the legacy migration rewrote a copy, not the host's dict
    assert env.config.tools.restrict_to_workspace is True


def test_config_validation_matches_model_validate_and_keeps_cross_checks(monkeypatch) -> None:
    from nanobot.kernel.hostenv import _validate_config

    for name in [k for k in os.environ if k.startswith("NANOBOT_")]:
        monkeypatch.delenv(name)
    data = {
        "agents": {"defaults": {"workspace": "/w", "modelPreset": "p", "maxToolIterations": 9}},
        "providers": {"vllm": {"apiBase": VLLM_BASE}, "labproxy": {"api_base": "http://x/v1"}},
        "modelPresets": {"p": {"model": "m", "provider": "vllm", "tier": "local"}},
        "tools": {"exec": {"enable": False}},
    }
    ours = _validate_config(data)
    assert type(ours) is Config
    assert ours.model_dump() == Config.model_validate(data).model_dump()
    assert ours.providers.model_extra["labproxy"].api_base == "http://x/v1"

    monkeypatch.setenv("NANOBOT_AGENTS__DEFAULTS__MAX_TOOL_ITERATIONS", "3")
    assert _validate_config(data).agents.defaults.max_tool_iterations == 9

    with pytest.raises(ValueError, match="not found in model_presets"):
        _validate_config({"agents": {"defaults": {"model_preset": "nope"}}})
    with pytest.raises(ValueError, match="unknown config section"):
        _validate_config({"channels": {}})
