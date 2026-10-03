"""The capability index: sync, its invariant, and the SQL filter (spec 011 FR-004, FR-007).

Property tests use seeded random loops (no hypothesis dependency): >= 500 examples each
for the soundness invariant (SQL filter == a straightforward Python evaluation of rules
1-4) and the incremental-sync invariant (any sync sequence == one fresh sync).
"""

from __future__ import annotations

import random
import sqlite3

import pytest

from nanobot.capabilities.index import CapabilityEntry, CapabilityIndex, SyncResult
from nanobot.capabilities.manifest import EFFECTS, IOSpec
from nanobot.capabilities.types import UnknownTypeError, load_vocabulary, satisfies

TYPES = [t for t in load_vocabulary() if t != "none"]
OUTPUTS = [*TYPES, "none"]


def entry(name, io=None, *, kind="skill", sha=None, available=True, description=None):
    return CapabilityEntry(
        kind=kind, name=name, description=description or f"{name} description", io=io,
        content_sha256=sha or f"sha-{name}", available=available,
    )


def random_io(rng: random.Random) -> IOSpec | None:
    if rng.random() < 0.2:
        return None
    return IOSpec(
        inputs_all=tuple(sorted(rng.sample(TYPES, rng.choice((0, 0, 1, 2))))),
        inputs_any=tuple(sorted(rng.sample(TYPES, rng.choice((0, 0, 1, 2, 3))))),
        outputs=tuple(sorted(rng.sample(OUTPUTS, rng.choice((0, 1, 1, 2))))),
        effects=tuple(sorted(rng.sample(EFFECTS, rng.choice((0, 1, 1, 2, 3))))),
    )


def random_entries(rng: random.Random, pool: int = 12) -> list[CapabilityEntry]:
    names = rng.sample([f"cap-{i}" for i in range(pool)], rng.randint(0, pool))
    out = []
    for name in names:
        kind = rng.choice(("skill", "tool"))
        version = rng.randint(0, 2)
        out.append(entry(
            name, random_io(random.Random(f"{name}-{version}")), kind=kind,
            sha=f"{name}-{kind}-{version}", available=rng.random() < 0.8,
            description=f"{name} v{version}",
        ))
    return out


def reference_filter(entries, task_types, requested, allowed_effects, kinds=None):
    """FR-007 rules 1-4 evaluated directly in Python."""
    allowed = set(allowed_effects)
    survivors = set()
    for item in entries:
        if kinds is not None and item.kind not in kinds:
            continue
        if not (item.available or item.kind == "skill"):
            continue
        io = item.io
        if io is not None:
            if any(e not in allowed or (e == "sudo" and "exec" not in allowed) for e in io.effects):
                continue
            if not all(any(satisfies(t, r) for t in task_types) for r in io.inputs_all):
                continue
            if io.inputs_any and not any(
                satisfies(t, r) for r in io.inputs_any for t in task_types
            ):
                continue
            if requested is not None and not any(
                o == "any" or satisfies(o, requested) or satisfies(requested, o)
                for o in io.outputs
            ):
                continue
        survivors.add((item.kind, item.name))
    return survivors


def snapshot(index: CapabilityIndex):
    return index.entries(), index.child_rows()


# -- sync ------------------------------------------------------------------------------


def test_sync_counts_and_untouched_rows(tmp_path):
    index = CapabilityIndex(tmp_path / "data" / "capabilities.db")
    a, b, c = entry("a", IOSpec(("file:pdf",))), entry("b"), entry("c", kind="tool")
    assert index.sync([a, b, c]) == SyncResult(added=3, updated=0, removed=0, unchanged=0)
    assert index.sync([a, b, c]) == SyncResult(0, 0, 0, 3)
    b2 = entry("b", sha="new-sha")
    c2 = entry("c", kind="tool", available=False)
    assert index.sync([a, b2, c2]) == SyncResult(added=0, updated=2, removed=0, unchanged=1)
    assert index.sync([b2]) == SyncResult(added=0, updated=0, removed=2, unchanged=1)
    assert [e.name for e in index.entries()] == ["b"]
    assert len(index) == 1


def test_unchanged_rows_keep_their_rowid(tmp_path):
    path = tmp_path / "capabilities.db"
    index = CapabilityIndex(path)
    index.sync([entry("a"), entry("b")])
    before = dict(sqlite3.connect(path).execute("SELECT name, id FROM capabilities"))
    index.sync([entry("a"), entry("b", sha="changed"), entry("c")])
    after = dict(sqlite3.connect(path).execute("SELECT name, id FROM capabilities"))
    assert after["a"] == before["a"]


def test_skill_and_tool_with_the_same_name_coexist():
    index = CapabilityIndex(":memory:")
    index.sync([entry("my"), entry("my", kind="tool")])
    assert {(e.kind, e.name) for e in index.entries()} == {("skill", "my"), ("tool", "my")}


def test_database_uses_wal_and_persists(tmp_path):
    path = tmp_path / "state" / "data" / "capabilities.db"
    index = CapabilityIndex(path)
    index.sync([entry("a", IOSpec(("url/git",), (), ("text/markdown",), ("network",)))])
    index.close()
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    reopened = CapabilityIndex(path)
    [item] = reopened.entries()
    assert item.io == IOSpec(("url/git",), (), ("text/markdown",), ("network",))


def test_raw_io_dict_is_normalised():
    item = entry("a", {"inputs": ["file:pdf"]})
    assert item.io == IOSpec(("file:pdf",))


def test_entry_rejects_unknown_kind():
    with pytest.raises(ValueError):
        entry("a", kind="prompt")


def test_incremental_sync_equals_fresh_sync_property():
    """SC-003: >= 500 random sync sequences; the index always equals a fresh one."""
    rng = random.Random(20261003)
    for example in range(500):
        incremental = CapabilityIndex(":memory:")
        lists = [random_entries(rng) for _ in range(rng.randint(1, 5))]
        for entries in lists:
            incremental.sync(entries)
        fresh = CapabilityIndex(":memory:")
        fresh.sync(lists[-1])
        assert snapshot(incremental) == snapshot(fresh), example
        # A no-op resync reports everything unchanged.
        assert incremental.sync(lists[-1]).unchanged == len(
            {(e.kind, e.name) for e in lists[-1]}
        )


# -- filter ----------------------------------------------------------------------------


def _names(rows):
    return {(row.kind, row.name) for row in rows}


def test_filter_rules_by_example():
    index = CapabilityIndex(":memory:")
    items = [
        entry("pdf-sum", IOSpec(("file:pdf",), (), ("text/markdown",), ("read-workspace",))),
        entry("git", IOSpec(("url/git",), (), ("text/markdown",), ("network",))),
        entry("any-of", IOSpec((), ("file:pdf", "file:office"), ("any",), ("none",))),
        entry("needs-sudo", IOSpec((), (), ("none",), ("sudo",))),
        entry("untyped"),
        entry("down-tool", IOSpec(), kind="tool", available=False),
        entry("down-skill", IOSpec((), (), ("text",), ("none",)), available=False),
        entry("files", IOSpec(("file",), (), ("data",), ("read-workspace",))),
    ]
    index.sync(items)
    task = {"text/query", "workspace", "file:pdf"}
    got = _names(index.filter(task, "text/markdown", EFFECTS))
    assert ("skill", "pdf-sum") in got
    assert ("skill", "git") not in got  # needs url/git
    assert ("skill", "any-of") in got
    assert ("skill", "untyped") in got
    assert ("tool", "down-tool") not in got  # unavailable tool
    assert ("skill", "down-skill") in got  # unavailable skills keep being listed
    assert ("skill", "needs-sudo") not in got  # output none is not comparable
    assert ("skill", "files") not in got  # data is not comparable with text/markdown
    no_sudo = _names(index.filter(task, None, [e for e in EFFECTS if e != "exec"]))
    assert ("skill", "needs-sudo") not in no_sudo  # sudo also needs exec
    assert ("skill", "needs-sudo") in _names(index.filter(task, None, EFFECTS))
    assert ("skill", "files") in _names(index.filter(task, None, EFFECTS))
    only_tools = _names(index.filter(task, None, EFFECTS, kinds=["tool"]))
    assert only_tools == set()


def test_output_any_and_general_outputs_match_a_specific_request():
    index = CapabilityIndex(":memory:")
    index.sync([
        entry("anything", IOSpec(outputs=("any",), effects=("none",))),
        entry("texty", IOSpec(outputs=("text",), effects=("none",))),
        entry("python", IOSpec(outputs=("text/code:python",), effects=("none",))),
        entry("csv", IOSpec(outputs=("data/csv",), effects=("none",))),
    ])
    got = {row.name for row in index.filter({"text/query"}, "text/code", EFFECTS)}
    assert got == {"anything", "texty", "python"}


def test_filter_rejects_unknown_types():
    index = CapabilityIndex(":memory:")
    with pytest.raises(UnknownTypeError):
        index.filter({"bogus"}, None, EFFECTS)
    with pytest.raises(UnknownTypeError):
        index.filter({"text/query"}, "bogus", EFFECTS)


def test_sql_filter_matches_python_reference_property():
    """SC-003: >= 500 random libraries and tasks; SQL survivors == the Python rules."""
    rng = random.Random(1103)
    index = CapabilityIndex(":memory:")
    for example in range(600):
        if example % 20 == 0:
            entries = random_entries(rng, pool=40)
            index.sync(entries)
        task = set(rng.sample(TYPES, rng.randint(1, 6))) | {"text/query"}
        requested = rng.choice([None, None, *OUTPUTS])
        allowed = rng.sample(EFFECTS, rng.randint(0, len(EFFECTS)))
        kinds = rng.choice([None, ["skill"], ["tool"]])
        expected = reference_filter(entries, task, requested, allowed, kinds)
        assert _names(index.filter(task, requested, allowed, kinds=kinds)) == expected, (
            example, task, requested, allowed,
        )
