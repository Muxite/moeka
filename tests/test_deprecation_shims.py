"""Task 12: every legacy shim warns exactly once, pointing at the caller — and
none of the *new* Kernel-facing code (``nanobot/kernel``, ``moeka``) triggers it.

Part A exercises the awork-compatibility surface directly (``nanobot.api.complete``,
``nanobot.core.MoekaCore``/``MoekaKernel``, ``nanobot.core.vec.open_vec_store``) and
checks each call/construction emits exactly one ``DeprecationWarning`` whose
``filename`` is this file (i.e. it points at the call site below, not at some
internal frame). Part B builds a ``moeka.Kernel`` and drives its llm / agent /
memory / sessions / epistemics paths with ``DeprecationWarning`` turned into an
error, and asserts nothing raises.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from nanobot.config.schema import AgentProfileConfig, Config
from nanobot.core import MoekaCore
from nanobot.providers.base import LLMResponse, LLMUsage

_CONFIG_DICT = {"providers": {"openrouter": {"apiKey": "sk-test"}}}

# ---------------------------------------------------------------------------
# Part A: each shim's own warning — one per call, filename == this file.
# ---------------------------------------------------------------------------


class _StubProvider:
    """Answers a fixed script; accepts (and ignores) ``provider_context``."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)

    async def chat_with_retry(self, *, messages, max_tokens=None, temperature=None,
                               provider_context=None):
        content = self._replies.pop(0)
        usage = LLMUsage.reported(input_tokens=5, output_tokens=3)
        return LLMResponse(content=content, finish_reason="stop", usage=usage)

    async def chat_stream_with_retry(self, *, messages, max_tokens=None, temperature=None,
                                      on_content_delta=None):
        content = self._replies.pop(0)
        if on_content_delta is not None:
            await on_content_delta(content)
        return LLMResponse(content=content, finish_reason="stop")


def _install(monkeypatch, provider: _StubProvider) -> None:
    import nanobot.providers.factory as factory

    monkeypatch.setattr(
        factory, "make_provider",
        lambda config, *, preset_name=None, preset=None, model=None, **kw: provider,
    )


def _one_deprecation(recorded: list[warnings.WarningMessage]) -> warnings.WarningMessage:
    deprecations = [w for w in recorded if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1, f"expected exactly one DeprecationWarning, got {deprecations}"
    return deprecations[0]


def _assert_here(warning: warnings.WarningMessage, needle: str) -> None:
    assert warning.filename == __file__, warning.filename
    assert needle in str(warning.message)


async def test_acomplete_warns_once_here(monkeypatch):
    from nanobot.api.complete import acomplete

    _install(monkeypatch, _StubProvider(["hi"]))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = await acomplete("hi", config_dict=dict(_CONFIG_DICT))
    assert out == "hi"
    _assert_here(_one_deprecation(caught), "nanobot.api.complete.acomplete")


def test_complete_warns_once_here(monkeypatch):
    from nanobot.api.complete import complete

    _install(monkeypatch, _StubProvider(["hi"]))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = complete("hi", config_dict=dict(_CONFIG_DICT))
    assert out == "hi"
    _assert_here(_one_deprecation(caught), "nanobot.api.complete.complete")


async def test_acomplete_json_warns_once_here(monkeypatch):
    from nanobot.api.complete import acomplete_json

    _install(monkeypatch, _StubProvider(['{"ok": true}']))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = await acomplete_json("q", config_dict=dict(_CONFIG_DICT))
    assert out == {"ok": True}
    _assert_here(_one_deprecation(caught), "nanobot.api.complete.acomplete_json")


def test_complete_json_warns_once_here(monkeypatch):
    from nanobot.api.complete import complete_json

    _install(monkeypatch, _StubProvider(['{"ok": true}']))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = complete_json("q", config_dict=dict(_CONFIG_DICT))
    assert out == {"ok": True}
    _assert_here(_one_deprecation(caught), "nanobot.api.complete.complete_json")


async def test_acomplete_stream_warns_once_here(monkeypatch):
    from nanobot.api.complete import acomplete_stream

    _install(monkeypatch, _StubProvider(["chunk"]))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        chunks = [c async for c in acomplete_stream("hi", config_dict=dict(_CONFIG_DICT))]
    assert chunks == ["chunk"]
    _assert_here(_one_deprecation(caught), "nanobot.api.complete.acomplete_stream")


def test_complete_stream_warns_once_here(monkeypatch):
    from nanobot.api.complete import complete_stream

    _install(monkeypatch, _StubProvider(["chunk"]))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        chunks = list(complete_stream("hi", config_dict=dict(_CONFIG_DICT)))
    assert chunks == ["chunk"]
    _assert_here(_one_deprecation(caught), "nanobot.api.complete.complete_stream")


def test_open_vec_store_warns_once_here(tmp_path):
    from nanobot.core.vec import open_vec_store

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        store = open_vec_store(tmp_path / "vec.db")
    _assert_here(_one_deprecation(caught), "nanobot.core.vec.open_vec_store")
    store.close()


def test_moeka_core_create_warns_once_here(tmp_path):
    config = Config.model_validate(dict(_CONFIG_DICT))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core = MoekaCore.create(config=config, workspace=tmp_path)
    try:
        _assert_here(_one_deprecation(caught), "nanobot.core.MoekaCore.create")
    finally:
        core.cleanup()


def test_moeka_core_from_config_warns_once_here(tmp_path):
    config = Config.model_validate(dict(_CONFIG_DICT))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        core = MoekaCore.from_config(config, workspace=tmp_path)
    try:
        _assert_here(_one_deprecation(caught), "nanobot.core.MoekaCore.from_config")
    finally:
        core.cleanup()


def test_moeka_core_scoped_warns_once_here():
    config = Config.model_validate(dict(_CONFIG_DICT))
    profile = AgentProfileConfig(tools_allow=["web_search"], skills_include=[])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with MoekaCore.scoped(profile=profile, config=config, workspace=None):
            pass
    _assert_here(_one_deprecation(caught), "nanobot.core.MoekaCore.scoped")


async def test_moeka_core_scoped_async_warns_once_here():
    config = Config.model_validate(dict(_CONFIG_DICT))
    profile = AgentProfileConfig(tools_allow=["web_search"], skills_include=[])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with MoekaCore.scoped_async(profile=profile, config=config, workspace=None):
            pass
    _assert_here(_one_deprecation(caught), "nanobot.core.MoekaCore.scoped_async")


# ---------------------------------------------------------------------------
# Part B: the new Kernel surface never triggers a legacy DeprecationWarning.
# ---------------------------------------------------------------------------


async def test_kernel_paths_emit_no_deprecation_warning(tmp_path: Path) -> None:
    """llm / agent / memory / sessions / epistemics through ``moeka.Kernel`` — none
    of it may route through a legacy shim (ruling 3: kernel-internal code must call
    the non-deprecated internal instead)."""
    from moeka.agents import AgentSpec
    from moeka.testing import FakeProvider
    from nanobot.kernel.epistemics import Epistemics
    from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
    from nanobot.kernel.kernel import Kernel

    env = Environment.for_host(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        credentials={"oa": "sk-test"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main",
    )
    main = ModelSpec(name="main", model="fake-main", provider="openai", max_tokens=100)

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with Kernel(env) as kernel:
            kernel.llm.register_provider("main", FakeProvider(["hello there"]), main)

            # llm
            completion = kernel.llm.complete_sync("hi")
            assert completion.text == "hello there"

            # agent
            kernel.llm.register_provider("main", FakeProvider(["done"]), main)
            agent = kernel.agent(AgentSpec(name="a"))
            result = agent.run_sync("hi")
            assert result.content == "done"

            # sessions
            session = kernel.sessions.create_sync("s1")
            session.append_sync({"role": "user", "content": "hi"})

            # memory
            store = kernel.memory("docs")
            store.add("some text about deprecation shims")
            store.search("deprecation")

            # epistemics
            epi: Epistemics = kernel.epistemics
            fact_id = epi.record_fact("hello", source="user", ref="test:x")
            assert epi.fact(fact_id) is not None
