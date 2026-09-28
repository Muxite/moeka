"""Task 25: P5 end to end (design I3 / section 7) through ``MoekaKernel``.

One story, one kernel, a scripted fake provider (no network):

1. A strict ``CoreEnvironment`` (separate ``work_dir``/``state_dir``, recording trace)
   backs a ``MoekaKernel``; ``kernel.facts``/``kernel.artifacts`` are the real on-disk
   stores under ``state_dir``.
2. The host registers an artifact kind, ingests an inventory document and records the
   spans it extracted as ``document`` facts.
3. A real agent turn runs: the fake model calls a host action that drafts the artifact
   through ``kernel.propose``. The cited leaf (``hostname``) commits; the uncited leaf
   (``os``) stays provisional; a forged cite is refused with nothing stored.
4. The epistemic audit (``clarify.resolve_divergence``): a formatting-only difference
   (``hostname``) commits the document's value without a question; a semantic one
   (``port``: document 8080, draft 80) and the unsupported ``os`` each give ONE
   question.
5. The user answers through ``kernel.answer``: each answer becomes a ``user`` fact and
   commits the leaf; every committed cite resolves through ``kernel.facts`` to the right
   provenance, the completed artifact validates, and a reopened store sees the same.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from nanobot.config.schema import Config
from nanobot.kernel import MoekaKernel
from nanobot.kernel.artifacts import ArtifactStore, CitationError
from nanobot.kernel.clarify import CommitReady, Divergence, Question, resolve_divergence
from nanobot.kernel.env import CoreEnvironment, Paths, StaticCredentialResolver
from nanobot.kernel.facts import FactStore
from nanobot.kernel.legacy import LegacyEnvironment
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest

# Task 12: MoekaKernel (MoekaCore) is now a deprecation shim (behaviour unchanged)
# — this file intentionally exercises it directly; allow the warning here.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

INVENTORY = """\
# Inventory
host: web-01
listen port: 8080
"""


class ServerSpec(BaseModel):
    hostname: str
    port: int
    os: str


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))

    def of(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("event") == name]


def _strict_env(tmp_path: Path, sink: RecordingSink) -> CoreEnvironment:
    work, state = tmp_path / "work", tmp_path / "state"
    work.mkdir()
    state.mkdir()
    cfg = Config.model_validate({"agents": {"defaults": {"workspace": str(work)}}})
    return CoreEnvironment(
        config=LegacyEnvironment.from_config(cfg).config,
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=work, state_dir=state),
        trace=sink,
        strict=True,
    )


def _fake_provider(script: list[LLMResponse]) -> MagicMock:
    provider = MagicMock(spec=LLMProvider)
    provider.generation = GenerationSettings()
    provider.supports_progress_deltas = False
    provider.get_default_model.return_value = "fake-model"
    calls = {"n": 0}

    async def chat_stream_with_retry(**kwargs: Any) -> LLMResponse:
        step = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        return step

    provider.chat_stream_with_retry = chat_stream_with_retry
    return provider


def _span(doc: str, needle: str) -> str:
    start = doc.index(needle)
    return f"{start}:{start + len(needle)}"


@pytest.mark.asyncio
async def test_p5_story_end_to_end(tmp_path: Path) -> None:
    sink = RecordingSink()
    env = _strict_env(tmp_path, sink)

    # -- 2. host: register a kind, ingest a document, record its spans as facts ----
    ingest_ref = "doc:inventory.md"

    # The model drafts: hostname in a different case (cited), port 80 (uncited, and
    # the document says 8080), os "debian" (uncited: nothing supports it).
    script = [
        LLMResponse(content="", tool_calls=[ToolCallRequest(
            id="c1", name="draft_server",
            arguments={"hostname": "WEB-01", "port": 80, "os": "debian"},
        )]),
        LLMResponse(content="Drafted the server spec.", tool_calls=[]),
    ]
    kernel = MoekaKernel.create(
        config_dict={"providers": {"openrouter": {"apiKey": "sk-test-key"}}},
        workspace=env.paths.work_dir, provider=_fake_provider(script), env=env,
    )
    try:
        assert kernel.env is env and kernel.env.strict
        kernel.artifacts.register_kind("server", ServerSpec)
        # Chunks indexed (0 without moeka[vec]); provenance does not depend on the index.
        assert kernel.ingest_text(INVENTORY, source=ingest_ref) >= 0
        host_tid = kernel.facts.record(
            "document", ingest_ref, "web-01", span=_span(INVENTORY, "web-01"))
        port_tid = kernel.facts.record(
            "document", ingest_ref, 8080, span=_span(INVENTORY, "8080"))

        # -- 3. a real agent turn drafts the artifact through kernel.propose -------
        drafted: dict[str, str] = {}

        @kernel.action
        def draft_server(hostname: str, port: int, os: str) -> str:
            """Draft the server spec artifact from the inventory."""
            result = kernel.propose(
                "server", {"hostname": hostname, "port": port, "os": os},
                {"hostname": host_tid},
            )
            drafted["id"] = result.artifact_id
            return f"drafted {result.artifact_id}"

        run = await kernel.run("Draft the server spec from the inventory.")
        assert "draft_server" in run.tools_used
        assert run.content == "Drafted the server spec."
        aid = drafted["id"]

        arts = kernel.artifacts
        assert arts.committed(aid) == {"hostname": "WEB-01"}
        assert arts.citations(aid) == {"hostname": host_tid}
        assert arts.provisional(aid) == {"port": 80, "os": "debian"}

        # A forged cite is refused outright; nothing is stored.
        with pytest.raises(CitationError):
            kernel.propose("server", {"os": "debian"}, {"os": "fact-" + "0" * 32},
                           artifact_id=aid)
        assert arts.provisional(aid) == {"port": 80, "os": "debian"}

        # -- 4. epistemic audit, one divergence at a time ---------------------------
        host = resolve_divergence(Divergence(
            kind="server", artifact_id=aid, path="hostname", proposed="WEB-01",
            known="web-01", known_trace_id=host_tid,
        ))
        assert isinstance(host, CommitReady)  # formatting only: no question
        kernel.propose(host.kind, host.delta, host.cites, artifact_id=host.artifact_id)
        assert arts.committed(aid)["hostname"] == "web-01"  # the source's spelling

        port_q = resolve_divergence(Divergence(
            kind="server", artifact_id=aid, path="port", proposed=80,
            known=8080, known_trace_id=port_tid,
        ))
        os_q = resolve_divergence(Divergence(
            kind="server", artifact_id=aid, path="os", proposed="debian",
        ))
        assert isinstance(port_q, Question) and port_q.path == "port"
        assert "8080" in port_q.prompt and "80" in port_q.prompt
        assert isinstance(os_q, Question) and os_q.path == "os"
        # Still provisional until the user answers.
        assert "port" not in arts.committed(aid) and "os" not in arts.committed(aid)

        # -- 5. the user answers; each answer commits with user provenance ----------
        kernel.answer(port_q, 80, "core:default:turn-2")
        kernel.answer(os_q, "Debian 12", "core:default:turn-3")

        assert arts.committed(aid) == {"hostname": "web-01", "port": 80, "os": "Debian 12"}
        assert arts.provisional(aid) == {}
        spec = arts.committed_model(aid)
        assert spec == ServerSpec(hostname="web-01", port=80, os="Debian 12")

        cites = arts.citations(aid)
        provenance = {
            path: (rec.source_kind, rec.source_ref, rec.value)
            for path, tid in cites.items()
            if (rec := kernel.facts.resolve(tid)) is not None
        }
        assert provenance == {
            "hostname": ("document", ingest_ref, "web-01"),
            "port": ("user", "core:default:turn-2", 80),
            "os": ("user", "core:default:turn-3", "Debian 12"),
        }
        assert kernel.facts.resolve(cites["hostname"]).span == _span(INVENTORY, "web-01")

        # Trace: fact and artifact events, values never on the trace.
        assert len(sink.of("fact.recorded")) == 4  # 2 document + 2 user
        proposed = sink.of("artifact.proposed")
        assert len(proposed) == 4  # draft, hostname commit, two answers
        assert proposed[0]["committed"] == {"hostname": host_tid}
        assert sorted(proposed[0]["provisional"]) == ["os", "port"]
        assert [e["reason"] for e in sink.of("artifact.rejected")] == ["CitationError"]
        assert "Debian 12" not in repr(sink.events)

        # Both stores live under state_dir; nothing kernel-owned in work_dir.
        assert kernel.facts.path.parent == env.paths.state_dir
        assert kernel.artifacts.path.parent == env.paths.state_dir
        assert not list(env.paths.work_dir.rglob("facts.db*"))
        assert not list(env.paths.work_dir.rglob("artifacts.db*"))
    finally:
        kernel.cleanup()

    # -- durability: a fresh process view of the same state_dir agrees -------------
    with FactStore.from_env(env) as facts, ArtifactStore.from_env(env, facts=facts) as again:
        again.register_kind("server", ServerSpec)
        assert again.committed_model(aid) == spec
        assert all(facts.resolve(t) is not None for t in again.citations(aid).values())
