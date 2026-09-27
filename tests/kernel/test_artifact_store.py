"""Typed ArtifactStore enforcing I3 (Task 23, design I3 / sections 7-8).

Headline properties:
- an uncited leaf is stored as provisional and never reaches ``committed()``;
- every committed leaf carries a trace ID that resolves in the same ``FactStore``;
- a cite whose trace ID resolves to nothing rejects the whole ``propose`` (nothing
  is stored, not even the uncited leaves);
- user confirmation is a ``FactStore.record("user", ...)`` trace ID, not a sentinel;
- ``propose`` accumulates into an artifact; deltas are validated per leaf against
  the registered pydantic model of the artifact's kind;
- the store is SQLite (WAL, busy timeout, ``user_version``) under ``state_dir`` and
  survives reopen, concurrent threads and concurrent processes;
- the database sits behind the file floor in both path layouts.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from nanobot.kernel.artifacts import (
    ARTIFACTS_DB_FILENAME,
    SCHEMA_VERSION,
    ArtifactError,
    ArtifactIncompleteError,
    ArtifactKindMismatchError,
    ArtifactNotFoundError,
    ArtifactResult,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactValidationError,
    CitationError,
    UnknownKindError,
)
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.facts import FactStore
from nanobot.kernel.floors import check_floors
from nanobot.kernel.policy import CapabilityRequest, Principal
from nanobot.security.protected_paths import PROTECTED_MARKER, ProtectedFloor

AGENT = Principal("main", "agent", parent=Principal("host", "host"))
MISSING = "fact-" + "0" * 32


class Address(BaseModel):
    city: str
    zip_code: str | None = None


class Server(BaseModel):
    hostname: str
    cores: int = Field(ge=1)
    tags: list[str] = []
    labels: dict[str, str] = {}
    address: Address
    backup: Address | None = None


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class _EmptyConfig:
    def section(self, name: str) -> dict[str, Any]:
        return {}


@pytest.fixture
def facts(tmp_path: Path):
    f = FactStore(tmp_path / "state")
    yield f
    f.close()


@pytest.fixture
def store(tmp_path: Path, facts: FactStore):
    s = ArtifactStore(tmp_path / "state", facts)
    s.register_kind("server", Server)
    yield s
    s.close()


def _tool_fact(facts: FactStore, value: Any) -> str:
    return facts.record("tool", "call-1", value)


# -- I3 headline ------------------------------------------------------------------


def test_uncited_delta_never_reaches_committed(store: ArtifactStore) -> None:
    res = store.propose("server", {"hostname": "nas", "cores": 4})
    assert isinstance(res, ArtifactResult)
    assert res.committed == {}
    assert set(res.provisional) == {"hostname", "cores"}
    assert store.committed(res.artifact_id) == {}
    assert store.provisional(res.artifact_id) == {"hostname": "nas", "cores": 4}
    assert store.citations(res.artifact_id) == {}


def test_every_committed_leaf_resolves_to_its_trace_id(
    store: ArtifactStore, facts: FactStore,
) -> None:
    t_host = _tool_fact(facts, "nas")
    t_city = facts.record("document", "doc:inv", "Oslo", span="10-14")
    res = store.propose(
        "server",
        {"hostname": "nas", "address": {"city": "Oslo"}},
        {"hostname": t_host, "address.city": t_city},
    )
    assert res.committed == {"hostname": t_host, "address.city": t_city}
    assert res.provisional == ()
    assert store.committed(res.artifact_id) == {"hostname": "nas", "address": {"city": "Oslo"}}
    cites = store.citations(res.artifact_id)
    assert cites == {"hostname": t_host, "address.city": t_city}
    # Independently resolvable through the same FactStore.
    for path, tid in cites.items():
        rec = facts.resolve(tid)
        assert rec is not None, path
    assert facts.resolve(cites["address.city"]).source_kind == "document"


def test_mixed_delta_splits_committed_and_provisional(
    store: ArtifactStore, facts: FactStore,
) -> None:
    tid = _tool_fact(facts, "nas")
    res = store.propose("server", {"hostname": "nas", "cores": 8}, {"hostname": tid})
    assert res.committed == {"hostname": tid}
    assert res.provisional == ("cores",)
    assert store.committed(res.artifact_id) == {"hostname": "nas"}
    assert store.provisional(res.artifact_id) == {"cores": 8}


def test_cite_to_missing_fact_rejects_whole_propose(
    store: ArtifactStore, facts: FactStore,
) -> None:
    good = _tool_fact(facts, "nas")
    with pytest.raises(CitationError) as exc:
        store.propose(
            "server",
            {"hostname": "nas", "cores": 2, "tags": ["a"]},
            {"hostname": good, "cores": MISSING},
            artifact_id="srv-1",
        )
    assert "cores" in str(exc.value)
    assert exc.value.paths == ("cores",)
    # Atomic: nothing stored, not the good cite and not the uncited leaf.
    with pytest.raises(ArtifactNotFoundError):
        store.committed("srv-1")


@pytest.mark.parametrize("bad", ["", "not-a-trace-id", MISSING, 42, None])
def test_malformed_or_unknown_cite_rejected(store: ArtifactStore, bad: Any) -> None:
    with pytest.raises(CitationError):
        store.propose("server", {"hostname": "nas"}, {"hostname": bad})


def test_cite_rejection_leaves_existing_artifact_untouched(
    store: ArtifactStore, facts: FactStore,
) -> None:
    tid = _tool_fact(facts, "nas")
    aid = store.propose("server", {"hostname": "nas"}, {"hostname": tid}).artifact_id
    with pytest.raises(CitationError):
        store.propose("server", {"hostname": "evil"}, {"hostname": MISSING}, artifact_id=aid)
    assert store.committed(aid) == {"hostname": "nas"}
    assert store.citations(aid) == {"hostname": tid}
    assert store.provisional(aid) == {}


def test_cite_for_path_not_in_delta_rejected(store: ArtifactStore, facts: FactStore) -> None:
    tid = _tool_fact(facts, "x")
    with pytest.raises(ArtifactValidationError, match="cores"):
        store.propose("server", {"hostname": "nas"}, {"cores": tid})


def test_cite_on_parent_path_does_not_cover_leaves(
    store: ArtifactStore, facts: FactStore,
) -> None:
    """Cites are per leaf: a cite on "address" is not a leaf of this delta."""
    tid = _tool_fact(facts, {"city": "Oslo"})
    with pytest.raises(ArtifactValidationError, match="address"):
        store.propose("server", {"address": {"city": "Oslo"}}, {"address": tid})


# -- user confirmation --------------------------------------------------------------


def test_user_confirmation_is_a_user_fact(store: ArtifactStore, facts: FactStore) -> None:
    """The user's answer is recorded with user provenance first; its trace ID commits."""
    tid = facts.record("user", "session:tg:1/turn-7", "backup.example")
    res = store.propose("server", {"hostname": "backup.example"}, {"hostname": tid})
    assert res.committed == {"hostname": tid}
    rec = facts.resolve(store.citations(res.artifact_id)["hostname"])
    assert rec is not None and rec.source_kind == "user"


@pytest.mark.parametrize("sentinel", ["user", "USER", "confirmed"])
def test_no_magic_user_sentinel(store: ArtifactStore, sentinel: str) -> None:
    with pytest.raises(CitationError):
        store.propose("server", {"hostname": "nas"}, {"hostname": sentinel})


# -- accumulation -------------------------------------------------------------------


def test_propose_accumulates(store: ArtifactStore, facts: FactStore) -> None:
    t1, t2 = _tool_fact(facts, "nas"), _tool_fact(facts, 4)
    aid = store.propose("server", {"hostname": "nas"}, {"hostname": t1}).artifact_id
    res = store.propose("server", {"cores": 4}, {"cores": t2}, artifact_id=aid)
    assert res.artifact_id == aid
    assert store.committed(aid) == {"hostname": "nas", "cores": 4}
    assert store.citations(aid) == {"hostname": t1, "cores": t2}


def test_nested_leaves_accumulate(store: ArtifactStore, facts: FactStore) -> None:
    t1, t2 = _tool_fact(facts, "Oslo"), _tool_fact(facts, "0150")
    aid = store.propose("server", {"address": {"city": "Oslo"}}, {"address.city": t1}).artifact_id
    store.propose("server", {"address": {"zip_code": "0150"}}, {"address.zip_code": t2},
                  artifact_id=aid)
    assert store.committed(aid) == {"address": {"city": "Oslo", "zip_code": "0150"}}


def test_recommit_replaces_value_and_cite(store: ArtifactStore, facts: FactStore) -> None:
    t1, t2 = _tool_fact(facts, "nas"), _tool_fact(facts, "nas2")
    aid = store.propose("server", {"hostname": "nas"}, {"hostname": t1}).artifact_id
    store.propose("server", {"hostname": "nas2"}, {"hostname": t2}, artifact_id=aid)
    assert store.committed(aid) == {"hostname": "nas2"}
    assert store.citations(aid) == {"hostname": t2}


def test_uncited_update_does_not_overwrite_committed(
    store: ArtifactStore, facts: FactStore,
) -> None:
    """An uncited change to a committed leaf is a pending provisional edit, never a commit."""
    tid = _tool_fact(facts, "nas")
    aid = store.propose("server", {"hostname": "nas"}, {"hostname": tid}).artifact_id
    store.propose("server", {"hostname": "guess"}, artifact_id=aid)
    assert store.committed(aid) == {"hostname": "nas"}
    assert store.citations(aid) == {"hostname": tid}
    assert store.provisional(aid) == {"hostname": "guess"}


def test_commit_clears_provisional_for_that_leaf(store: ArtifactStore, facts: FactStore) -> None:
    aid = store.propose("server", {"hostname": "guess", "cores": 2}).artifact_id
    tid = _tool_fact(facts, "nas")
    store.propose("server", {"hostname": "nas"}, {"hostname": tid}, artifact_id=aid)
    assert store.committed(aid) == {"hostname": "nas"}
    assert store.provisional(aid) == {"cores": 2}


def test_overlapping_paths_supersede(store: ArtifactStore, facts: FactStore) -> None:
    """Setting ``backup`` to null and later ``backup.city`` never leaves both in one view."""
    t_none, t_city = _tool_fact(facts, None), _tool_fact(facts, "Bergen")
    aid = store.propose("server", {"backup": None}, {"backup": t_none}).artifact_id
    assert store.committed(aid) == {"backup": None}
    store.propose("server", {"backup": {"city": "Bergen"}}, {"backup.city": t_city},
                  artifact_id=aid)
    assert store.committed(aid) == {"backup": {"city": "Bergen"}}
    assert store.citations(aid) == {"backup.city": t_city}
    store.propose("server", {"backup": None}, {"backup": t_none}, artifact_id=aid)
    assert store.committed(aid) == {"backup": None}
    assert store.citations(aid) == {"backup": t_none}


def test_new_artifact_ids_are_unique_and_explicit_ids_kept(store: ArtifactStore) -> None:
    a = store.propose("server", {"hostname": "a"}).artifact_id
    b = store.propose("server", {"hostname": "b"}).artifact_id
    assert a != b and a.startswith("art-")
    assert store.propose("server", {"hostname": "c"}, artifact_id="mine").artifact_id == "mine"


def test_kind_mismatch_on_existing_artifact(store: ArtifactStore) -> None:
    class Other(BaseModel):
        hostname: str

    store.register_kind("other", Other)
    aid = store.propose("server", {"hostname": "nas"}).artifact_id
    with pytest.raises(ArtifactKindMismatchError):
        store.propose("other", {"hostname": "x"}, artifact_id=aid)
    assert store.kind_of(aid) == "server"


# -- kinds and validation -------------------------------------------------------------


def test_unregistered_kind_rejected(store: ArtifactStore) -> None:
    with pytest.raises(UnknownKindError, match="nope"):
        store.propose("nope", {"hostname": "nas"})


def test_register_kind_validation(store: ArtifactStore) -> None:
    with pytest.raises(TypeError):
        store.register_kind("bad", dict)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.register_kind("", Server)
    with pytest.raises(ValueError, match="already registered"):
        store.register_kind("server", Address)
    store.register_kind("server", Server)  # same model again: idempotent
    store.register_kind("server", Address, replace=True)
    assert store.kinds()["server"] is Address


def test_unknown_field_rejected(store: ArtifactStore) -> None:
    with pytest.raises(ArtifactValidationError, match="ram"):
        store.propose("server", {"hostname": "nas", "ram": 16}, artifact_id="x")
    with pytest.raises(ArtifactValidationError, match="address.country"):
        store.propose("server", {"address": {"country": "NO"}}, artifact_id="x")
    with pytest.raises(ArtifactNotFoundError):
        store.provisional("x")


def test_type_invalid_leaf_rejected_even_when_uncited(store: ArtifactStore) -> None:
    with pytest.raises(ArtifactValidationError, match="cores"):
        store.propose("server", {"cores": "many"})
    with pytest.raises(ArtifactValidationError, match="cores"):
        store.propose("server", {"cores": 0})  # Field(ge=1) constraint applies


def test_leaf_values_are_coerced_to_json(store: ArtifactStore) -> None:
    aid = store.propose("server", {"cores": "4", "labels": {"env": "prod"}}).artifact_id
    assert store.provisional(aid) == {"cores": 4, "labels": {"env": "prod"}}


@pytest.mark.parametrize(
    "delta",
    [{}, {"address": {}}, {"a.b": 1}, {"": 1}, [("hostname", "x")]],
)
def test_malformed_delta_rejected(store: ArtifactStore, delta: Any) -> None:
    with pytest.raises(ArtifactValidationError):
        store.propose("server", delta)


def test_errors_share_a_base(store: ArtifactStore) -> None:
    for cls in (CitationError, ArtifactValidationError, UnknownKindError,
                ArtifactKindMismatchError, ArtifactIncompleteError):
        assert issubclass(cls, ArtifactError)


def test_mixed_validity_delta_stores_nothing(store: ArtifactStore, facts: FactStore) -> None:
    tid = _tool_fact(facts, "nas")
    with pytest.raises(ArtifactValidationError, match="cores"):
        store.propose("server", {"hostname": "nas", "cores": "many"}, {"hostname": tid},
                      artifact_id="mixed")
    with pytest.raises(ArtifactNotFoundError):
        store.committed("mixed")


# -- the registered model's own validators ----------------------------------------------


class Checked(BaseModel):
    hostname: str
    cores: int | None = None
    home: Address | None = None

    @field_validator("hostname")
    @classmethod
    def _no_spaces_lowercase(cls, value: str) -> str:
        if " " in value:
            raise ValueError("hostname must not contain spaces")
        return value.lower()


class StrictBox(BaseModel):
    model_config = ConfigDict(strict=True)
    cores: int | None = None


class CheckedCity(BaseModel):
    city: str

    @field_validator("city")
    @classmethod
    def _title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("city must not be blank")
        return value.title()


class Site(BaseModel):
    where: CheckedCity | None = None


def test_field_validator_rejects_cited_leaf(store: ArtifactStore, facts: FactStore) -> None:
    store.register_kind("checked", Checked)
    tid = _tool_fact(facts, "Bad Name")
    with pytest.raises(ArtifactValidationError, match="spaces"):
        store.propose("checked", {"hostname": "Bad Name"}, {"hostname": tid}, artifact_id="c")
    with pytest.raises(ArtifactNotFoundError):
        store.committed("c")


def test_field_validator_rejects_uncited_leaf(store: ArtifactStore) -> None:
    store.register_kind("checked", Checked)
    with pytest.raises(ArtifactValidationError, match="spaces"):
        store.propose("checked", {"hostname": "Bad Name"})


def test_normalising_validator_output_is_stored(store: ArtifactStore, facts: FactStore) -> None:
    """committed() and committed_model() agree: the validator's output is what is stored."""
    store.register_kind("checked", Checked)
    aid = store.propose("checked", {"hostname": "NAS"}, {"hostname": _tool_fact(facts, "NAS")},
                        ).artifact_id
    assert store.committed(aid) == {"hostname": "nas"}
    assert store.committed_model(aid).hostname == "nas"


def test_strict_model_config_is_honoured(store: ArtifactStore) -> None:
    store.register_kind("strict", StrictBox)
    with pytest.raises(ArtifactValidationError, match="cores"):
        store.propose("strict", {"cores": "4"})
    aid = store.propose("strict", {"cores": 4}).artifact_id
    assert store.provisional(aid) == {"cores": 4}


def test_nested_model_field_validator_runs(store: ArtifactStore, facts: FactStore) -> None:
    store.register_kind("site", Site)
    with pytest.raises(ArtifactValidationError, match="where.city"):
        store.propose("site", {"where": {"city": "  "}})
    aid = store.propose("site", {"where": {"city": "oslo"}},
                        {"where.city": _tool_fact(facts, "oslo")}).artifact_id
    assert store.committed(aid) == {"where": {"city": "Oslo"}}
    assert store.committed_model(aid) == Site(where=CheckedCity(city="Oslo"))


def test_model_validators_refused_at_register(store: ArtifactStore) -> None:
    """Cross-field model validators cannot be honoured per leaf: the model is refused."""
    class Pair(BaseModel):
        low: int = 0
        high: int = 0

        @model_validator(mode="after")
        def _ordered(self) -> Pair:
            if self.low > self.high:
                raise ValueError("low > high")
            return self

    class Holder(BaseModel):
        pair: Pair | None = None

    class ListHolder(BaseModel):
        pairs: list[Pair] = []  # a single leaf: validated whole, so allowed

    with pytest.raises(ValueError, match="model_validator"):
        store.register_kind("pair", Pair)
    with pytest.raises(ValueError, match="pair"):
        store.register_kind("holder", Holder)
    store.register_kind("lists", ListHolder)
    with pytest.raises(ArtifactValidationError, match="low > high"):
        store.propose("lists", {"pairs": [{"low": 2, "high": 1}]})
    assert "pair" not in store.kinds() and "holder" not in store.kinds()


def test_model_instance_value_is_walked_like_a_dict(
    store: ArtifactStore, facts: FactStore,
) -> None:
    """A nested model instance gets per-leaf cites, like a dict; unset fields are not leaves."""
    tid = _tool_fact(facts, "Oslo")
    res = store.propose("server", {"address": Address(city="Oslo")}, {"address.city": tid})
    assert res.committed == {"address.city": tid}
    assert res.provisional == ()
    assert store.committed(res.artifact_id) == {"address": {"city": "Oslo"}}
    res2 = store.propose("server", {"address": Address(city="Oslo", zip_code="0150")})
    assert set(res2.provisional) == {"address.city", "address.zip_code"}


class ParentChecked(BaseModel):
    address: Address | None = None

    @field_validator("address")
    @classmethod
    def _no_x_city(cls, value: Address | None) -> Address | None:
        if value is not None and value.city == "X":
            raise ValueError("parent field_validator on nested field")
        return value


def _not_x(value: Address | None) -> Address | None:
    if value is not None and value.city == "X":
        raise ValueError("parent Annotated validator on nested field")
    return value


class ParentAnnotated(BaseModel):
    address: Annotated[Address | None, AfterValidator(_not_x)] = None


class ParentSerialized(BaseModel):
    address: Address | None = None

    @field_serializer("address")
    def _ser(self, value: Address | None) -> Any:
        return value.model_dump() if value is not None else None


@pytest.mark.parametrize(
    ("model", "message"),
    [(ParentChecked, "parent field_validator"), (ParentAnnotated, "parent Annotated")],
)
def test_parent_validator_on_nested_field_runs_as_one_leaf(
    store: ArtifactStore, facts: FactStore, model: type[BaseModel], message: str,
) -> None:
    """A nested-model field the PARENT validates is one whole leaf, not walked into."""
    store.register_kind("pc", model)
    tid = _tool_fact(facts, {"city": "X"})
    with pytest.raises(ArtifactValidationError, match=message):
        store.propose("pc", {"address": {"city": "X"}}, {"address": tid}, artifact_id="pc1")
    with pytest.raises(ArtifactNotFoundError):
        store.committed("pc1")
    # Per-leaf cites do not apply to a whole-leaf field.
    with pytest.raises(ArtifactValidationError, match="address.city"):
        store.propose("pc", {"address": {"city": "Oslo"}}, {"address.city": tid})
    ok = _tool_fact(facts, {"city": "Oslo"})
    aid = store.propose("pc", {"address": Address(city="Oslo")}, {"address": ok}).artifact_id
    assert store.citations(aid) == {"address": ok}
    committed = store.committed(aid)
    assert committed == {"address": {"city": "Oslo", "zip_code": None}}
    # The two views agree.
    assert store.committed_model(aid).model_dump() == committed


def test_parent_serializer_on_nested_field_is_one_leaf(store: ArtifactStore) -> None:
    store.register_kind("ps", ParentSerialized)
    res = store.propose("ps", {"address": {"city": "Oslo"}})
    assert res.provisional == ("address",)


def test_unrelated_model_instance_is_not_duck_typed(store: ArtifactStore) -> None:
    class LookAlike(BaseModel):
        city: str

    with pytest.raises(ArtifactValidationError, match="address"):
        store.propose("server", {"address": LookAlike(city="Oslo")})


def test_subclass_instance_is_walked(store: ArtifactStore) -> None:
    class Sub(Address):
        pass

    res = store.propose("server", {"address": Sub(city="Oslo")})
    assert res.provisional == ("address.city",)


# -- views ----------------------------------------------------------------------------


def test_views_on_unknown_artifact(store: ArtifactStore) -> None:
    for view in (store.committed, store.provisional, store.citations, store.kind_of,
                 store.committed_model):
        with pytest.raises(ArtifactNotFoundError):
            view("nope")


def test_committed_model_requires_complete_commit(
    store: ArtifactStore, facts: FactStore,
) -> None:
    aid = store.propose(
        "server",
        {"hostname": "nas", "cores": 4, "address": {"city": "Oslo"}},
        {"hostname": _tool_fact(facts, "nas"), "cores": _tool_fact(facts, 4)},
    ).artifact_id
    # address.city is only provisional, so the committed model is incomplete.
    with pytest.raises(ArtifactIncompleteError, match="address"):
        store.committed_model(aid)
    store.propose("server", {"address": {"city": "Oslo"}},
                  {"address.city": _tool_fact(facts, "Oslo")}, artifact_id=aid)
    model = store.committed_model(aid)
    assert isinstance(model, Server)
    assert model == Server(hostname="nas", cores=4, address=Address(city="Oslo"))


def test_views_return_copies(store: ArtifactStore) -> None:
    aid = store.propose("server", {"tags": ["a"]}).artifact_id
    store.provisional(aid)["tags"].append("b")
    assert store.provisional(aid) == {"tags": ["a"]}


# -- persistence ----------------------------------------------------------------------


def test_state_survives_reopen(tmp_path: Path, facts: FactStore) -> None:
    state = tmp_path / "state"
    tid = _tool_fact(facts, "nas")
    with ArtifactStore(state, facts) as s:
        s.register_kind("server", Server)
        aid = s.propose("server", {"hostname": "nas", "cores": 2}, {"hostname": tid}).artifact_id
    with ArtifactStore(state, facts) as s:
        # Views work without re-registering; only committed_model needs the kind.
        assert s.committed(aid) == {"hostname": "nas"}
        assert s.provisional(aid) == {"cores": 2}
        assert s.citations(aid) == {"hostname": tid}
        assert s.kind_of(aid) == "server"
        with pytest.raises(UnknownKindError):
            s.committed_model(aid)


def test_sqlite_setup(tmp_path: Path, facts: FactStore) -> None:
    with ArtifactStore(tmp_path / "state", facts) as s:
        assert s.path == tmp_path / "state" / ARTIFACTS_DB_FILENAME
        assert s.schema_version() == SCHEMA_VERSION
    conn = sqlite3.connect(tmp_path / "state" / ARTIFACTS_DB_FILENAME)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_newer_schema_refused(tmp_path: Path, facts: FactStore) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state / ARTIFACTS_DB_FILENAME)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(ArtifactStoreError, match="newer"):
        ArtifactStore(state, facts)


# -- concurrency ----------------------------------------------------------------------


class _Wide(BaseModel):
    model_config = {"extra": "forbid"}
    f0: int | None = None
    f1: int | None = None
    f2: int | None = None
    f3: int | None = None
    f4: int | None = None
    f5: int | None = None


def _race_fresh_file(state: Path, facts: FactStore, tids: list[str]) -> None:
    n = len(tids)
    errors: list[BaseException] = []
    barrier = threading.Barrier(n)

    def work(idx: int) -> None:
        try:
            barrier.wait(timeout=60)
            with ArtifactStore(state, facts) as s:
                s.register_kind("wide", _Wide)
                for _ in range(5):
                    s.propose("wide", {f"f{idx}": idx}, {f"f{idx}": tids[idx]},
                              artifact_id="w")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            barrier.abort()

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, (state, errors)
    with ArtifactStore(state, facts) as s:
        assert s.committed("w") == {f"f{i}": i for i in range(n)}
        assert s.citations("w") == {f"f{i}": tids[i] for i in range(n)}
    _assert_integrity(state)


def test_concurrent_threads_distinct_leaves(tmp_path: Path, facts: FactStore) -> None:
    """Fresh file, many instances at once (the Task 22 WAL-switch race), one artifact.

    Repeated over 15 fresh files: one round hits the race only sometimes, so a single
    round is a weak guard. ``test_enable_wal_retries_*`` prove the retry deterministically.
    """
    tids = [_tool_fact(facts, i) for i in range(6)]
    for round_no in range(15):
        _race_fresh_file(tmp_path / f"art-state-{round_no}", facts, tids)


class _LockedThenOk:
    """A fake connection whose WAL switch reports ``database is locked`` ``fails`` times."""

    def __init__(self, fails: int, message: str = "database is locked") -> None:
        self.fails = fails
        self.message = message
        self.calls = 0

    def execute(self, sql: str) -> None:
        assert sql == "PRAGMA journal_mode=WAL"
        self.calls += 1
        if self.calls <= self.fails:
            raise sqlite3.OperationalError(self.message)


def test_enable_wal_retries_until_unlocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic proof of the retry the artifact store relies on (Task 22 race)."""
    from nanobot.kernel import facts as facts_mod

    sleeps: list[float] = []
    monkeypatch.setattr(facts_mod.time, "sleep", sleeps.append)
    conn = _LockedThenOk(fails=5)
    facts_mod._enable_wal(conn)  # type: ignore[arg-type]
    assert conn.calls == 6
    assert len(sleeps) == 5 and sleeps == sorted(sleeps)  # backoff grows


def test_enable_wal_gives_up_at_deadline_and_on_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nanobot.kernel import facts as facts_mod

    clock = [0.0]
    monkeypatch.setattr(facts_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(facts_mod.time, "sleep", lambda d: clock.__setitem__(0, clock[0] + d))
    forever = _LockedThenOk(fails=10**9)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        facts_mod._enable_wal(forever)  # type: ignore[arg-type]
    assert clock[0] >= facts_mod._BUSY_TIMEOUT_MS / 1000
    other = _LockedThenOk(fails=1, message="disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk"):
        facts_mod._enable_wal(other)  # type: ignore[arg-type]
    assert other.calls == 1


def test_artifact_store_uses_the_retrying_wal_switch() -> None:
    from nanobot.kernel import artifacts as art_mod
    from nanobot.kernel import facts as facts_mod

    assert art_mod._enable_wal is facts_mod._enable_wal


_WRITER = """
import json, sys
from pathlib import Path
from pydantic import BaseModel
from nanobot.kernel.artifacts import ArtifactStore
from nanobot.kernel.facts import FactStore

class Rec(BaseModel):
    items: dict[str, int] = {}
    a: int | None = None
    b: int | None = None
    c: int | None = None
    d: int | None = None

state, tag, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
with FactStore(state) as facts, ArtifactStore(state, facts) as s:
    s.register_kind("rec", Rec)
    cites = []
    for i in range(n):
        tid = facts.record("tool", tag, i)
        s.propose("rec", {tag: i}, {tag: tid}, artifact_id="p")
        s.propose("rec", {tag: i}, artifact_id=f"{tag}-{i}")
        cites.append(tid)
print(json.dumps(cites))
"""


def test_concurrent_processes(tmp_path: Path) -> None:
    """Separate OS processes on fresh fact + artifact databases: no lost writes."""
    state = tmp_path / "state"
    tags, per_proc = ["a", "b", "c", "d"], 40
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(state), tag, str(per_proc)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for tag in tags
    ]
    last: dict[str, str] = {}
    for tag, p in zip(tags, procs):
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err
        last[tag] = json.loads(out.strip().splitlines()[-1])[-1]
    with FactStore(state) as facts, ArtifactStore(state, facts) as s:
        assert s.committed("p") == {tag: per_proc - 1 for tag in tags}
        assert s.citations("p") == last
        for tid in last.values():
            assert facts.resolve(tid) is not None
        for tag in tags:
            for i in range(per_proc):
                assert s.provisional(f"{tag}-{i}") == {tag: i}
    _assert_integrity(state)


def _assert_integrity(state: Path) -> None:
    conn = sqlite3.connect(state / ARTIFACTS_DB_FILENAME)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


# -- trace ----------------------------------------------------------------------------


def test_propose_emits_trace_without_values(tmp_path: Path, facts: FactStore) -> None:
    sink = RecordingSink()
    tid = _tool_fact(facts, "nas")
    with ArtifactStore(tmp_path / "state", facts, trace=sink) as s:
        s.register_kind("server", Server)
        aid = s.propose("server", {"hostname": "nas", "cores": 3}, {"hostname": tid}).artifact_id
        with pytest.raises(CitationError):
            s.propose("server", {"hostname": "secret-value"}, {"hostname": MISSING},
                      artifact_id=aid)
    assert sink.events[0] == {
        "event": "artifact.proposed",
        "artifact_id": aid,
        "kind": "server",
        "committed": {"hostname": tid},
        "provisional": ["cores"],
    }
    assert sink.events[1] == {
        "event": "artifact.rejected",
        "artifact_id": aid,
        "kind": "server",
        "reason": "CitationError",
        "paths": ["hostname"],
    }
    assert "secret-value" not in json.dumps(sink.events)
    assert "nas" not in json.dumps(sink.events)


def test_failing_sink_does_not_break_propose(tmp_path: Path, facts: FactStore) -> None:
    class Boom:
        def emit(self, event: dict[str, Any]) -> None:
            raise RuntimeError("sink down")

    with ArtifactStore(tmp_path / "state", facts, trace=Boom()) as s:
        s.register_kind("server", Server)
        aid = s.propose("server", {"hostname": "nas"}).artifact_id
        assert s.provisional(aid) == {"hostname": "nas"}


# -- env ------------------------------------------------------------------------------


def test_from_env(tmp_path: Path) -> None:
    paths = Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state")
    sink = RecordingSink()
    env = CoreEnvironment(
        config=_EmptyConfig(), credentials=StaticCredentialResolver({}), paths=paths, trace=sink,
    )
    with ArtifactStore.from_env(env) as s:
        assert s.path == paths.state_dir / ARTIFACTS_DB_FILENAME
        assert s.facts.path.parent == paths.state_dir
        s.register_kind("server", Server)
        tid = s.facts.record("user", "turn-1", "nas")
        s.propose("server", {"hostname": "nas"}, {"hostname": tid})
    assert [e["event"] for e in sink.events] == ["fact.recorded", "artifact.proposed"]
    assert not (paths.work_dir / ARTIFACTS_DB_FILENAME).exists()


# -- file floor -----------------------------------------------------------------------


def _assert_floor_protects(floor: ProtectedFloor, path: Path) -> None:
    for cap in ("fs.write", "fs.read"):
        deny = check_floors(AGENT, CapabilityRequest(cap, str(path)), protected=floor)
        assert deny is not None, (cap, path)
        assert deny.marker == PROTECTED_MARKER


def _db_files(state: Path) -> list[Path]:
    db = state / ARTIFACTS_DB_FILENAME
    return [db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")]


def test_db_protected_in_split_layout(tmp_path: Path) -> None:
    paths = Paths(work_dir=tmp_path / "work", state_dir=tmp_path / "state")
    for f in _db_files(paths.state_dir):
        _assert_floor_protects(ProtectedFloor.from_paths(paths), f)


def test_db_protected_in_legacy_flat_layout(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    paths = Paths(work_dir=ws, state_dir=ws, overlap_ok=True)
    for f in _db_files(ws):
        _assert_floor_protects(ProtectedFloor.from_paths(paths), f)
        _assert_floor_protects(ProtectedFloor(data_dir=None, workspace=ws), f)
    assert ProtectedFloor.from_paths(paths).reason(ws / "artifacts.txt", write=True) is None
