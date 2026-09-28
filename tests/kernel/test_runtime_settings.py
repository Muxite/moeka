"""Non-credential tunables (Task 5): legacy env vars via LegacyEnvironment only.

Every tunable the pre-kernel code read from ``os.environ`` is now a
``runtime`` ConfigSource value. LegacyEnvironment fills it from the old variable
(``LEGACY_ENV_SETTINGS``); a kernel-native env gets defaults even when the
variable is set in the process.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import Config
from nanobot.kernel.legacy import LEGACY_ENV_SETTINGS, LegacyEnvironment
from nanobot.providers.base import DEFAULT_STREAM_IDLE_TIMEOUT_S
from nanobot.providers.factory import make_provider
from tests._kernel_env import DictConfigSource, credential_env

_ALL_SETTING_VARS = [name for names in LEGACY_ENV_SETTINGS.values() for name in names]


@pytest.fixture(autouse=True)
def _clean_settings_env(monkeypatch):
    for name in _ALL_SETTING_VARS:
        monkeypatch.delenv(name, raising=False)


def _config(tmp_path) -> Config:
    return Config.model_validate({
        "providers": {"openai": {"apiKey": "sk-runtime-test"}},
        "agents": {"defaults": {"workspace": str(tmp_path), "model": "openai/gpt-4o"}},
    })


def _kernel_env(tmp_path, runtime: dict | None = None):
    config = DictConfigSource({"runtime": runtime} if runtime else None)
    return credential_env(root=tmp_path / "host", config=config)


def _provider():
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return provider


# -- the runtime section --------------------------------------------------


def test_legacy_runtime_section_maps_every_legacy_setting(tmp_path, monkeypatch):
    for index, name in enumerate(_ALL_SETTING_VARS):
        monkeypatch.setenv(name, f"v{index}")
    runtime = LegacyEnvironment.from_config(_config(tmp_path)).config.section("runtime")
    for setting, names in LEGACY_ENV_SETTINGS.items():
        key = setting.partition("/")[2]
        assert runtime[key] == f"v{_ALL_SETTING_VARS.index(names[0])}"


def test_kernel_env_runtime_section_ignores_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_STREAM_IDLE_TIMEOUT_S", "7")
    assert _kernel_env(tmp_path).config.section("runtime") == {}


# -- stream idle timeout / openai-compat request timeout (provider factory) --


def test_stream_idle_timeout_legacy_env_var_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_STREAM_IDLE_TIMEOUT_S", "7")
    config = _config(tmp_path)
    provider = make_provider(config, env=LegacyEnvironment.from_config(config))
    assert provider.stream_idle_timeout_s == 7.0


def test_stream_idle_timeout_env_none_is_the_legacy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_STREAM_IDLE_TIMEOUT_S", "8")
    assert make_provider(_config(tmp_path)).stream_idle_timeout_s == 8.0


def test_stream_idle_timeout_kernel_env_ignores_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_STREAM_IDLE_TIMEOUT_S", "7")
    provider = make_provider(_config(tmp_path), env=_kernel_env(tmp_path))
    assert provider.stream_idle_timeout_s == DEFAULT_STREAM_IDLE_TIMEOUT_S


def test_stream_idle_timeout_kernel_env_runtime_value(tmp_path):
    env = _kernel_env(tmp_path, {"stream_idle_timeout_s": 12.5})
    assert make_provider(_config(tmp_path), env=env).stream_idle_timeout_s == 12.5


def test_openai_compat_timeout_legacy_vs_kernel(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_OPENAI_COMPAT_TIMEOUT_S", "45")
    config = _config(tmp_path)
    legacy = make_provider(config, env=LegacyEnvironment.from_config(config))
    kernel = make_provider(config, env=_kernel_env(tmp_path))
    assert legacy.request_timeout_s == 45.0
    assert kernel.request_timeout_s == 120.0


# -- concurrency cap (AgentLoop) ------------------------------------------


_BUILT: list = []


@pytest.fixture(autouse=True)
def _close_built_loops():
    # Loops built directly (not by a Kernel) own a SQLite session store: close it so
    # it is not left for the GC (an "unclosed database" ResourceWarning).
    yield
    while _BUILT:
        _BUILT.pop().sessions.close()


def _loop(tmp_path, env, **extra):
    loop = AgentLoop.from_config(
        _config(tmp_path), tool_registry=ToolRegistry(), provider=_provider(), env=env, **extra
    )
    _BUILT.append(loop)
    return loop


def test_max_concurrent_requests_legacy_env_var_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_MAX_CONCURRENT_REQUESTS", "3")
    loop = _loop(tmp_path, None)  # from_config builds the LegacyEnvironment
    assert loop._concurrency_gate is not None
    assert loop._concurrency_gate._value == 3


def test_max_concurrent_requests_kernel_env_ignores_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_MAX_CONCURRENT_REQUESTS", "3")
    assert _loop(tmp_path, _kernel_env(tmp_path))._concurrency_gate is None


def test_max_concurrent_requests_kernel_runtime_and_explicit_arg(tmp_path):
    env = _kernel_env(tmp_path, {"max_concurrent_requests": 2})
    assert _loop(tmp_path, env)._concurrency_gate._value == 2
    assert _loop(tmp_path, env, max_concurrent_requests=5)._concurrency_gate._value == 5


def test_max_concurrent_requests_invalid_value_is_unlimited(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_MAX_CONCURRENT_REQUESTS", "lots")
    assert _loop(tmp_path, None)._concurrency_gate is None


# -- workspace sandbox markers --------------------------------------------


def test_sandbox_marker_legacy_env_var_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_SANDBOX_ENFORCED", "macos_app_sandbox")
    config = _config(tmp_path)
    config.tools.restrict_to_workspace = True
    loop = AgentLoop.from_config(config, tool_registry=ToolRegistry(), provider=_provider())
    status = loop.workspace_scopes.sandbox_status
    assert status.level == "system"
    assert status.provider == "macos_app_sandbox"


def test_sandbox_marker_kernel_env_ignores_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_SANDBOX_ENFORCED", "macos_app_sandbox")
    config = _config(tmp_path)
    config.tools.restrict_to_workspace = True
    loop = AgentLoop.from_config(
        config, tool_registry=ToolRegistry(), provider=_provider(), env=_kernel_env(tmp_path)
    )
    assert loop.workspace_scopes.sandbox_status.level == "application"


# -- skill requires.env ---------------------------------------------------


def _skill(tmp_path, env_name: str) -> None:
    skill_dir = tmp_path / "skills" / "needs-env"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: needs-env\ndescription: needs an env var\n"
        f'metadata: {{"nanobot": {{"requires": {{"env": ["{env_name}"]}}}}}}\n---\nbody\n',
        encoding="utf-8",
    )


def test_skill_requires_env_legacy_env_var_honoured(tmp_path, monkeypatch):
    _skill(tmp_path, "T5_SKILL_TOKEN")
    monkeypatch.setenv("T5_SKILL_TOKEN", "x")
    env = LegacyEnvironment.from_config(_config(tmp_path))
    assert SkillsLoader(tmp_path, env=env).get_skill_availability("needs-env")[0] is True
    # The env-less legacy direct construction keeps the process-env check.
    assert SkillsLoader(tmp_path).get_skill_availability("needs-env")[0] is True


def test_skill_requires_env_kernel_env_uses_resolver_only(tmp_path, monkeypatch):
    _skill(tmp_path, "T5_SKILL_TOKEN")
    monkeypatch.setenv("T5_SKILL_TOKEN", "ambient")
    bare = credential_env(root=tmp_path / "host")
    available, missing = SkillsLoader(tmp_path, env=bare).get_skill_availability("needs-env")
    assert available is False
    assert "ENV: T5_SKILL_TOKEN" in missing
    granted = credential_env({"T5_SKILL_TOKEN": "y"}, scopes={"T5_SKILL_TOKEN": ["skills"]})
    assert SkillsLoader(tmp_path, env=granted).get_skill_availability("needs-env")[0] is True
    requirements = SkillsLoader(tmp_path, env=granted).get_skill_requirements("needs-env")
    assert requirements["missing_env"] == []
