"""Document memory on a kernel (Task 11): ``kernel.memory`` / ``DocStore``.

Keyword (FTS5) paths run everywhere; vector/hybrid-fusion paths need sqlite-vec and
use a deterministic fake embedder (never a model download), so they skip without it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import nanobot.kernel.memory as memory_mod
from moeka.agents import AgentSpec
from moeka.memory import DocStore, Hit
from moeka.testing import FakeProvider
from nanobot.core.vec import open_vec_store
from nanobot.kernel.hostenv import Environment, ModelSpec, ProviderSpec
from nanobot.kernel.kernel import Kernel
from nanobot.kernel.memory import scope_filename, validate_scope
from nanobot.providers.base import LLMResponse, LLMUsage, ToolCallRequest

try:
    import sqlite_vec  # noqa: F401

    _HAS_SQLITE_VEC = True
except ImportError:
    _HAS_SQLITE_VEC = False

needs_sqlite_vec = pytest.mark.skipif(not _HAS_SQLITE_VEC, reason="sqlite-vec not installed")

MAIN = ModelSpec(name="main", model="fake-main", provider="openai", max_tokens=100)


class FakeEmbedder:
    """Deterministic bag-of-words hashing embedder; counts constructions and calls."""

    constructed = 0
    DIM = 32

    def __init__(self, model_name: str = "fake-embedder") -> None:
        type(self).constructed += 1
        self.model_name = model_name
        self.available = True
        self.calls = 0
        self.lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[array]:
        # float32 arrays (``tobytes`` like numpy's); no numpy needed.
        with self.lock:
            self.calls += 1
        out = []
        for text in texts:
            vec = [0.0] * self.DIM
            for word in re.findall(r"\w+", text.lower()):
                h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
                vec[h % self.DIM] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append(array("f", (v / norm for v in vec)))
        return out


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    FakeEmbedder.constructed = 0
    monkeypatch.setattr(memory_mod, "SentenceTransformerEmbedder", FakeEmbedder)
    return FakeEmbedder


def _env(tmp_path: Path) -> Environment:
    return Environment.for_host(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        credentials={"oa": "sk-test"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main",
    )


@pytest.fixture
def kernel(tmp_path):
    k = Kernel(_env(tmp_path))
    yield k
    k.close()


def _keyword_ok(store: DocStore) -> None:
    if not store.keyword_available:
        pytest.skip("FTS5 unavailable")


# -- scopes -------------------------------------------------------------------------


def test_scopes_are_isolated_files_under_state_dir(kernel) -> None:
    a, b = kernel.memory("a"), kernel.memory("b")
    _keyword_ok(a)
    assert a.add("The quick brown fox jumps over the lazy dog.", source="fox.md") == 1
    assert [h.source for h in a.search("fox", mode="keyword")] == ["fox.md"]
    assert b.search("fox") == []
    assert b.count() == 0
    root = kernel.env.paths.state_dir / "memory"
    assert a.path == root / "a.db" and b.path == root / "b.db"
    assert a.path.is_file()
    assert not (kernel.env.paths.work_dir / "memory").exists()


def test_scope_names_are_validated_and_encoded(kernel, tmp_path) -> None:
    for bad in ["", "  ", "../x", "a/b", "a\\b", "..", "x..y", "a\x00b", 3, "x" * 200]:
        with pytest.raises(ValueError):
            validate_scope(bad)
        with pytest.raises(ValueError):
            kernel.memory(bad)
    assert scope_filename("agent:coach") == "agent%3Acoach.db"
    assert scope_filename("agent_coach") != scope_filename("agent:coach")
    assert kernel.memory().path.name == "default.db"
    assert kernel.memory() is kernel.memory("default")
    assert kernel.memory("agent:coach").scope == "agent:coach"
    with pytest.raises(ValueError, match="not both"):
        kernel.memory("a", path=tmp_path / "x.db")


def test_same_file_same_handle_and_path_stores(kernel, tmp_path) -> None:
    host = kernel.memory(path=tmp_path / "corpus" / "kb.db")
    assert host is kernel.memory(path=str(tmp_path / "corpus" / "kb.db"))
    assert host.scope is None
    _keyword_ok(host)
    host.add("alpha beta", source="s")
    assert host.count() == 1 and (tmp_path / "corpus" / "kb.db").is_file()


def test_closed_kernel_refuses_memory_and_handles(tmp_path) -> None:
    kernel = Kernel(_env(tmp_path))
    store = kernel.memory("a")
    store.add("hello world")
    kernel.close()
    with pytest.raises(RuntimeError, match="closed"):
        kernel.memory("a")
    with pytest.raises(RuntimeError, match="closed"):
        store.count()


# -- API ----------------------------------------------------------------------------


def test_sources_count_and_clear_by_source(kernel) -> None:
    store = kernel.memory("kb")
    _keyword_ok(store)
    store.add("one fish", source="block-a", collection="phrases")
    store.add("two fish", source="block-a", collection="phrases")
    store.add("red fish", source="block-b", collection="phrases")
    store.add("blue fish", source="block-b", collection="other")
    store.add("no source fish")
    assert store.sources() == {"block-a": 2, "block-b": 2}
    assert store.sources(collection="phrases") == {"block-a": 2, "block-b": 1}
    assert store.count() == 5
    assert store.count(collection="phrases") == 3
    assert store.count(source="block-b") == 2
    assert store.count(collection="phrases", source="block-b") == 1
    # clear() defaults to the "default" collection, like add/search (and legacy).
    store.clear(source="block-a")  # nothing of block-a lives in "default"
    assert store.sources() == {"block-a": 2, "block-b": 2}
    store.clear(collection="phrases", source="block-a")
    assert store.sources() == {"block-b": 2}
    assert store.search("one", mode="keyword", collection=None) == []
    store.clear(collection="other", source="block-b")
    assert store.sources() == {"block-b": 1}
    store.clear()  # only the unsourced chunk in "default"
    assert store.count() == 1 and store.count(collection="default") == 0
    store.clear(collection=None)  # everything, explicitly
    assert store.count() == 0 and store.sources() == {}


def test_long_and_non_ascii_scopes_get_bounded_file_names(kernel) -> None:
    thai = "agent:" + "\u0e01" * 30  # 281 bytes percent-encoded
    long_cjk = "agent:" + "\u52a9\u624b" * 60
    names = {scope_filename(s) for s in (thai, long_cjk, thai + "x", long_cjk[:-1])}
    assert len(names) == 4
    for scope in (thai, long_cjk):
        name = scope_filename(scope)
        assert len(name.encode()) <= 200 and name == scope_filename(scope)
        assert name.startswith("agent%3A%E0%B8%81") or name.startswith("agent%3A%E5")
        store = kernel.memory(scope)
        _keyword_ok(store)
        assert store.add("sawasdee krap", source="greeting") == 1
        assert [h.source for h in store.search("sawasdee", mode="keyword")] == ["greeting"]
    assert scope_filename("agent:coach") == "agent%3Acoach.db"  # short names unchanged


def test_unopenable_store_raises_instead_of_going_silent(kernel, tmp_path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not a directory")
    store = kernel.memory(path=blocker / "kb.db")
    with pytest.raises(RuntimeError, match="cannot open document store"):
        store.add("hello")
    with pytest.raises(RuntimeError, match="cannot open document store"):
        store.search("hello")


def test_search_hits_carry_tags_collection_and_filters(kernel) -> None:
    store = kernel.memory("kb")
    _keyword_ok(store)
    store.add("Acme ships rockets weekly.", source="acme", tags=["co:acme", "r"],
              collection="research")
    store.add("Globex ships devices.", source="globex", tags=["co:globex", "r"],
              collection="research")
    hits = store.search("ships", mode="keyword", collection="research", tags=["co:acme"])
    assert hits == [Hit(source="acme", text="Acme ships rockets weekly.", score=hits[0].score,
                        tags=("co:acme", "r"), collection="research")]
    assert store.search("ships", mode="keyword") == []  # default collection is "default"
    assert len(store.search("ships", mode="keyword", collection=None)) == 2
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert store.search("ships", mode="keyword", collection=None, since=future) == []
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert len(store.search("ships", collection=None, since=past)) == 2
    with pytest.raises(ValueError, match="timezone"):
        store.search("ships", since=datetime.now())
    with pytest.raises(ValueError, match="mode"):
        store.search("ships", mode="fuzzy")
    with pytest.raises(ValueError):
        store.search("ships", k=0)
    with pytest.raises(TypeError):
        store.add("x", tags="co:acme")


def test_meta_roundtrip_and_availability(kernel) -> None:
    store = kernel.memory("kb")
    assert store.get_meta("k") is None
    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"
    assert store.available is _HAS_SQLITE_VEC
    assert isinstance(store.keyword_available, bool)


# -- the shared embedder -------------------------------------------------------------


def test_one_embedder_serves_every_scope(kernel, fake_embedder) -> None:
    stores = [kernel.memory(f"scope-{i}") for i in range(5)]
    for i, store in enumerate(stores):
        store.add(f"document number {i} about foxes")
    assert fake_embedder.constructed == 1
    embedders = {id(s._vec._embedder) for s in stores}
    assert len(embedders) == 1
    shared = stores[0]._vec._embedder
    if _HAS_SQLITE_VEC:
        assert shared.calls >= 5  # every store embedded through the one instance
        assert all(s.available for s in stores)
        assert stores[3].search("foxes", mode="vec")[0].text.endswith("3 about foxes")


# -- the LRU ------------------------------------------------------------------------


def test_lru_releases_least_recent_and_held_handles_reopen(kernel) -> None:
    registry = kernel._memory_stores()
    registry.capacity = 2
    a, b, c = kernel.memory("a"), kernel.memory("b"), kernel.memory("c")
    _keyword_ok(a)
    a.add("apples are red", source="a")
    b.add("bananas are yellow", source="b")
    assert registry.open_paths() == [a.path, b.path]
    c.add("cherries are dark", source="c")
    assert registry.open_paths() == [b.path, c.path]
    assert a._vec._conn is None  # released (closed), not dropped
    # The held handle still works: it reopens and becomes most recent.
    assert [h.source for h in a.search("apples", mode="keyword")] == ["a"]
    assert registry.open_paths() == [c.path, a.path]
    assert b._vec._conn is None
    assert b.count() == 1
    # kernel.memory returns the same (held) handle after eviction.
    assert kernel.memory("c") is c


def test_pinned_stores_are_never_evicted(kernel) -> None:
    registry = kernel._memory_stores()
    registry.capacity = 1
    pinned = kernel.memory("agent:x")
    pinned._pin()
    other = kernel.memory("other")
    other.add("text here")
    assert pinned._vec._conn is not None
    assert set(registry.open_paths()) == {pinned.path, other.path}
    pinned.close()  # the agent's loop holds it: close() leaves it open
    assert pinned._vec._conn is not None
    pinned._unpin()
    kernel.memory("third").add("more text")
    assert pinned._vec._conn is None


def test_concurrent_use_under_eviction(kernel) -> None:
    registry = kernel._memory_stores()
    registry.capacity = 2
    stores = [kernel.memory(f"s{i}") for i in range(6)]
    _keyword_ok(stores[0])
    errors: list[BaseException] = []

    def work(store: DocStore, n: int) -> None:
        try:
            for j in range(15):
                store.add(f"entry {n} {j} zebra", source=f"src{n}")
                store.search("zebra", mode="keyword")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(s, i)) for i, s in enumerate(stores)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert [s.count() for s in stores] == [15] * 6


# -- parity with the legacy open_vec_store ----------------------------------------------

_PHRASES = {
    "summary": [
        "Seasoned platform engineer who ships reliable Linux infrastructure.",
        "Built self-hosted observability for a homelab fleet.",
        "Engineer focused on reliable backups and restore drills.",
    ],
    "skills": [
        "Linux administration, systemd, and container orchestration.",
        "Python asyncio services with SQLite persistence.",
        "Reliable infrastructure automation with Ansible.",
    ],
}
_QUERIES = ["reliable Linux infrastructure", "SQLite Python", "backups restore", "homelab"]


def _fill_legacy(store: Any) -> None:
    for block, phrases in _PHRASES.items():
        for phrase in phrases:
            store.add_documents(phrase, source=block, collection="phrases", tags=[block])


def _fill_docstore(store: DocStore) -> None:
    for block, phrases in _PHRASES.items():
        for phrase in phrases:
            store.add(phrase, source=block, collection="phrases", tags=[block])


def _assert_parity(legacy: Any, store: DocStore, mode: str) -> None:
    compared = 0
    for query in _QUERIES:
        for tags in (None, ["summary"], ["skills"]):
            old = legacy.search_documents_scored(
                query, k=4, collection="phrases", mode=mode, tags=tags,
                caller="awork.phrase_cache",
            )
            new = [(h.source, h.text, h.score) for h in store.search(
                query, k=4, collection="phrases", mode=mode, tags=tags,
            )]
            assert new == old, (query, tags)
            compared += bool(old)
    assert compared >= 6


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # Task 12: open_vec_store, on purpose
def test_hybrid_matches_open_vec_store_keyword_fallback(kernel, tmp_path, monkeypatch) -> None:
    # Neither side has a vector backend: hybrid falls back to keyword identically.
    class NoEmbed(FakeEmbedder):
        def __init__(self, model_name: str = "none") -> None:
            super().__init__(model_name)
            self.available = False

    monkeypatch.setattr(memory_mod, "SentenceTransformerEmbedder", NoEmbed)
    legacy = open_vec_store(tmp_path / "legacy.db", embedder=NoEmbed())
    store = kernel.memory(path=tmp_path / "new.db")
    if not store.keyword_available:
        pytest.skip("FTS5 unavailable")
    assert not store.available and not legacy.available
    _fill_legacy(legacy)
    _fill_docstore(store)
    _assert_parity(legacy, store, "hybrid")
    assert store.sources(collection="phrases") == {"skills": 3, "summary": 3}
    legacy.close()


@needs_sqlite_vec
@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # Task 12: open_vec_store, on purpose
def test_hybrid_matches_open_vec_store_with_vectors(kernel, tmp_path) -> None:
    legacy = open_vec_store(tmp_path / "legacy.db", embedder=FakeEmbedder())
    store = kernel.memory(path=tmp_path / "new.db")
    assert legacy.available and store.available
    _fill_legacy(legacy)
    _fill_docstore(store)
    for mode in ("hybrid", "vec", "keyword"):
        _assert_parity(legacy, store, mode)
    legacy.close()


# -- agents -------------------------------------------------------------------------


def _tool_call(name: str, args: dict[str, Any], call_id: str = "c1") -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=args)],
        finish_reason="tool_calls",
        usage=LLMUsage.reported(input_tokens=10, output_tokens=5),
    )


def _tool_message(call: Any, name: str) -> str:
    """The latest *name* tool result the model was sent in *call*."""
    for message in reversed(call.messages):
        if message.get("role") == "tool" and message.get("name") == name:
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content)
    raise AssertionError(f"no {name} tool result in {call.messages!r}")


async def test_agent_memory_uses_the_state_dir_scope(kernel) -> None:
    fake = FakeProvider(["hello"])
    kernel.llm.register_provider("main", fake, MAIN)
    agent = kernel.agent(AgentSpec(name="coach", memory=True))
    result = await agent.run("hi")
    assert result.stop_reason == "completed"
    store = kernel.memory("agent:coach")
    assert agent._memory is store
    assert store.path == kernel.env.paths.state_dir / "memory" / "agent%3Acoach.db"
    assert agent._loop.vec_store is store._vec
    assert store._pinned
    assert not (kernel.env.paths.work_dir / "memory" / "vec.db").exists()
    await agent.aclose()
    assert not store._pinned


def _system_prompt(call: Any) -> str:
    return next(m["content"] for m in call.messages if m.get("role") == "system")


async def test_agent_memory_files_are_per_agent_and_off_by_default(kernel) -> None:
    from nanobot.kernel.agent import agent_memory_dir

    work_memory = kernel.env.paths.work_dir / "memory"
    work_memory.mkdir(parents=True)
    (work_memory / "MEMORY.md").write_text("WORKSPACE-SECRET", encoding="utf-8")
    state = kernel.env.paths.state_dir
    for name in ("alice", "bob"):
        mem = agent_memory_dir(state, f"coach-{name}")
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text(f"MEMORY-OF-{name.upper()}", encoding="utf-8")
    fake = FakeProvider(default="ok")
    kernel.llm.register_provider("main", fake, MAIN)

    await kernel.agent(AgentSpec(name="coach-alice", memory=True)).run("hi")
    prompt = _system_prompt(fake.calls[-1])
    assert "MEMORY-OF-ALICE" in prompt
    assert "MEMORY-OF-BOB" not in prompt and "WORKSPACE-SECRET" not in prompt
    await kernel.agent(AgentSpec(name="coach-bob", memory=True)).run("hi")
    prompt = _system_prompt(fake.calls[-1])
    assert "MEMORY-OF-BOB" in prompt and "MEMORY-OF-ALICE" not in prompt
    # memory=False: no long-term memory at all, its own or the workspace's.
    plain = kernel.agent(AgentSpec(name="plain"))
    await plain.run("hi")
    prompt = _system_prompt(fake.calls[-1])
    assert "MEMORY-OF-" not in prompt and "WORKSPACE-SECRET" not in prompt
    assert plain._loop.context.memory.memory_dir == agent_memory_dir(state, "plain")
    # Nothing new was written into work_dir.
    assert sorted(p.name for p in work_memory.iterdir()) == ["MEMORY.md"]


def test_agent_dirname_is_safe() -> None:
    from nanobot.kernel.agent import agent_dirname

    assert agent_dirname("coach") == "coach"
    assert agent_dirname("..") == "%2E%2E" and agent_dirname("a/b") == "a%2Fb"
    long = agent_dirname("é" * 300)
    assert len(long) < 120 and "~" in long and long != agent_dirname("é" * 301)


async def test_rewind_fork_and_delete_drop_archived_history_for_the_key(kernel) -> None:
    from nanobot.agent.memory import MemoryStore
    from nanobot.kernel.agent import agent_memory_dir

    fake = FakeProvider(default="ok")
    kernel.llm.register_provider("main", fake, MAIN)
    agent = kernel.agent(AgentSpec(name="coach", memory=True))
    key = "coach:dana"
    chat = await kernel.sessions.create(key)
    cp = await chat.append({"role": "user", "content": "one"},
                           {"role": "assistant", "content": "two"})
    await chat.append({"role": "user", "content": "three"},
                      {"role": "assistant", "content": "four"})
    agent.tools  # noqa: B018 - builds the loop (its memory store)
    store = agent._loop.context.memory
    # An archive of the later turns (as consolidation would write), another key's
    # entry, and an agent built in an earlier process with entries for these keys.
    store.append_history("ARCHIVED-LATER-TURNS", session_key=key)
    store.append_history("OTHER-KEY-ENTRY", session_key="coach:other")
    ghost = MemoryStore(kernel.env.paths.work_dir,
                        memory_dir=agent_memory_dir(kernel.env.paths.state_dir, "ghost"))
    ghost.append_history("GHOST-ENTRY", session_key=key)
    ghost.append_history("GHOST-FORK-TARGET", session_key=f"{key}/b")

    await agent.run("again", session=chat)
    assert "ARCHIVED-LATER-TURNS" in _system_prompt(fake.calls[-1])

    await chat.rewind(cp)  # no committed summary survives: the key's entries go
    await agent.run("after rewind", session=chat)
    assert "ARCHIVED-LATER-TURNS" not in _system_prompt(fake.calls[-1])
    contents = [e["content"] for e in store._read_entries()]
    assert contents == ["OTHER-KEY-ENTRY"]
    assert [e["content"] for e in ghost._read_entries()] == ["GHOST-FORK-TARGET"]

    await chat.fork(cp, key=f"{key}/b")  # a new transcript under that key
    assert ghost._read_entries() == []
    await kernel.sessions.delete("coach:other")
    assert store._read_entries() == []


def test_drop_session_history_by_generation(tmp_path) -> None:
    from nanobot.agent.memory import MemoryStore, drop_session_history

    store = MemoryStore(tmp_path, memory_dir=tmp_path / "memory")
    store.append_history("LEGACY", session_key="k")  # no generation stamp: gen 0
    store.append_history("GEN-0", session_key="k", generation=0)
    store.append_history("GEN-1", session_key="k", generation=1)
    store.append_history("OTHER-GEN-1", session_key="other", generation=1)
    assert store._read_entries()[2]["gen"] == 1

    assert drop_session_history(store.history_file, "k", generation=1) == 1
    assert [e["content"] for e in store._read_entries()] == ["LEGACY", "GEN-0", "OTHER-GEN-1"]
    assert store.drop_session_history("k", generation=0) == 2
    assert [e["content"] for e in store._read_entries()] == ["OTHER-GEN-1"]
    # No generation: every entry for the key goes (fork target, delete).
    assert drop_session_history(store.history_file, "other") == 1


def test_session_clear_starts_a_new_history_generation() -> None:
    from nanobot.session.manager import Session, history_generation

    session = Session(key="k")
    assert history_generation(session.metadata) == 0
    session.add_message("user", "hi")
    session.clear()
    assert history_generation(session.metadata) == 1
    session.clear()
    assert history_generation(session.metadata) == 2


async def test_rewind_after_new_keeps_the_archive_of_the_discarded_transcript(kernel) -> None:
    import asyncio

    fake = FakeProvider(["first reply", "PRE-NEW-SUMMARY"], default="ok")
    kernel.llm.register_provider("main", fake, MAIN)
    agent = kernel.agent(AgentSpec(name="coach", memory=True))
    key = "coach:erin"
    chat = await kernel.sessions.create(key)
    await agent.run("before new", session=chat)
    await agent.run("/new", session=chat)  # archives the old transcript in the background
    store = agent._loop.context.memory
    for _ in range(200):
        if any(e["content"] == "PRE-NEW-SUMMARY" for e in store._read_entries()):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the /new archive was never written")
    assert store._read_entries()[-1]["gen"] == 0

    cp = chat.checkpoint()
    await agent.run("after new", session=chat)
    store.append_history("POST-NEW-ARCHIVE", session_key=key, generation=1)
    await chat.rewind(cp)
    # The post-/new archive summarised messages the rewind removed; the pre-/new
    # archive summarised a transcript no rewind can reach, so it stays.
    assert [e["content"] for e in store._read_entries()] == ["PRE-NEW-SUMMARY"]
    await agent.run("after rewind", session=chat)
    assert "PRE-NEW-SUMMARY" in _system_prompt(fake.calls[-1])

    # A late archive of an older generation, landing after a drop, is kept too.
    store.append_history("LATE-OLD-GEN", session_key=key, generation=0)
    await chat.rewind(cp)
    assert "LATE-OLD-GEN" in [e["content"] for e in store._read_entries()]

    # Deleting the key still drops every generation.
    await kernel.sessions.delete(key)
    assert store._read_entries() == []


async def test_doc_scopes_search_tool_in_a_run(kernel) -> None:
    kb = kernel.memory("kb")
    _keyword_ok(kb)
    kb.add("Alice and Bob met at the climbing gym in 2019.", source="notes/alice.md")
    kb.add("Alice prefers bouldering to rope climbing.", source="notes/prefs.md",
           collection="preferences")
    kernel.memory("faq").add("The gym opens at seven.", source="faq.md")
    kernel.memory("secret").add("Alice climbing secret diary", source="diary.md")
    fake = FakeProvider([
        _tool_call("search_documents", {"query": "Alice climbing", "scope": "kb"}, "c1"),
        _tool_call("search_documents", {"query": "Alice climbing", "scope": "secret"}, "c2"),
        _tool_call("search_documents", {"query": "gym"}, "c3"),
        "They met climbing.",
    ])
    kernel.llm.register_provider("main", fake, MAIN)
    agent = kernel.agent(AgentSpec(name="reader", doc_scopes=("kb", "faq")))
    info = next(t for t in agent.tools if t.name == "search_documents")
    assert info.read_only is True
    result = await agent.run("How did Alice and Bob meet?")
    assert result.stop_reason == "completed" and result.content == "They met climbing."
    first = json.loads(_tool_message(fake.calls[1], "search_documents"))
    # Every collection of the scope is searched, not only "default".
    assert {r["source"] for r in first} == {"notes/alice.md", "notes/prefs.md"}
    assert all(r["scope"] == "kb" for r in first)
    denied = _tool_message(fake.calls[2], "search_documents")
    assert "unknown document scope 'secret'" in denied and "kb, faq" in denied
    assert "diary" not in denied
    both = json.loads(_tool_message(fake.calls[3], "search_documents"))
    assert {r["scope"] for r in both} == {"kb", "faq"}


def test_doc_scopes_tool_obeys_the_tool_scope(kernel) -> None:
    with pytest.raises(ValueError, match="tools_deny"):
        kernel.agent(AgentSpec(name="d", doc_scopes=("kb",), tools_deny=("search_documents",)))
    with pytest.raises(ValueError, match="tools_allow"):
        kernel.agent(AgentSpec(name="l", doc_scopes=("kb",), tools_allow=("read_file",)))
    with pytest.raises(ValueError):
        kernel.agent(AgentSpec(name="bad", doc_scopes=("../etc",)))
    agent = kernel.agent(AgentSpec(name="plain"))
    assert "search_documents" not in {t.name for t in agent.tools}
