"""Part B: the SQLite capability index (FR-004, SC-003 incremental-sync invariant)."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3

import pytest
from _h011 import (
    EFFECTS,
    INPUT_TYPES,
    TYPES,
    cap,
    close_index,
    const_ranker,
    entry,
    open_index,
    ref_sync_counts,
    run_select,
    sync_tuple,
)

PROPERTY_EXAMPLES = 500


def _lib():
    return [
        entry("skill", "pdf-sum", "Summarise PDFs", {"inputs": ["file:pdf"],
                                                     "outputs": ["text/markdown"],
                                                     "effects": ["read-workspace"]}),
        entry("skill", "untyped-one", "No io block"),
        entry("tool", "exec", "Run shell commands", {"effects": ["exec"]}),
        entry("tool", "web_fetch", "Fetch a URL", {"inputs": ["url/web"], "effects": ["network"]},
              available=False),
    ]


@pytest.fixture
def index(tmp_path):
    idx = open_index(tmp_path / "caps.db")
    yield idx
    close_index(idx)


@pytest.mark.fr("FR-004")
def test_entry_and_result_types_exist():
    idx = cap("index")
    e = entry("skill", "x", "d")
    assert isinstance(e, idx.CapabilityEntry)
    assert (e.kind, e.name, e.description, e.io, e.available) == ("skill", "x", "d", None, True)
    assert len(e.content_sha256) == 64


@pytest.mark.fr("FR-004")
def test_first_sync_adds_everything(index):
    result = index.sync(_lib())
    assert isinstance(result, cap("index").SyncResult)
    assert sync_tuple(result) == (4, 0, 0, 0)


@pytest.mark.fr("FR-004")
def test_resync_same_list_is_all_unchanged(index):
    index.sync(_lib())
    assert sync_tuple(index.sync(_lib())) == (0, 0, 0, 4)


@pytest.mark.fr("FR-004")
def test_sha_change_is_an_update(index):
    lib = _lib()
    index.sync(lib)
    lib[0] = entry("skill", "pdf-sum", "Summarise PDFs (v2)", {"inputs": ["file:pdf"]})
    assert sync_tuple(index.sync(lib)) == (0, 1, 0, 3)


@pytest.mark.fr("FR-004")
def test_availability_change_is_an_update(index):
    lib = _lib()
    index.sync(lib)
    old = lib[3]
    lib[3] = entry("tool", "web_fetch", "Fetch a URL", {"inputs": ["url/web"],
                                                         "effects": ["network"]},
                   available=True, sha=old.content_sha256)
    assert sync_tuple(index.sync(lib)) == (0, 1, 0, 3)


@pytest.mark.fr("FR-004")
def test_removed_and_added(index):
    lib = _lib()
    index.sync(lib)
    new = lib[1:] + [entry("skill", "fresh", "new one")]
    assert sync_tuple(index.sync(new)) == (1, 0, 1, 3)
    assert sync_tuple(index.sync([])) == (0, 0, 4, 0)
    assert sync_tuple(index.sync(lib)) == (4, 0, 0, 0)


@pytest.mark.fr("FR-004")
def test_kind_is_part_of_identity(index):
    both = [entry("skill", "same", "skill one"), entry("tool", "same", "tool one")]
    assert sync_tuple(index.sync(both)) == (2, 0, 0, 0)
    assert sync_tuple(index.sync(both[:1])) == (0, 0, 1, 1)


@pytest.mark.fr("FR-004")
def test_index_persists_across_instances(tmp_path):
    path = tmp_path / "caps.db"
    first = open_index(path)
    first.sync(_lib())
    close_index(first)
    second = open_index(path)
    try:
        assert sync_tuple(second.sync(_lib())) == (0, 0, 0, 4)
    finally:
        close_index(second)


@pytest.mark.fr("FR-004")
def test_database_uses_wal(tmp_path):
    path = tmp_path / "caps.db"
    idx = open_index(path)
    idx.sync(_lib())
    try:
        con = sqlite3.connect(path)
        try:
            assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            con.close()
    finally:
        close_index(idx)


@pytest.mark.fr("FR-004")
def test_index_reflects_sync_in_selection(index):
    """A removed capability no longer survives; an updated io is the one filtered on."""
    filler = [entry("skill", f"filler-{i}", "filler", {"inputs": ["url/video"]}) for i in range(4)]
    index.sync([entry("skill", "a", "a", {"inputs": ["file:pdf"]}), *filler])
    sel = run_select(index, {"file:pdf"}, k=1, ranker=const_ranker)
    assert set(sel.survivors) == {"a"}
    index.sync([entry("skill", "a", "a v2", {"inputs": ["data/csv"]}), *filler])
    sel = run_select(index, {"file:pdf"}, k=1, ranker=const_ranker)
    assert sel.fallback == "empty" and set(sel.survivors) == set()
    index.sync([entry("skill", "b", "b", {"inputs": ["file:pdf"]}), *filler])
    sel = run_select(index, {"file:pdf"}, k=1, ranker=const_ranker)
    assert set(sel.survivors) == {"b"}


# -- SC-003: incremental sync == fresh sync (property test) ------------------------------------


def _random_io(rng):
    if rng.random() < 0.25:
        return None
    io = {}
    if rng.random() < 0.5:
        io["inputs"] = rng.sample(INPUT_TYPES, rng.randint(0, 2))
    elif rng.random() < 0.7:
        pool = rng.sample(INPUT_TYPES, 4)
        io["inputs"] = {"all": pool[: rng.randint(0, 2)], "any": pool[2: 2 + rng.randint(0, 2)]}
    if rng.random() < 0.7:
        io["outputs"] = rng.sample(TYPES, rng.randint(1, 2))
    if rng.random() < 0.7:
        io["effects"] = rng.sample(EFFECTS, rng.randint(1, 3))
    return io


def _pool(rng):
    """(kind, name) -> list of 2-3 content versions (description, io)."""
    pool = {}
    for i in range(rng.randint(3, 9)):
        kind = rng.choice(["skill", "tool"])
        name = f"cap-{i}" if rng.random() < 0.85 else "shared-name"
        pool[(kind, name)] = [(f"{name} v{v} " + rng.choice(["pdf", "csv", "web", "git", "log"]),
                               _random_io(rng)) for v in range(rng.randint(2, 3))]
    return pool


def _draw(rng, pool):
    keys = [k for k in pool if rng.random() < 0.7]
    rng.shuffle(keys)
    out = []
    for kind, name in keys:
        desc, io = rng.choice(pool[(kind, name)])
        avail = rng.random() < 0.8
        sha = hashlib.sha256(json.dumps([kind, name, desc, io], sort_keys=True).encode()).hexdigest()
        out.append(entry(kind, name, desc, io, available=avail, sha=sha))
    return out


def _table_counts(path):
    con = sqlite3.connect(path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        con.close()


def _selections(index, rng_seed):
    rng = random.Random(rng_seed)
    out = []
    for _ in range(4):
        types = set(rng.sample(INPUT_TYPES, rng.randint(0, 4)))
        req = rng.choice([None, *TYPES])
        allowed = set(rng.sample(EFFECTS, rng.randint(2, 6))) | {"none", "exec"}
        for ranker in (const_ranker, None):
            sel = run_select(index, types, req, query="pdf csv web git", allowed=allowed, k=1,
                             ranker=ranker)
            out.append((sel.survivors, sel.selected, sel.fallback))
    return out


@pytest.mark.fr("FR-004", "SC-003")
@pytest.mark.timeout(900)
def test_incremental_sync_equals_fresh_sync_property(tmp_path):
    for example in range(PROPERTY_EXAMPLES):
        rng = random.Random(11_000 + example)
        pool = _pool(rng)
        d = tmp_path / f"ex{example}"
        d.mkdir()
        inc = open_index(d / "inc.db")
        try:
            prev: dict = {}
            last = []
            for _ in range(rng.randint(1, 5)):
                last = _draw(rng, pool)
                result = inc.sync(last)
                assert sync_tuple(result) == ref_sync_counts(prev, last), example
                prev = {(e.kind, e.name): (e.content_sha256, bool(e.available)) for e in last}
            fresh = open_index(d / "fresh.db")
            try:
                assert sync_tuple(fresh.sync(last)) == (len(last), 0, 0, 0), example
                assert _table_counts(d / "inc.db") == _table_counts(d / "fresh.db"), example
                if len(last) >= 2:
                    assert _selections(inc, example) == _selections(fresh, example), example
                assert sync_tuple(inc.sync(last)) == (0, 0, 0, len(last)), example
                assert sync_tuple(fresh.sync(last)) == (0, 0, 0, len(last)), example
            finally:
                close_index(fresh)
        finally:
            close_index(inc)
