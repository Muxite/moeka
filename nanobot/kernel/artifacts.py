"""Typed artifact store enforcing I3, "unearned knowledge is forbidden" (Task 23).

Named ``kernel/artifacts.py``; unrelated to ``nanobot/utils/artifacts.py``.

Contract:
- The host or a plugin registers a pydantic model per artifact kind with
  :meth:`ArtifactStore.register_kind`. Kinds live in memory on the instance (the
  same idiom as :class:`nanobot.kernel.solvers.SolverRegistry`); stored artifacts
  keep their kind name, so the views work after a reopen without re-registering.
- :meth:`ArtifactStore.propose` takes ``(kind, delta, cites, artifact_id=)``.
  ``delta`` is a nested dict; ``cites`` maps a dotted LEAF path (``"address.city"``)
  to a :class:`~nanobot.kernel.facts.FactStore` trace ID.
- Leaves: the delta is walked along the registered model. A key naming a nested
  pydantic model field (``Address`` or ``Address | None``) whose value is a dict is
  descended into, unless the parent hooks that field (see below); any other field is
  one leaf, whatever its value (a ``list`` or a ``dict[str, str]`` field is a single
  leaf). Cites are per leaf: a cite on a parent
  path does not cover the leaves below it and is rejected.
- Per leaf:
  - cited, and ``FactStore.resolve(trace_id)`` returns a record: COMMITTED;
  - no cite: stored as PROVISIONAL (design I3: "unsupported inferences stay
    provisional flags"). It never appears in :meth:`committed`.
  - cited, but the trace ID resolves to nothing (unknown, malformed, not a string):
    the WHOLE ``propose`` is rejected with :class:`CitationError` and nothing is
    stored, not even the uncited leaves. Rationale: uncited data is an honest
    "not yet grounded"; a cite to a fact that does not exist is a false claim of
    provenance (a hallucinated or forged ID, or a caller bug), so it is refused
    outright rather than silently downgraded to provisional.
- User confirmation is not a sentinel. Design section 7: "the user's answer is
  recorded with user provenance before the commit". The caller records it with
  ``FactStore.record("user", <turn ref>, value)`` and cites the returned trace ID,
  so a user-confirmed leaf is as traceable as a document or tool leaf. A literal
  ``"user"`` cite is just an unknown trace ID and is rejected.
- Validation (strict, atomic, before any write):
  - unregistered kind: :class:`UnknownKindError`;
  - an empty delta, a non-dict delta, an empty nested dict, a key that is empty,
    not a string or contains ``"."``, a key that is not a field of the model
    (``extra`` config is ignored: unknown fields are always rejected), a leaf value
    that fails its field type and constraints, or a cite whose path is not a leaf of
    this delta: :class:`ArtifactValidationError`;
  - an existing ``artifact_id`` of another kind: :class:`ArtifactKindMismatchError`.
  Uncited leaves are validated too: provisional means "not grounded", never
  "not typed".
- Leaf validation runs through the registered model ITSELF (review fix, option a):
  ``model.__pydantic_validator__.validate_assignment`` on a ``model_construct()``
  instance, per leaf, on the model that owns the field (a nested model for a nested
  leaf). So the field type and constraints, ``@field_validator``s, Annotated
  validators and ``model_config`` (``strict=True`` rejects ``"4"`` for an ``int``)
  all apply, and the stored value is the model's own JSON dump of the validated
  value: a normalising validator's output is what ``committed()`` returns, the same
  value ``committed_model()`` yields. Caveat: a field validator reading
  ``info.data`` sees only defaults for the other fields, never the artifact's
  other leaves.
- ``@model_validator``s are cross-field and cannot be honoured leaf by leaf (and
  would run against a partial instance), so :meth:`ArtifactStore.register_kind`
  REFUSES a model that defines one, on the root or on any nested model the leaf walk
  descends into (option b for that part). A model inside a single leaf (e.g.
  ``list[Pair]``) is validated whole, so its model validators do run and are allowed.
- A nested model INSTANCE (of the field's model or a subclass) as a delta value is
  walked like a dict of the fields it set (``model_dump(exclude_unset=True)``), so it
  gets per-leaf cites too; defaults it did not set are not leaves. An instance of an
  unrelated model is not duck-typed: it is validated whole and rejected.
- A nested-model field that the PARENT hooks (``@field_validator`` or
  ``@field_serializer`` naming it or ``"*"``, or field metadata such as
  ``Annotated[M | None, AfterValidator(f)]``) is NOT walked into: it is one whole leaf
  (cite path ``"address"``, not ``"address.city"``), so the parent's validator runs
  on the whole nested value and ``committed()`` matches ``committed_model()``. This
  whole-leaf fallback (chosen over refusing the model) is the same rule as for a
  model inside ``list[M]``: validators that cannot run per leaf get the whole value.
- Accumulation: ``propose`` on an existing artifact merges leaf by leaf; it never
  replaces the artifact wholesale. ``artifact_id=None`` creates ``art-<32 hex>``;
  an unknown explicit ID creates that artifact.
  - A committed leaf replaces the committed value and cite at that path and clears
    any provisional value there.
  - A provisional leaf never touches the committed value at that path: an uncited
    change to a committed value is a pending edit shown by :meth:`provisional`.
  - Overlapping paths supersede within one view: committing ``backup`` removes
    committed ``backup.*`` and vice versa (same for provisional), so a view never
    holds both a parent value and a child of it.
- Views:
  - :meth:`committed` returns a PARTIAL nested dict of committed leaves only (a
    fresh copy). Required fields may be missing: an artifact is built up over time.
  - :meth:`committed_model` validates the committed dict against the registered
    model and returns the model instance; missing required fields raise
    :class:`ArtifactIncompleteError`. It needs the kind registered on this instance.
  - :meth:`provisional` returns a nested dict of provisional leaves, never mixed
    into the committed views. :meth:`citations` maps each committed leaf path to its
    trace ID. Unknown artifact IDs raise :class:`ArtifactNotFoundError`.

What "committed" guarantees (and what it does not):
- It guarantees the leaf was proposed with a cite that resolved, at propose time,
  to a record in the fact store handed to this instance. Facts are immutable and
  never deleted, so the cite keeps resolving.
- It does NOT check that the fact supports the value: the store checks provenance
  exists, not that it is relevant or correct. The epistemic audit ("supported by
  provenance?", design section 7) is the caller's job.
- It is not proof against an ``exec``-capable agent. The file floor denies the file
  tools access to ``facts.db`` and ``artifacts.db`` (and their SQLite sidecars), but
  ``exec`` is not stopped by the file floor: a shell can insert a fake fact into
  ``facts.db`` and then cite it, or write ``artifacts.db`` directly. A non-``None``
  ``resolve`` is necessary, not sufficient, provenance against such an agent. Real
  containment needs OS isolation (a sandboxed exec backend); strict mode refuses
  exec without one.

Storage:
- ``<state_dir>/artifacts.db`` (:data:`ARTIFACTS_DB_FILENAME`), next to
  ``facts.db``. A separate file, not tables inside ``facts.db``: the fact store owns
  its file and schema version and refuses newer ones, and facts are append-only, so
  a cross-file reference cannot dangle. ``state_dir`` for the same reason as the
  fact store and the plugin registry: host-owned kernel state.
- SQLite set up exactly like :mod:`nanobot.kernel.facts`: WAL (switched with the
  same retry, :func:`nanobot.kernel.facts._enable_wal`, so concurrent first opens
  of a fresh file do not hit the Task 22 ``database is locked`` race), busy timeout,
  connect timeout, one connection per instance, an instance lock, schema created
  and ``PRAGMA user_version`` set in one ``BEGIN IMMEDIATE``; a newer version is
  refused with :class:`ArtifactStoreError`.
- Each ``propose`` is one ``BEGIN IMMEDIATE`` transaction (kind check plus all leaf
  writes), so concurrent proposers on one artifact serialise and none is lost.
  Cites are resolved before the transaction (facts are immutable).
- Tables: ``artifacts(artifact_id, kind, created_at, updated_at)`` and
  ``fields(artifact_id, path, state, value, trace_id, updated_at)`` with
  ``state`` in (``committed``, ``provisional``); ``trace_id`` is NOT NULL exactly
  for committed rows (a SQL ``CHECK``).

Trace (values never on the trace):
- ``{"event": "artifact.proposed", "artifact_id", "kind", "committed": {path:
  trace_id}, "provisional": [paths]}`` per accepted ``propose``. This is the raw
  signal for the "provenance rate" objective (design section 8).
- ``{"event": "artifact.rejected", "artifact_id", "kind", "reason": <error class>,
  "paths": [paths]}`` per rejected ``propose``.

Wiring (what is live): library only. :meth:`ArtifactStore.from_env` builds the store
(and, unless given, its fact store) from a ``CoreEnvironment``. No gateway,
``AgentLoop`` or tool path proposes artifacts yet.

Imports: stdlib, pydantic and kernel modules only; import-cheap.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import types
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from nanobot.kernel.facts import FactStore, _enable_wal
from nanobot.kernel.trace import NullTraceSink, TraceSink, safe_emit

if TYPE_CHECKING:
    from nanobot.kernel.env import CoreEnvironment

ARTIFACTS_DB_FILENAME = "artifacts.db"
SCHEMA_VERSION = 1
ARTIFACT_ID_PREFIX = "art-"

_CONNECT_TIMEOUT_S = 30.0
_BUSY_TIMEOUT_MS = 10_000
_COMMITTED = "committed"
_PROVISIONAL = "provisional"


class ArtifactStoreError(RuntimeError):
    """The artifact database cannot be used (e.g. written by a newer schema)."""


class ArtifactError(ValueError):
    """A rejected ``propose`` or view request. ``paths`` names the offending leaves."""

    def __init__(self, message: str, paths: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.paths = paths


class UnknownKindError(ArtifactError):
    """The artifact kind is not registered on this store."""


class ArtifactValidationError(ArtifactError):
    """The delta or its cites do not fit the registered model."""


class CitationError(ArtifactError):
    """A cite names a trace ID that resolves to no fact."""


class ArtifactKindMismatchError(ArtifactError):
    """The artifact exists with a different kind."""


class ArtifactIncompleteError(ArtifactError):
    """The committed leaves do not yet form a valid model."""


class ArtifactNotFoundError(LookupError):
    """No artifact with that ID."""


@dataclass(frozen=True)
class ArtifactResult:
    """Outcome of an accepted ``propose``: leaves committed (path -> trace ID) and provisional."""

    artifact_id: str
    kind: str
    committed: dict[str, str]
    provisional: tuple[str, ...]


def new_artifact_id() -> str:
    return ARTIFACT_ID_PREFIX + uuid.uuid4().hex


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """The pydantic model a field holds (``M`` or ``M | None``), else None."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1 and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return args[0]
    return None


def _parent_hooks_field(model: type[BaseModel], name: str) -> bool:
    """True when ``model`` itself attaches validation or serialization to field ``name``.

    ``@field_validator``/``@field_serializer`` naming the field (or ``"*"``), or field
    metadata (``Annotated[..., AfterValidator(f)]``, constraints) on the parent.
    """
    field = model.model_fields[name]
    if field.metadata:
        return True
    decorators = model.__pydantic_decorators__
    for group in (decorators.field_validators, decorators.field_serializers):
        for decorator in group.values():
            if name in decorator.info.fields or "*" in decorator.info.fields:
                return True
    return False


def _walk_target(model: type[BaseModel], name: str) -> type[BaseModel] | None:
    """The nested model the leaf walk descends into for field ``name``, else None.

    A nested-model field the PARENT validates or serializes is NOT walked: it is one
    whole leaf, so the parent's hooks run on the whole value (review round 2).
    """
    sub = _nested_model(model.model_fields[name].annotation)
    if sub is None or _parent_hooks_field(model, name):
        return None
    return sub


def _walked_models(
    model: type[BaseModel], prefix: str = "", seen: frozenset[type] = frozenset(),
) -> list[tuple[str, type[BaseModel]]]:
    """``model`` and every nested model the leaf walk descends into, with their paths."""
    out = [(prefix, model)]
    if model in seen:
        return out
    for name in model.model_fields:
        sub = _walk_target(model, name)
        if sub is not None:
            out.extend(_walked_models(sub, f"{prefix}{name}.", seen | {model}))
    return out


def _validate_leaf(model: type[BaseModel], key: str, value: Any) -> Any:
    """Validate one field value through the model's OWN validator; return its JSON dump.

    ``validate_assignment`` on a ``model_construct()`` instance runs the field's type,
    constraints, ``@field_validator``s (before/after/wrap/plain), Annotated validators
    and the model config (``strict``, ...), exactly as the model would, without
    requiring the other fields. The dump goes through the model's serializer, so a
    normalising validator's output is what gets stored.
    """
    instance = model.model_construct()
    model.__pydantic_validator__.validate_assignment(instance, key, value)
    return instance.model_dump(mode="json", include={key})[key]


def _flatten(model: type[BaseModel], delta: Any, prefix: str = "") -> dict[str, Any]:
    """Validate ``delta`` against ``model``; return {dotted leaf path: JSON value}."""
    where = prefix.rstrip(".") or "delta"
    if not isinstance(delta, dict):
        raise ArtifactValidationError(f"{where}: expected an object, got {type(delta).__name__}",
                                      (prefix.rstrip("."),) if prefix else ())
    if not delta:
        raise ArtifactValidationError(f"{where}: empty", (prefix.rstrip("."),) if prefix else ())
    leaves: dict[str, Any] = {}
    errors: list[str] = []
    bad: list[str] = []
    for key, value in delta.items():
        path = f"{prefix}{key}"
        if not isinstance(key, str) or not key or "." in key:
            errors.append(f"{path!r}: field names must be non-empty strings without '.'")
            bad.append(str(path))
            continue
        field = model.model_fields.get(key)
        if field is None:
            errors.append(f"{path}: not a field of {model.__name__}")
            bad.append(path)
            continue
        sub = _walk_target(model, key)
        if sub is not None and isinstance(value, sub):
            # An instance of the field's model (or a subclass) is walked like a dict of
            # the fields it set, so it gets the same per-leaf cites; defaults it did not
            # set are not leaves. Any other model instance is not duck-typed: it goes to
            # leaf validation whole, where the model rejects it.
            value = value.model_dump(exclude_unset=True)
        if sub is not None and isinstance(value, dict):
            try:
                leaves.update(_flatten(sub, value, path + "."))
            except ArtifactValidationError as exc:
                errors.append(str(exc))
                bad.extend(exc.paths)
            continue
        try:
            leaves[path] = _validate_leaf(model, key, value)
        except ValidationError as exc:
            detail = "; ".join(e["msg"] for e in exc.errors())
            errors.append(f"{path}: {detail}")
            bad.append(path)
    if errors:
        raise ArtifactValidationError("; ".join(errors), tuple(bad))
    return leaves


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in sorted(flat):
        node = out
        *parents, last = path.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = flat[path]
    return out


class ArtifactStore:
    """Typed artifacts whose committed leaves each cite a resolvable fact (I3)."""

    def __init__(
        self, state_dir: Path, facts: FactStore, *, trace: TraceSink | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / ARTIFACTS_DB_FILENAME
        self.facts = facts
        self._trace: TraceSink = trace if trace is not None else NullTraceSink()
        self._kinds: dict[str, type[BaseModel]] = {}
        self._lock = threading.RLock()
        self._conn_obj: sqlite3.Connection | None = None
        self._owns_facts = False
        try:
            self._ensure_schema()
        except BaseException:
            self.close()
            raise

    @classmethod
    def from_env(cls, env: CoreEnvironment, *, facts: FactStore | None = None) -> ArtifactStore:
        """The host's artifact store under ``env.paths.state_dir``, tracing to ``env.trace``.

        Without ``facts`` it opens ``FactStore.from_env(env)`` and closes it on :meth:`close`.
        """
        owned = facts is None
        fact_store = FactStore.from_env(env) if facts is None else facts
        try:
            store = cls(env.paths.state_dir, fact_store, trace=env.trace)
        except BaseException:
            if owned:
                fact_store.close()
            raise
        store._owns_facts = owned
        return store

    # -- kinds ---------------------------------------------------------------

    def register_kind(self, name: str, model: type[BaseModel], *, replace: bool = False) -> None:
        """Register ``model`` for artifact kind ``name`` (idempotent for the same model)."""
        if not isinstance(name, str) or not name:
            raise ValueError("kind name must be a non-empty string")
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise TypeError("kind model must be a pydantic BaseModel subclass")
        for path, sub in _walked_models(model):
            if sub.__pydantic_decorators__.model_validators:
                where = path.rstrip(".") or "<root>"
                raise ValueError(
                    f"artifact kind {name!r}: {sub.__name__} at {where} defines a "
                    "model_validator; cross-field validators cannot be honoured per leaf, "
                    "so the model is refused (field validators and config are honoured)"
                )
        with self._lock:
            current = self._kinds.get(name)
            if current is not None and current is not model and not replace:
                raise ValueError(
                    f"artifact kind {name!r} already registered to {current.__name__}"
                )
            self._kinds[name] = model

    def kinds(self) -> dict[str, type[BaseModel]]:
        """A copy of the registered kinds."""
        with self._lock:
            return dict(self._kinds)

    def _model(self, kind: str) -> type[BaseModel]:
        with self._lock:
            model = self._kinds.get(kind)
        if model is None:
            raise UnknownKindError(f"artifact kind {kind!r} is not registered")
        return model

    # -- SQLite plumbing -----------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if self._conn_obj is None:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=_CONNECT_TIMEOUT_S,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
                _enable_wal(conn)
            except BaseException:
                conn.close()
                raise
            self._conn_obj = conn
        return self._conn_obj

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            version = self.schema_version()
            if version > SCHEMA_VERSION:
                raise ArtifactStoreError(
                    f"artifact store {self.path} has schema version {version}, newer than "
                    f"supported {SCHEMA_VERSION}; refusing to open"
                )
            if version == SCHEMA_VERSION:
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS artifacts (
                        artifact_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fields (
                        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
                        path TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('committed', 'provisional')),
                        value TEXT NOT NULL,
                        trace_id TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (artifact_id, path, state),
                        CHECK ((state = 'committed') = (trace_id IS NOT NULL))
                    )
                    """
                )
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                with suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise

    def schema_version(self) -> int:
        """The database's ``PRAGMA user_version``."""
        with self._lock:
            row = self._conn().execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row is not None else 0

    def close(self) -> None:
        """Checkpoint the WAL and release the connection (and an owned fact store)."""
        with self._lock:
            conn, self._conn_obj = self._conn_obj, None
            try:
                if conn is not None:
                    try:
                        with suppress(sqlite3.OperationalError):
                            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    finally:
                        with suppress(sqlite3.Error):
                            conn.close()
            finally:
                if self._owns_facts:
                    self._owns_facts = False
                    self.facts.close()

    def __enter__(self) -> ArtifactStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- propose -------------------------------------------------------------

    def _reject(self, exc: ArtifactError, kind: str, artifact_id: str | None) -> ArtifactError:
        safe_emit(self._trace, {
            "event": "artifact.rejected",
            "artifact_id": artifact_id,
            "kind": kind,
            "reason": type(exc).__name__,
            "paths": list(exc.paths),
        })
        return exc

    def propose(
        self,
        kind: str,
        delta: dict[str, Any],
        cites: dict[str, str] | None = None,
        *,
        artifact_id: str | None = None,
    ) -> ArtifactResult:
        """Merge ``delta`` into an artifact: cited leaves commit, uncited stay provisional.

        See the module docstring for the full contract. Raises an :class:`ArtifactError`
        subclass and stores nothing when any part of the delta or its cites is invalid.
        """
        try:
            if artifact_id is not None and (not isinstance(artifact_id, str) or not artifact_id):
                raise ArtifactValidationError("artifact_id must be a non-empty string")
            model = self._model(kind)
            leaves = _flatten(model, delta)
            cites = dict(cites or {})
            stray = sorted(str(p) for p in cites if p not in leaves)
            if stray:
                raise ArtifactValidationError(
                    "cites name paths that are not leaves of this delta: " + ", ".join(stray),
                    tuple(stray),
                )
            unresolved = sorted(
                path for path, tid in cites.items()
                if not isinstance(tid, str) or self.facts.resolve(tid) is None
            )
            if unresolved:
                raise CitationError(
                    "cites resolve to no fact (rejected, nothing stored): "
                    + ", ".join(unresolved),
                    tuple(unresolved),
                )
        except ArtifactError as exc:
            raise self._reject(exc, kind, artifact_id) from None

        aid = artifact_id or new_artifact_id()
        committed = {p: cites[p] for p in leaves if p in cites}
        provisional = tuple(p for p in leaves if p not in cites)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT kind FROM artifacts WHERE artifact_id = ?", (aid,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO artifacts (artifact_id, kind, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (aid, kind, now, now),
                    )
                elif row["kind"] != kind:
                    raise ArtifactKindMismatchError(
                        f"artifact {aid!r} is of kind {row['kind']!r}, not {kind!r}"
                    )
                else:
                    conn.execute(
                        "UPDATE artifacts SET updated_at = ? WHERE artifact_id = ?", (now, aid),
                    )
                for path, value in leaves.items():
                    state = _COMMITTED if path in committed else _PROVISIONAL
                    # Overlapping paths supersede: same path, ancestors and descendants.
                    # A commit also clears provisional values there.
                    states = (_COMMITTED, _PROVISIONAL) if state == _COMMITTED else (_PROVISIONAL,)
                    for s in states:
                        conn.execute(
                            "DELETE FROM fields WHERE artifact_id = ? AND state = ? AND ("
                            " path = ?"
                            " OR substr(path, 1, length(?) + 1) = ? || '.'"
                            " OR substr(?, 1, length(path) + 1) = path || '.')",
                            (aid, s, path, path, path, path),
                        )
                    conn.execute(
                        "INSERT INTO fields (artifact_id, path, state, value, trace_id, "
                        "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (aid, path, state, json.dumps(value, ensure_ascii=False),
                         committed.get(path), now),
                    )
                conn.execute("COMMIT")
            except BaseException as exc:
                with suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                if isinstance(exc, ArtifactError):
                    raise self._reject(exc, kind, aid) from None
                raise
        safe_emit(self._trace, {
            "event": "artifact.proposed",
            "artifact_id": aid,
            "kind": kind,
            "committed": dict(committed),
            "provisional": list(provisional),
        })
        return ArtifactResult(aid, kind, committed, provisional)

    # -- views ---------------------------------------------------------------

    def kind_of(self, artifact_id: str) -> str:
        """The kind the artifact was created with."""
        with self._lock:
            row = self._conn().execute(
                "SELECT kind FROM artifacts WHERE artifact_id = ?", (artifact_id,),
            ).fetchone()
        if row is None:
            raise ArtifactNotFoundError(f"no artifact {artifact_id!r}")
        return str(row["kind"])

    def _rows(self, artifact_id: str, state: str) -> list[sqlite3.Row]:
        with self._lock:
            conn = self._conn()
            if conn.execute(
                "SELECT 1 FROM artifacts WHERE artifact_id = ?", (artifact_id,),
            ).fetchone() is None:
                raise ArtifactNotFoundError(f"no artifact {artifact_id!r}")
            return conn.execute(
                "SELECT path, value, trace_id FROM fields WHERE artifact_id = ? AND state = ?",
                (artifact_id, state),
            ).fetchall()

    def committed(self, artifact_id: str) -> dict[str, Any]:
        """Committed leaves only, as a partial nested dict (a fresh copy)."""
        rows = self._rows(artifact_id, _COMMITTED)
        return _nest({r["path"]: json.loads(r["value"]) for r in rows})

    def citations(self, artifact_id: str) -> dict[str, str]:
        """Committed leaf path -> the trace ID it cites."""
        rows = self._rows(artifact_id, _COMMITTED)
        return {r["path"]: r["trace_id"] for r in sorted(rows, key=lambda r: r["path"])}

    def provisional(self, artifact_id: str) -> dict[str, Any]:
        """Uncited (provisional) leaves as a nested dict, never part of the committed views."""
        rows = self._rows(artifact_id, _PROVISIONAL)
        return _nest({r["path"]: json.loads(r["value"]) for r in rows})

    def committed_model(self, artifact_id: str) -> BaseModel:
        """The committed leaves validated as the kind's model; incomplete raises."""
        kind = self.kind_of(artifact_id)
        model = self._model(kind)
        data = self.committed(artifact_id)
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            paths = tuple(".".join(str(p) for p in e["loc"]) for e in exc.errors())
            raise ArtifactIncompleteError(
                f"artifact {artifact_id!r} committed leaves are not a valid {model.__name__}: "
                + "; ".join(f"{p}: {e['msg']}" for p, e in zip(paths, exc.errors())),
                paths,
            ) from None


__all__ = [
    "ARTIFACTS_DB_FILENAME",
    "ARTIFACT_ID_PREFIX",
    "SCHEMA_VERSION",
    "ArtifactError",
    "ArtifactIncompleteError",
    "ArtifactKindMismatchError",
    "ArtifactNotFoundError",
    "ArtifactResult",
    "ArtifactStore",
    "ArtifactStoreError",
    "ArtifactValidationError",
    "CitationError",
    "UnknownKindError",
    "new_artifact_id",
]
