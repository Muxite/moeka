"""Part B: index provenance (FR-007): ``cert_id`` / ``cert_status`` stored per capability and
the certified boost in the default ranker."""

from __future__ import annotations

import pytest
from _h012 import (
    Signer,
    agent_of,
    caps_for,
    cap,
    certify,
    trust_dir,
    write_skill,
)


def _entry(name, description, *, status, cert_id=None, kind="skill"):
    idx = cap("index")
    return idx.CapabilityEntry(
        kind=kind, name=name, description=description, io=None,
        content_sha256=(name.encode().hex() * 8)[:64].ljust(64, "0"), available=True,
        cert_id=cert_id, cert_status=status,
    )


def _select(index, query, k=1):
    from nanobot.capabilities.select import select
    from nanobot.capabilities.task_types import TaskTyping

    return select(index, TaskTyping(types=frozenset(), requested_output=None), query, k=k,
                  kinds=("skill",))


STATUSES = ["certified", "uncertified", "refused:bad-signature", "refused:no-cert", "builtin"]


@pytest.mark.fr("FR-007")
def test_entries_round_trip_cert_fields(tmp_path):
    idx = cap("index").CapabilityIndex(tmp_path / "caps.db")
    try:
        entries = [_entry(f"h012-s{i}", "d", status=s,
                          cert_id=("0123456789abcdef" if s == "certified" else None))
                   for i, s in enumerate(STATUSES)]
        idx.sync(entries)
        got = {e.name: (e.cert_id, e.cert_status) for e in idx.entries()}
        assert got == {e.name: (e.cert_id, e.cert_status) for e in entries}
    finally:
        idx.close()


@pytest.mark.fr("FR-007")
def test_cert_fields_persist_across_instances(tmp_path):
    path = tmp_path / "caps.db"
    idx = cap("index").CapabilityIndex(path)
    idx.sync([_entry("h012-p", "d", status="certified", cert_id="fedcba9876543210")])
    idx.close()
    idx2 = cap("index").CapabilityIndex(path)
    try:
        [e] = idx2.entries()
        assert (e.cert_id, e.cert_status) == ("fedcba9876543210", "certified")
    finally:
        idx2.close()


@pytest.mark.fr("FR-007")
def test_certified_wins_a_tie_under_default_ranker():
    idx = cap("index").CapabilityIndex(":memory:")
    try:
        idx.sync([
            _entry("aaa-plain", "pdf report digest", status="uncertified"),
            _entry("zzz-signed", "pdf report digest", status="certified",
                   cert_id="0123456789abcdef"),
            _entry("mmm-other", "unrelated words", status="uncertified"),
        ])
        sel = _select(idx, "pdf report digest", k=1)
        assert sel.fallback is None
        assert sel.survivors[0] == "zzz-signed"
        assert sel.selected == ("zzz-signed",)
    finally:
        idx.close()


@pytest.mark.fr("FR-007")
def test_without_certified_ties_break_by_name():
    idx = cap("index").CapabilityIndex(":memory:")
    try:
        idx.sync([
            _entry("aaa-plain", "pdf report digest", status="uncertified"),
            _entry("zzz-plain", "pdf report digest", status="uncertified"),
            _entry("mmm-other", "unrelated words", status="uncertified"),
        ])
        assert _select(idx, "pdf report digest").survivors[0] == "aaa-plain"
    finally:
        idx.close()


@pytest.mark.fr("FR-007")
@pytest.mark.parametrize("status", ["builtin", "refused:bad-signature", "uncertified"])
def test_only_certified_is_boosted(status):
    idx = cap("index").CapabilityIndex(":memory:")
    try:
        idx.sync([
            _entry("aaa-plain", "pdf report digest", status="uncertified"),
            _entry("zzz-other", "pdf report digest", status=status),
            _entry("mmm-other", "unrelated words", status="uncertified"),
        ])
        assert _select(idx, "pdf report digest").survivors[0] == "aaa-plain"
    finally:
        idx.close()


@pytest.mark.fr("FR-007")
def test_boost_is_multiplicative_not_dominant():
    """Lexical scores 1.76 (uncertified) vs 0.5 (certified): x1.10 does not reorder them, and a
    certified capability scoring 0 stays below every positive score."""
    idx = cap("index").CapabilityIndex(":memory:")
    try:
        idx.sync([
            _entry("aaa-strong", "pdf report digest", status="uncertified"),
            _entry("zzz-weak", "pdf", status="certified", cert_id="0123456789abcdef"),
            _entry("bbb-zero", "nothing shared", status="certified", cert_id="1123456789abcdef"),
        ])
        sel = _select(idx, "pdf report digest", k=1)
        assert list(sel.survivors) == ["aaa-strong", "zzz-weak", "bbb-zero"]
    finally:
        idx.close()


@pytest.mark.fr("FR-007")
def test_kernel_index_records_cert_status(kmaker, tmp_path):
    alpha = Signer("alpha")
    work = tmp_path / "work"
    tdir = trust_dir(tmp_path / "trust", alpha)
    cid = certify(write_skill(work, "h012-ix-cert"), alpha)
    write_skill(work, "h012-ix-plain")
    caps = caps_for(False, tdir, selection="filtered", top_k=3)
    kernel, _ = kmaker(work=work, caps=caps)
    agent_of(kernel).run_sync("Please summarise ~/docs/report.pdf as markdown")
    db = kernel.env.paths.data_dir / "capabilities.db"
    assert db.exists()
    idx = cap("index").CapabilityIndex(db)
    try:
        by_name = {(e.kind, e.name): e for e in idx.entries()}
    finally:
        idx.close()
    cert = by_name[("skill", "h012-ix-cert")]
    assert (cert.cert_status, cert.cert_id) == ("certified", cid)
    plain = by_name[("skill", "h012-ix-plain")]
    assert (plain.cert_status, plain.cert_id) == ("uncertified", None)
    others = [e for (_k, name), e in by_name.items()
              if name not in ("h012-ix-cert", "h012-ix-plain")]
    assert others, "built-in skills (and tools) are indexed too"
    assert all(e.cert_status == "builtin" and e.cert_id is None for e in others)
