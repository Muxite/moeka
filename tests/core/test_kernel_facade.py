"""Task 25: the P5 facade on ``MoekaKernel`` (``.env``, ``.facts``, ``.artifacts``,
``.propose``, ``.answer``).

Headline properties:
- the stores are built lazily from the loop's ``CoreEnvironment`` (``env.paths.state_dir``)
  and share ONE fact store, so ``answer`` cites resolve;
- nothing is created on disk until a store is first used (the facade is additive);
- no ``CoreEnvironment`` (a hand-built loop without one): the stores are ``None`` and
  ``propose``/``answer`` raise a clear ``RuntimeError``;
- a host may inject its own stores; an artifact store over a different fact store is
  refused;
- ``cleanup()`` closes the stores the kernel built, never injected ones.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from nanobot.core import MoekaCore, MoekaKernel
from nanobot.kernel.artifacts import ARTIFACTS_DB_FILENAME, ArtifactStore
from nanobot.kernel.clarify import Question
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.facts import FACTS_DB_FILENAME, FactStore
from nanobot.kernel.legacy import LegacyEnvironment
from nanobot.kernel.trace import NullTraceSink

_CFG = {
    "providers": {"openrouter": {"apiKey": "sk-test-key"}},
    "agents": {"defaults": {"model": "openrouter/test-model"}},
}


class Server(BaseModel):
    hostname: str
    os: str | None = None


def _env(tmp_path: Path) -> CoreEnvironment:
    work, state = tmp_path / "work", tmp_path / "state"
    work.mkdir()
    state.mkdir()
    from nanobot.config.schema import Config

    cfg = Config.model_validate({"agents": {"defaults": {"workspace": str(work)}}})
    return CoreEnvironment(
        config=LegacyEnvironment.from_config(cfg).config,
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=work, state_dir=state),
        trace=NullTraceSink(),
        strict=True,
    )


@pytest.fixture
def kernel(tmp_path: Path):
    env = _env(tmp_path)
    k = MoekaKernel.create(config_dict=_CFG, workspace=env.paths.work_dir, env=env)
    yield k
    k.cleanup()


def test_kernel_is_core():
    assert MoekaKernel is MoekaCore


def test_env_is_the_loops_environment(kernel):
    assert kernel.env is kernel.loop.env
    assert kernel.env.strict is True


def test_stores_are_lazy_and_land_under_state_dir(kernel):
    state = kernel.env.paths.state_dir
    assert not (state / FACTS_DB_FILENAME).exists()
    assert not (state / ARTIFACTS_DB_FILENAME).exists()
    facts = kernel.facts
    arts = kernel.artifacts
    assert isinstance(facts, FactStore) and isinstance(arts, ArtifactStore)
    assert facts.path == state / FACTS_DB_FILENAME
    assert arts.path == state / ARTIFACTS_DB_FILENAME
    # One fact store, shared, and the same object on every access.
    assert arts.facts is facts
    assert kernel.facts is facts and kernel.artifacts is arts
    work = kernel.env.paths.work_dir
    assert not any(work.rglob(FACTS_DB_FILENAME)) and not any(work.rglob(ARTIFACTS_DB_FILENAME))


def test_propose_and_answer_passthrough(kernel):
    kernel.artifacts.register_kind("server", Server)
    tid = kernel.facts.record("document", "inv.md", "web-01", span="0:6")
    res = kernel.propose("server", {"hostname": "web-01", "os": "debian"}, {"hostname": tid})
    assert res.committed == {"hostname": tid}
    assert res.provisional == ("os",)
    q = Question(kind="server", artifact_id=res.artifact_id, path="os",
                 prompt="?", proposed="debian")
    out = kernel.answer(q, "Debian 12", "sess:3")
    assert kernel.artifacts.committed(res.artifact_id)["os"] == "Debian 12"
    fact = kernel.facts.resolve(out.committed["os"])
    assert fact.source_kind == "user" and fact.source_ref == "sess:3"


def test_propose_artifact_id_is_keyword(kernel):
    kernel.artifacts.register_kind("server", Server)
    res = kernel.propose("server", {"hostname": "a"}, artifact_id="art-fixed")
    assert res.artifact_id == "art-fixed"


def test_cleanup_closes_built_stores_and_rebuilds_on_next_use(kernel):
    facts = kernel.facts
    kernel.artifacts
    kernel.cleanup()
    assert facts._conn_obj is None
    # A fresh store is built on next access; the data persisted.
    assert kernel.facts is not facts


def test_injected_stores_are_used_and_not_closed(tmp_path, kernel):
    own = FactStore(tmp_path / "elsewhere")
    arts = ArtifactStore(tmp_path / "elsewhere", own)
    try:
        kernel.facts = own
        kernel.artifacts = arts
        assert kernel.facts is own and kernel.artifacts is arts
        kernel.cleanup()
        assert own._conn_obj is not None
        assert kernel.facts is own
    finally:
        arts.close()
        own.close()


def test_injected_artifacts_adopt_their_fact_store(tmp_path, kernel):
    own = FactStore(tmp_path / "elsewhere")
    arts = ArtifactStore(tmp_path / "elsewhere", own)
    try:
        kernel.artifacts = arts
        assert kernel.facts is own
    finally:
        arts.close()
        own.close()


def test_injected_artifacts_over_another_injected_fact_store_refused(tmp_path, kernel):
    mine = FactStore(tmp_path / "mine")
    other = FactStore(tmp_path / "other")
    arts = ArtifactStore(tmp_path / "other", other)
    try:
        kernel.facts = mine
        with pytest.raises(ValueError, match="fact store"):
            kernel.artifacts = arts
        with pytest.raises(ValueError, match="fact store"):
            kernel.artifacts = arts
            kernel.facts = other  # pragma: no cover - never reached
    finally:
        arts.close()
        other.close()
        mine.close()


def test_injected_fact_store_mismatching_injected_artifacts_refused(tmp_path, kernel):
    own = FactStore(tmp_path / "own")
    arts = ArtifactStore(tmp_path / "own", own)
    other = FactStore(tmp_path / "other")
    try:
        kernel.artifacts = arts
        with pytest.raises(ValueError, match="fact store"):
            kernel.facts = other
        assert kernel.facts is own
    finally:
        arts.close()
        own.close()
        other.close()


def test_injection_replaces_kernel_built_stores(tmp_path, kernel):
    built_facts = kernel.facts
    built_arts = kernel.artifacts
    own = FactStore(tmp_path / "own")
    arts = ArtifactStore(tmp_path / "own", own)
    try:
        kernel.artifacts = arts  # kernel-built facts are swapped for the store's own
        assert kernel.facts is own and kernel.artifacts is arts
        assert built_facts._conn_obj is None and built_arts._conn_obj is None
    finally:
        arts.close()
        own.close()


def test_assigning_facts_drops_a_kernel_built_artifact_store(tmp_path, kernel):
    built_arts = kernel.artifacts
    own = FactStore(tmp_path / "own")
    try:
        kernel.facts = own
        assert built_arts._conn_obj is None
        assert kernel.artifacts.facts is own  # rebuilt over the new fact store
    finally:
        kernel.cleanup()
        own.close()


def test_no_env_degrades_to_none_and_clear_errors():
    core = MoekaCore(SimpleNamespace())  # a hand-built loop without a CoreEnvironment
    assert core.env is None
    assert core.facts is None
    assert core.artifacts is None
    with pytest.raises(RuntimeError, match="CoreEnvironment"):
        core.propose("server", {"hostname": "a"}, {})
    q = Question(kind="server", artifact_id="a", path="os", prompt="?", proposed=1)
    with pytest.raises(RuntimeError, match="CoreEnvironment"):
        core.answer(q, 1, "t")
    core.cleanup()  # still a no-op-safe teardown


def test_default_create_uses_legacy_env_under_its_workspace(tmp_path):
    """No explicit env: the loop's LegacyEnvironment (flat layout) backs the stores."""
    with MoekaKernel.scoped(config_dict=_CFG, workspace=tmp_path) as k:
        assert k.env is not None and k.env.strict is False
        assert k.facts.path == k.env.paths.state_dir / FACTS_DB_FILENAME
        assert k.facts.path.is_relative_to(tmp_path.resolve())
