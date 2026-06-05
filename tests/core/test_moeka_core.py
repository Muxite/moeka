"""Tests for the MoekaCore facade."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nanobot.core import Config, MoekaCore, RunResult


def _write_config(tmp_path: Path) -> Path:
    data = {
        "providers": {"openrouter": {"apiKey": "sk-test-key"}},
        "agents": {"defaults": {"model": "openai/gpt-4.1"}},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data))
    return config_path


def _make_core(tmp_path: Path) -> MoekaCore:
    return MoekaCore.create(config_path=_write_config(tmp_path), workspace=tmp_path)


_CONFIG_DATA = {
    "providers": {"openrouter": {"apiKey": "sk-test-key"}},
    "agents": {"defaults": {"model": "openai/gpt-4.1"}},
}


def test_create_missing_config():
    with pytest.raises(FileNotFoundError):
        MoekaCore.create(config_path="/nonexistent/config.json")


def test_create_builds_loop(tmp_path):
    core = _make_core(tmp_path)
    assert core.loop is not None
    assert core.loop.workspace == tmp_path


# ---------------------------------------------------------------------------
# Data-driven construction — files are optional
# ---------------------------------------------------------------------------

def test_create_from_config_dict_no_file(tmp_path):
    """A plain dict builds a working core with no config.json on disk."""
    core = MoekaCore.create(config_dict=dict(_CONFIG_DATA), workspace=tmp_path)
    assert core.loop is not None
    assert core.loop.workspace == tmp_path
    assert core.loop.tools.has("read_file")  # builtins still load


def test_create_from_config_object(tmp_path):
    """A pre-built Config object is consumed directly (no disk read)."""
    config = Config.model_validate(dict(_CONFIG_DATA))
    core = MoekaCore.from_config(config, workspace=tmp_path)
    assert core.loop.workspace == tmp_path


def test_create_in_memory_uses_ephemeral_workspace(monkeypatch):
    """In-memory config with no workspace lands in temp, never ~/.nanobot."""
    import tempfile

    # Guard: make the default state home explode if anything tries to use it.
    def _boom():
        raise AssertionError("core touched ~/.nanobot for an in-memory config")

    monkeypatch.setattr("nanobot.config.loader.get_state_home", _boom)

    core = MoekaCore.create(config_dict=dict(_CONFIG_DATA))
    try:
        ws = core.workspace
        assert str(ws).startswith(tempfile.gettempdir())
        assert core._ephemeral_workspace == ws
    finally:
        core.cleanup()
    assert not ws.exists()
    assert core._ephemeral_workspace is None


def test_create_explicit_workspace_is_not_ephemeral(tmp_path):
    core = MoekaCore.create(config_dict=dict(_CONFIG_DATA), workspace=tmp_path)
    assert core._ephemeral_workspace is None
    core.cleanup()  # no-op
    assert tmp_path.exists()


def test_create_rejects_multiple_sources(tmp_path):
    with pytest.raises(ValueError):
        MoekaCore.create(
            config=Config.model_validate(dict(_CONFIG_DATA)),
            config_dict=dict(_CONFIG_DATA),
            workspace=tmp_path,
        )


def test_create_resolves_env_placeholders(tmp_path, monkeypatch):
    """`${VAR}` in an in-memory config is resolved from the environment.

    This is the keys.env pattern: secrets live in the environment, the config
    (file or dict) only references them — so it stays shareable.
    """
    monkeypatch.setenv("MOEKA_TEST_KEY", "sk-resolved-from-env")
    data = {
        "providers": {"openrouter": {"apiKey": "${MOEKA_TEST_KEY}"}},
        "agents": {"defaults": {"model": "openai/gpt-4.1"}},
    }
    core = MoekaCore.create(config_dict=data, workspace=tmp_path)
    assert core.loop.provider.api_key == "sk-resolved-from-env"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def test_action_decorator_registers_tool(tmp_path):
    core = _make_core(tmp_path)

    @core.action
    def get_weather(city: str) -> str:
        """Get weather."""
        return f"sunny in {city}"

    assert core.loop.tools.has("get_weather")
    tool = core.loop.tools.get("get_weather")
    assert tool.description == "Get weather."
    assert tool.parameters["required"] == ["city"]


def test_action_parameterized(tmp_path):
    core = _make_core(tmp_path)

    @core.action(name="lookup", read_only=True)
    def fn(q: str) -> str:
        return q

    assert core.loop.tools.has("lookup")
    assert core.loop.tools.get("lookup").read_only is True


def test_register_and_unregister_action(tmp_path):
    core = _make_core(tmp_path)
    name = core.register_action(lambda x: x, name="echo")
    assert name == "echo"
    assert core.loop.tools.has("echo")
    core.unregister_action("echo")
    assert not core.loop.tools.has("echo")


def test_actions_coexist_with_builtins(tmp_path):
    core = _make_core(tmp_path)
    builtin_count = len(core.loop.tools)
    assert builtin_count > 0  # default tools loaded at construction

    @core.action
    def custom(x: int) -> str:
        return str(x)

    assert len(core.loop.tools) == builtin_count + 1
    assert core.loop.tools.has("read_file")  # a builtin survived


# ---------------------------------------------------------------------------
# Running the loop — the registered action is exposed to the engine
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_returns_result_and_drains_bus(tmp_path):
    from nanobot.bus.events import OutboundMessage

    core = _make_core(tmp_path)
    core.loop.process_direct = AsyncMock(
        return_value=OutboundMessage(channel="cli", chat_id="direct", content="hello back"),
    )

    result = await core.run("hi")
    assert isinstance(result, RunResult)
    assert result.content == "hello back"
    assert core.loop.bus.outbound.empty()


@pytest.mark.asyncio
async def test_run_captures_tools_used(tmp_path):
    from nanobot.agent.hook import AgentHookContext
    from nanobot.bus.events import OutboundMessage
    from nanobot.providers.base import ToolCallRequest

    core = _make_core(tmp_path)

    @core.action
    def do_thing(x: int) -> str:
        return str(x)

    async def fake_process_direct(message, *, session_key, media=None, on_stream=None):
        # The action is visible to the engine at run time.
        assert core.loop.tools.has("do_thing")
        ctx = AgentHookContext(iteration=0, messages=[{"role": "user", "content": message}])
        ctx.tool_calls = [ToolCallRequest(id="c1", name="do_thing", arguments={"x": 1})]
        for h in core.loop._extra_hooks:
            await h.after_iteration(ctx)
        return OutboundMessage(channel="cli", chat_id="direct", content="done")

    core.loop.process_direct = fake_process_direct
    result = await core.run("go")
    assert result.content == "done"
    assert result.tools_used == ["do_thing"]


@pytest.mark.asyncio
async def test_action_invoked_by_real_engine(tmp_path):
    """End-to-end: a registered Python function is actually called by the agent.

    Uses a scripted fake provider (no mocking of process_direct) so the call goes
    through the real runner -> ToolRegistry -> FunctionTool path.
    """
    from unittest.mock import MagicMock

    from nanobot.providers.base import (
        GenerationSettings,
        LLMProvider,
        LLMResponse,
        ToolCallRequest,
    )

    calls: list[dict] = []

    provider = MagicMock(spec=LLMProvider)
    provider.generation = GenerationSettings()
    provider.supports_progress_deltas = False
    step = {"n": 0}

    async def chat_with_retry(**kwargs):
        step["n"] += 1
        if step["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="multiply", arguments={"a": 6, "b": 7})],
            )
        return LLMResponse(content="The answer is 42.", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry

    core = MoekaCore.create(
        config_path=_write_config(tmp_path), workspace=tmp_path, provider=provider,
    )

    @core.action
    def multiply(a: int, b: int) -> str:
        """Multiply two integers."""
        calls.append({"a": a, "b": b})
        return str(a * b)

    result = await core.run("what is 6 times 7?")

    assert calls == [{"a": 6, "b": 7}]          # the host function really ran
    assert "multiply" in result.tools_used
    assert result.content == "The answer is 42."


@pytest.mark.asyncio
async def test_think_returns_text(tmp_path):
    from nanobot.bus.events import OutboundMessage

    core = _make_core(tmp_path)
    core.loop.process_direct = AsyncMock(
        return_value=OutboundMessage(channel="cli", chat_id="direct", content="42"),
    )
    assert await core.think("answer?") == "42"


@pytest.mark.asyncio
async def test_run_restores_hooks_on_error(tmp_path):
    core = _make_core(tmp_path)
    original = core.loop._extra_hooks
    core.loop.process_direct = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await core.run("hi")
    assert core.loop._extra_hooks is original


# ---------------------------------------------------------------------------
# Host-document RAG (requires moeka[vec])
# ---------------------------------------------------------------------------

def test_ingest_noop_without_vec(tmp_path, monkeypatch):
    core = _make_core(tmp_path)
    # Force the unavailable path regardless of installed extras.
    if core.loop.vec_store is not None:
        monkeypatch.setattr(core.loop.vec_store, "_available", False)
    assert core.ingest("some text") == 0
    assert core.retrieve("query") == []
    assert core.vec_available is False


def test_ingest_retrieve_roundtrip(tmp_path):
    pytest.importorskip("sqlite_vec")
    pytest.importorskip("sentence_transformers")

    from nanobot.agent.vec_store import VecStore

    store = VecStore(tmp_path / "vec.db")
    if not store.available:
        pytest.skip("vec backend unavailable")

    n = store.add_documents(
        "The Eiffel Tower is located in Paris, France. "
        "It was completed in 1889 for the World's Fair.",
        source="facts",
    )
    assert n >= 1
    hits = store.search_documents("Where is the Eiffel Tower?", k=3)
    assert any("Paris" in h for h in hits)


def test_ingest_from_file(tmp_path):
    pytest.importorskip("sqlite_vec")
    pytest.importorskip("sentence_transformers")

    core = _make_core(tmp_path)
    if not core.vec_available:
        pytest.skip("vec backend unavailable")

    doc = tmp_path / "note.txt"
    doc.write_text("Project Moeka ships on Friday at noon.")
    assert core.ingest(doc, source=None) >= 1
    hits = core.retrieve("When does the project ship?", k=3)
    assert any("Friday" in h for h in hits)


# ---------------------------------------------------------------------------
# Structured documents API (awork contract) — fake store, no embeddings needed
# ---------------------------------------------------------------------------

class _FakeVecStore:
    """Records calls; returns canned scored results."""

    available = True

    def __init__(self):
        self.added: list[tuple[str, str | None, str]] = []
        self.cleared: list[str | None] = []
        self.scored = [
            ("a.md", "alpha text", 0.2),
            (None, "anonymous text", 0.7),
        ]

    def add_documents(self, text, source=None, *, collection="default"):
        self.added.append((text, source, collection))
        return 1

    def search_documents(self, query, k=5, *, collection="default"):
        return [t for _s, t, _d in self.scored][:k]

    def search_documents_scored(self, query, k=5, *, collection="default"):
        self.last_search = (query, k, collection)
        return self.scored[:k]

    def count_documents(self, *, collection="default"):
        self.last_count = collection
        return 42

    def clear_documents(self, *, collection="default"):
        self.cleared.append(collection)


def _core_with_fake_store(tmp_path):
    core = _make_core(tmp_path)
    fake = _FakeVecStore()
    core.loop.vec_store = fake
    return core, fake


def test_ingest_text_never_path_detects(tmp_path):
    """A path-looking string that names a real file is stored verbatim."""
    core, fake = _core_with_fake_store(tmp_path)
    real_file = tmp_path / "secret.txt"
    real_file.write_text("file contents that must NOT be read")

    assert core.ingest_text(str(real_file), source="raw") == 1
    text, source, collection = fake.added[0]
    assert text == str(real_file)  # the literal string, not the file body
    assert source == "raw"
    assert collection == "default"


def test_ingest_text_blank_is_noop(tmp_path):
    core, fake = _core_with_fake_store(tmp_path)
    assert core.ingest_text("   \n  ") == 0
    assert fake.added == []


def test_ingest_threads_collection(tmp_path):
    core, fake = _core_with_fake_store(tmp_path)
    core.ingest("raw text body", source="s", collection="jobs")
    assert fake.added[0] == ("raw text body", "s", "jobs")


def test_retrieve_documents_returns_chunks_in_order(tmp_path):
    from nanobot.core import RetrievedChunk

    core, fake = _core_with_fake_store(tmp_path)
    chunks = core.retrieve_documents("query", k=2, collection="kb")
    assert fake.last_search == ("query", 2, "kb")
    assert chunks == [
        RetrievedChunk(text="alpha text", source="a.md", score=0.2),
        RetrievedChunk(text="anonymous text", source=None, score=0.7),
    ]


def test_count_and_clear_documents_passthrough(tmp_path):
    core, fake = _core_with_fake_store(tmp_path)
    assert core.count_documents(collection="kb") == 42
    assert fake.last_count == "kb"
    core.clear_documents(collection=None)
    assert fake.cleared == [None]


def test_documents_api_degrades_without_vec(tmp_path):
    core = _make_core(tmp_path)
    core.loop.vec_store = None
    assert core.ingest_text("text") == 0
    assert core.retrieve_documents("q") == []
    assert core.count_documents() == 0
    core.clear_documents()  # must not raise


# ---------------------------------------------------------------------------
# Thinker API — streaming + structured one-shots
# ---------------------------------------------------------------------------

async def test_run_forwards_on_token_as_on_stream(tmp_path):
    core = _make_core(tmp_path)
    seen: list[str] = []

    async def on_token(delta: str) -> None:
        seen.append(delta)

    async def fake_process_direct(message, *, session_key, media=None, on_stream=None):
        assert on_stream is not None
        await on_stream("hel")
        await on_stream("lo")
        from nanobot.bus.events import OutboundMessage

        return OutboundMessage(channel="cli", chat_id="direct", content="hello")

    core.loop.process_direct = fake_process_direct
    result = await core.run("hi", on_token=on_token)
    assert seen == ["hel", "lo"]
    assert result.content == "hello"


async def test_think_structured_delegates_to_acomplete_json(tmp_path, monkeypatch):
    import importlib

    # nanobot.api re-exports shadow the submodule attribute; resolve the module.
    complete_mod = importlib.import_module("nanobot.api.complete")

    captured = {}

    async def fake_acomplete_json(prompt, *, schema=None, model_cls=None, retries=2, **kw):
        captured.update(
            prompt=prompt, schema=schema, model_cls=model_cls, retries=retries, kw=kw
        )
        return {"answer": 42}

    monkeypatch.setattr(complete_mod, "acomplete_json", fake_acomplete_json)
    schema = {"type": "object"}
    out = await MoekaCore.think_structured(
        "question", schema=schema, retries=1, model="m", temperature=0.0
    )
    assert out == {"answer": 42}
    assert captured["prompt"] == "question"
    assert captured["schema"] is schema
    assert captured["retries"] == 1
    assert captured["kw"] == {"model": "m", "temperature": 0.0}


def test_complete_sync_delegates(monkeypatch):
    import importlib

    complete_mod = importlib.import_module("nanobot.api.complete")
    monkeypatch.setattr(complete_mod, "complete", lambda prompt, **kw: f"sync:{prompt}")
    assert MoekaCore.complete_sync("hi") == "sync:hi"
