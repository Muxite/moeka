from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=4096,
        temperature=0.1,
        reasoning_effort=None,
    )
    return provider


def test_request_concurrency_is_unlimited_by_default(
    monkeypatch: pytest.MonkeyPatch,
    loop_factory,
) -> None:
    monkeypatch.delenv("NANOBOT_MAX_CONCURRENT_REQUESTS", raising=False)

    loop = loop_factory(provider=_provider(), patch_deps=True)

    assert loop._concurrency_gate is None


def test_direct_loop_without_env_ignores_process_env(
    monkeypatch: pytest.MonkeyPatch,
    loop_factory,
) -> None:
    """The env var is a legacy-adapter setting; a loop built without an env never reads it."""
    monkeypatch.setenv("NANOBOT_MAX_CONCURRENT_REQUESTS", "2")

    loop = loop_factory(provider=_provider(), patch_deps=True)

    assert loop._concurrency_gate is None


@pytest.mark.asyncio
async def test_positive_request_concurrency_keeps_explicit_cap(
    monkeypatch: pytest.MonkeyPatch,
    loop_factory,
    tmp_path,
) -> None:
    from nanobot.config.schema import Config
    from nanobot.kernel.legacy import LegacyEnvironment

    # Legacy host: the env var still reaches the loop through LegacyEnvironment.
    monkeypatch.setenv("NANOBOT_MAX_CONCURRENT_REQUESTS", "2")
    config = Config.model_validate({"agents": {"defaults": {"workspace": str(tmp_path)}}})
    loop = loop_factory(
        provider=_provider(), patch_deps=True, env=LegacyEnvironment.from_config(config),
    )
    gate = loop._concurrency_gate

    assert gate is not None
    for _ in range(2):
        await gate.acquire()
    try:
        assert gate.locked()
    finally:
        for _ in range(2):
            gate.release()


def test_kernel_env_ignores_process_env_cap(
    monkeypatch: pytest.MonkeyPatch,
    loop_factory,
    tmp_path,
) -> None:
    from tests._kernel_env import credential_env

    monkeypatch.setenv("NANOBOT_MAX_CONCURRENT_REQUESTS", "2")
    # Default kernel test roots live outside the loop workspace (tmp_path).
    loop = loop_factory(provider=_provider(), patch_deps=True, env=credential_env())

    assert loop._concurrency_gate is None
