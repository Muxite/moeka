"""Generic epistemic FactStore (Task 22, design I3 / section 7).

Headline properties:
- ``record`` stores a value with provenance (closed ``source_kind`` set, a
  ``source_ref``, an optional ``span``) and returns an opaque random trace ID;
- ``resolve`` returns the record, or ``None`` for an ID that points to nothing;
- the store is SQLite (WAL, busy timeout, ``PRAGMA user_version``) under
  ``state_dir`` and survives reopen, concurrent threads and concurrent processes;
- ``record`` emits a ``fact.recorded`` trace event;
- the database sits behind the file floor in both path layouts.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.facts import (
    FACTS_DB_FILENAME,
    SCHEMA_VERSION,
    FactRecord,
    FactStore,
    FactStoreError,
)
from nanobot.kernel.floors import check_floors
from nanobot.kernel.policy import CapabilityRequest, Principal
from nanobot.security.protected_paths import PROTECTED_MARKER, ProtectedFloor

AGENT = Principal("main", "agent", parent=Principal("host", "host"))


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class _EmptyConfig:
    def section(self, name: str) -> dict[str, Any]:
        return {}


@pytest.fixture
def store(tmp_path: Path):
    s = FactStore(tmp_path / "state")
    yield s
    s.close()


# -- round trip ---------------------------------------------------------------


def test_record_resolve_round_trip(store: FactStore) -> None:
    value = {"host": "nas", "disks": [1, 2.5, None, True], "note": "ü"}
    tid = store.record("document", "doc:42", value, span="120-180")
    rec = store.resolve(tid)
    assert isinstance(rec, FactRecord)
    assert rec.trace_id == tid
    assert rec.source_kind == "document"
    assert rec.source_ref == "doc:42"
    assert rec.span == "120-180"
    assert rec.value == value
    assert rec.created_at  # ISO-8601 UTC timestamp
    assert rec.created_at.endswith("+00:00")


@pytest.mark.parametrize("kind", ["document", "tool", "user"])
def test_every_allowed_source_kind(store: FactStore, kind: str) -> None:
    tid = store.record(kind, "ref", "v")
    rec = store.resolve(tid)
    assert rec is not None and rec.source_kind == kind and rec.span is None


def test_scalar_values_round_trip(store: FactStore) -> None:
    for value in ("text", 7, 0.5, None, False, [], {}):
        rec = store.resolve(store.record("tool", "call-1", value))
        assert rec is not None and rec.value == value


def test_resolve_unknown_returns_none(store: FactStore) -> None:
    assert store.resolve("fact-" + "0" * 32) is None
    assert store.resolve("not-a-trace-id") is None
    assert store.resolve("") is None


# -- validation ---------------------------------------------------------------


@pytest.mark.parametrize("kind", ["web", "Document", "", "tool ", "llm"])
def test_invalid_source_kind_rejected(store: FactStore, kind: str) -> None:
    with pytest.raises(ValueError, match="source_kind"):
        store.record(kind, "ref", "v")
    assert store.count() == 0


def test_empty_source_ref_rejected(store: FactStore) -> None:
    with pytest.raises(ValueError, match="source_ref"):
        store.record("tool", "", "v")


def test_non_json_value_rejected(store: FactStore) -> None:
    with pytest.raises(ValueError, match="JSON"):
        store.record("tool", "ref", {"x": object()})
    with pytest.raises(ValueError, match="JSON"):
        store.record("tool", "ref", float("nan"))
    assert store.count() == 0


# -- ids -------------------------------------------------------------------------


def test_ids_unique_and_stable(store: FactStore) -> None:
    ids = [store.record("tool", f"call-{i}", i) for i in range(1000)]
    assert len(set(ids)) == 1000
    for i, tid in enumerate(ids):
        rec = store.resolve(tid)
        assert rec is not None and rec.value == i and rec.source_ref == f"call-{i}"
        assert store.resolve(tid) == rec  # same ID, same content, every time


def test_ids_are_opaque_not_sequential(store: FactStore) -> None:
    """IDs carry 128 random-looking bits: no counter, no timestamp, no content hash."""
    a = store.record("user", "turn-1", "same")
    b = store.record("user", "turn-1", "same")
    assert a != b  # identical content still gets distinct provenance records
    for tid in (a, b):
        assert tid.startswith("fact-")
        body = tid.removeprefix("fact-")
        assert len(body) == 32 and int(body, 16) >= 0
    # Consecutive IDs share no long common prefix (not a counter or time-ordered ULID).
    ids = [store.record("tool", "r", 0).removeprefix("fact-") for _ in range(50)]
    shared = [len(_common_prefix(x, y)) for x, y in zip(ids, ids[1:])]
    assert max(shared) < 8


def _common_prefix(x: str, y: str) -> str:
    n = 0
    while n < min(len(x), len(y)) and x[n] == y[n]:
        n += 1
    return x[:n]


# -- persistence and schema -------------------------------------------------------


def test_persists_across_instances(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with FactStore(state) as first:
        tid = first.record("document", "doc:1", {"k": "v"}, span="/a/0")
    assert (state / FACTS_DB_FILENAME).is_file()
    with FactStore(state) as second:
        rec = second.resolve(tid)
    assert rec is not None and rec.value == {"k": "v"} and rec.span == "/a/0"


def test_wal_and_schema_version(tmp_path: Path) -> None:
    state = tmp_path / "state"
    FactStore(state).close()
    conn = sqlite3.connect(state / FACTS_DB_FILENAME)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_newer_schema_refused(tmp_path: Path) -> None:
    """A database written by a newer kernel is refused, never silently downgraded."""
    state = tmp_path / "state"
    FactStore(state).close()
    conn = sqlite3.connect(state / FACTS_DB_FILENAME)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(FactStoreError, match="schema"):
        FactStore(state)


def test_reopen_does_not_bump_or_reset_version(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with FactStore(state) as s:
        tid = s.record("tool", "r", 1)
    with FactStore(state) as s:
        assert s.resolve(tid) is not None
        assert s.schema_version() == SCHEMA_VERSION


# -- concurrency --------------------------------------------------------------


def test_concurrent_threads_shared_instance(store: FactStore) -> None:
    n_threads, per_thread = 8, 100
    results: list[list[str]] = [[] for _ in range(n_threads)]
    errors: list[BaseException] = []
    barrier = threading.Barrier(n_threads)

    def work(idx: int) -> None:
        try:
            barrier.wait()
            for i in range(per_thread):
                results[idx].append(store.record("tool", f"t{idx}", [idx, i]))
                store.resolve(results[idx][-1])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    ids = [tid for chunk in results for tid in chunk]
    assert len(set(ids)) == n_threads * per_thread
    assert store.count() == n_threads * per_thread
    for idx, chunk in enumerate(results):
        for i, tid in enumerate(chunk):
            rec = store.resolve(tid)
            assert rec is not None and rec.value == [idx, i]


def test_concurrent_threads_separate_instances(tmp_path: Path) -> None:
    """Each thread its own FactStore (own connection) on one file: SQLite locking."""
    state = tmp_path / "state"
    n_threads, per_thread = 6, 80
    ids: list[str] = []
    ids_lock = threading.Lock()
    errors: list[BaseException] = []
    barrier = threading.Barrier(n_threads)

    def work(idx: int) -> None:
        try:
            with FactStore(state) as s:
                barrier.wait(timeout=60)
                for i in range(per_thread):
                    tid = s.record("user", f"w{idx}", {"i": i})
                    with ids_lock:
                        ids.append(tid)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            barrier.abort()  # never leave the other threads parked on the barrier

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    with FactStore(state) as s:
        assert s.count() == n_threads * per_thread
        assert all(s.resolve(tid) is not None for tid in ids)
    _assert_integrity(state)


_WRITER = """
import json, sys
from pathlib import Path
from nanobot.kernel.facts import FactStore
state, tag, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
with FactStore(state) as s:
    ids = [s.record("tool", tag, {"tag": tag, "i": i}) for i in range(n)]
print(json.dumps(ids))
"""


def test_concurrent_processes(tmp_path: Path) -> None:
    """Separate OS processes writing one database lose no rows and corrupt nothing."""
    state = tmp_path / "state"
    n_procs, per_proc = 4, 150
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(state), f"p{i}", str(per_proc)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for i in range(n_procs)
    ]
    all_ids: list[str] = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        all_ids.extend(json.loads(out.strip().splitlines()[-1]))
    assert len(set(all_ids)) == n_procs * per_proc
    with FactStore(state) as s:
        assert s.count() == n_procs * per_proc
        for tid in all_ids:
            rec = s.resolve(tid)
            assert rec is not None and rec.source_kind == "tool"
    _assert_integrity(state)


def _assert_integrity(state: Path) -> None:
    conn = sqlite3.connect(state / FACTS_DB_FILENAME)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


# -- trace ------------------------------------------------------------------------


def test_record_emits_trace_event(tmp_path: Path) -> None:
    sink = RecordingSink()
    with FactStore(tmp_path / "state", trace=sink) as s:
        tid = s.record("document", "doc:9", {"secret-ish": "value"}, span="0-4")
    assert sink.events == [
        {
            "event": "fact.recorded",
            "trace_id": tid,
            "source_kind": "document",
            "source_ref": "doc:9",
            "span": "0-4",
        }
    ]
    # The value itself is never put on the trace (size, and it may be user data).
    assert "value" not in sink.events[0]


def test_rejected_record_emits_nothing(tmp_path: Path) -> None:
    sink = RecordingSink()
    with FactStore(tmp_path / "state", trace=sink) as s, pytest.raises(ValueError):
        s.record("web", "x", 1)
    assert sink.events == []


def test_failing_sink_does_not_break_record(tmp_path: Path) -> None:
    class Boom:
        def emit(self, event: dict[str, Any]) -> None:
            raise RuntimeError("sink down")

    with FactStore(tmp_path / "state", trace=Boom()) as s:
        tid = s.record("user", "turn-3", "yes")
        assert s.resolve(tid) is not None


# -- host wiring ------------------------------------------------------------------


def _env(paths: Paths, sink: RecordingSink) -> CoreEnvironment:
    return CoreEnvironment(
        config=_EmptyConfig(), credentials=StaticCredentialResolver({}), paths=paths, trace=sink,
    )


def test_from_env_uses_state_dir_and_env_trace(tmp_path: Path) -> None:
    paths = Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state")
    sink = RecordingSink()
    with FactStore.from_env(_env(paths, sink)) as s:
        assert s.path == paths.state_dir / FACTS_DB_FILENAME
        s.record("user", "turn-1", "hi")
    assert [e["event"] for e in sink.events] == ["fact.recorded"]
    assert not (paths.work_dir / FACTS_DB_FILENAME).exists()


# -- file floor -------------------------------------------------------------------


def _assert_floor_protects(floor: ProtectedFloor, path: Path) -> None:
    for cap in ("fs.write", "fs.read"):
        deny = check_floors(AGENT, CapabilityRequest(cap, str(path)), protected=floor)
        assert deny is not None, (cap, path)
        assert deny.marker == PROTECTED_MARKER


def _db_files(state: Path) -> list[Path]:
    db = state / FACTS_DB_FILENAME
    return [db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")]


def test_db_protected_in_split_layout(tmp_path: Path) -> None:
    paths = Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state")
    for f in _db_files(paths.state_dir):
        _assert_floor_protects(ProtectedFloor.from_paths(paths), f)


def test_db_protected_in_legacy_flat_layout(tmp_path: Path) -> None:
    """Flat layout: state_dir is the workspace, so the db and its sidecars are named."""
    ws = tmp_path / "ws"
    paths = Paths(work_dir=ws, state_dir=ws, overlap_ok=True)
    for f in _db_files(ws):
        _assert_floor_protects(ProtectedFloor.from_paths(paths), f)
        _assert_floor_protects(ProtectedFloor(data_dir=None, workspace=ws), f)
    assert ProtectedFloor.from_paths(paths).reason(ws / "facts.txt", write=True) is None
