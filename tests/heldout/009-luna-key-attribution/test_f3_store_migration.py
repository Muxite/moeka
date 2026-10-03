"""FR-003 / SC-003 / NFR-002: ``llm_calls.key_fp`` persisted; old stores migrate idempotently."""

from __future__ import annotations

import sqlite3

import pytest
from _h009 import (
    DB_NAME,
    KEY_AWORK,
    KEY_MOEKA,
    build_old_store,
    columns,
    record,
    ref_fp,
)

from nanobot.llm_usage import query
from nanobot.llm_usage.store import LLMUsageStore

FP1, FP2 = ref_fp(KEY_MOEKA), ref_fp(KEY_AWORK)


def _raw(path, sql, params=()):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _open(path) -> LLMUsageStore:
    s = LLMUsageStore(path)
    s.count()  # forces connect + migration
    return s


@pytest.mark.fr("FR-003")
def test_fresh_store_has_nullable_text_column(store):
    store.count()
    info = {c[1]: c for c in columns(store.path)}
    assert "key_fp" in info
    assert info["key_fp"][2].upper() == "TEXT"
    assert info["key_fp"][3] == 0  # nullable


@pytest.mark.fr("FR-003")
def test_record_writes_fp(store):
    store.record(record(key_fp=FP1))
    store.record(record(key_fp=None))
    store.close()
    got = sorted(_raw(store.path, "SELECT key_fp FROM llm_calls"), key=lambda r: str(r[0]))
    assert got == sorted([(FP1,), (None,)], key=lambda r: str(r[0]))


@pytest.mark.fr("FR-003", "FR-005")
def test_record_round_trips_through_read_side(store):
    store.record(record(key_fp=FP2))
    rows = [r for r in query.records(store) if r["kind"] == "model_call"]
    assert [r["key_fp"] for r in rows] == [FP2]


@pytest.mark.fr("FR-003", "FR-002")
def test_record_event_path_writes_fp(store):
    """The ledger path (``record_event`` via ``LedgerObserver``) persists it too."""
    from nanobot.kernel.ledger import LedgerObserver

    obs = LedgerObserver(store=store, pricing={})
    obs(record(key_fp=FP1))
    store.close()
    assert _raw(store.path, "SELECT key_fp FROM llm_calls") == [(FP1,)]


@pytest.mark.fr("FR-003", "SC-003")
@pytest.mark.parametrize("version", [4, 1])
def test_old_store_gains_column_and_old_rows_read_none(tmp_path, version):
    path = tmp_path / "data" / DB_NAME
    stamps = build_old_store(path, version=version, rows=3)
    assert "key_fp" not in {c[1] for c in columns(path)}
    s = _open(path)
    try:
        info = {c[1]: c for c in columns(path)}
        assert "key_fp" in info and info["key_fp"][3] == 0
        assert _raw(path, "SELECT COUNT(*), COUNT(key_fp) FROM llm_calls") == [(3, 0)]
        rows = [r for r in query.records(s, limit=50) if r["kind"] == "model_call"]
        assert len(rows) == 3
        assert all("key_fp" in r and r["key_fp"] is None for r in rows)
        assert sorted(r["started_at_ms"] for r in rows) == sorted(stamps)
    finally:
        s.close()


@pytest.mark.fr("FR-003", "SC-003")
def test_old_store_accepts_new_keyed_rows_after_migration(tmp_path):
    path = tmp_path / "data" / DB_NAME
    build_old_store(path, version=4, rows=2)
    s = _open(path)
    try:
        s.record(record(key_fp=FP1, cost_usd=0.5))
        by = {t.group["key_fp"]: t for t in query.totals(s, group_by=("key_fp",))}
        assert set(by) == {None, FP1}
        assert by[None].requests == 2
        assert by[FP1].requests == 1
    finally:
        s.close()


@pytest.mark.fr("FR-003", "NFR-002")
def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "data" / DB_NAME
    build_old_store(path, version=4, rows=2)
    s1 = _open(path)
    s1.close()
    after_first = columns(path)
    version_first = _raw(path, "PRAGMA user_version")[0][0]
    s2 = _open(path)
    s2.close()
    s3 = _open(path)
    s3.close()
    assert columns(path) == after_first
    assert [c[1] for c in after_first].count("key_fp") == 1
    assert _raw(path, "PRAGMA user_version")[0][0] == version_first
    assert _raw(path, "SELECT COUNT(*) FROM llm_calls")[0][0] == 2


@pytest.mark.fr("FR-003", "NFR-002")
def test_fresh_store_reopen_adds_nothing(tmp_path):
    path = tmp_path / "data" / DB_NAME
    s = _open(path)
    s.record(record(key_fp=FP1))
    s.close()
    before = columns(path)
    s = _open(path)
    s.close()
    assert columns(path) == before
    assert _raw(path, "SELECT key_fp FROM llm_calls") == [(FP1,)]


@pytest.mark.fr("FR-003")
def test_get_llm_usage_store_with_data_dir(tmp_path):
    from nanobot.llm_usage import close_llm_usage_store, get_llm_usage_store

    data_dir = tmp_path / "inst"
    s = get_llm_usage_store(data_dir=data_dir)
    try:
        s.record(record(key_fp=FP2))
        rows = [r for r in query.records(s) if r["kind"] == "model_call"]
        assert rows[0]["key_fp"] == FP2
    finally:
        close_llm_usage_store(data_dir=data_dir)
    assert _raw(data_dir / DB_NAME, "SELECT key_fp FROM llm_calls") == [(FP2,)]


@pytest.mark.fr("FR-003", "NFR-002")
def test_concurrent_open_of_old_store_converges(tmp_path):
    """Two stores opening the same old file both work; the column exists once."""
    path = tmp_path / "data" / DB_NAME
    build_old_store(path, version=4, rows=1)
    a, b = LLMUsageStore(path), LLMUsageStore(path)
    try:
        a.count()
        b.count()
        a.record(record(key_fp=FP1))
        b.record(record(key_fp=FP2))
    finally:
        a.close()
        b.close()
    assert [c[1] for c in columns(path)].count("key_fp") == 1
    assert sorted(_raw(path, "SELECT key_fp FROM llm_calls WHERE key_fp IS NOT NULL")) == sorted(
        [(FP1,), (FP2,)])
