"""A social-coach turn: a coach suggests a line, simulated counterparts answer it.

The user describes a situation ("I need to ask my manager Dana to move a
deadline"). The flow:

1. The user's statements about Dana become ``user`` facts (``kernel.epistemics``),
   and a ``counterpart`` dossier commits the leaves those facts support.
2. A coach agent (``kernel.agent``) suggests an opening line in the user's session.
3. The rehearsal conversation is checkpointed, then forked three times; a persona
   (plain ``kernel.llm``, not an agent) answers the line in each fork as the best,
   likely and worst case, fanned out with ``kernel.llm.batch`` at different seeds
   and temperatures and typed by a pydantic model.
4. Each persona states the assumptions it made about Dana. One that matches a
   known fact commits silently; one no fact supports becomes ONE clarifying
   question, and the user's answer commits with ``user`` provenance.

Runs offline: a ``FakeProvider`` answers for both model aliases (no network, no
key, no model download).

    python examples/coach_sim.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from moeka import Environment, Kernel, ModelSpec, ProviderSpec, Sampling
from moeka.agents import AgentSpec
from moeka.epistemics import CommitReady, Divergence, FactRecord, Question
from moeka.llm import GenerateOptions, Request, system
from moeka.sessions import Session
from moeka.testing import FakeCall, FakeProvider


class Counterpart(BaseModel):
    """The dossier the app keeps per person (domain schema: the app's, not moeka's)."""

    name: str | None = None
    relationship: str | None = None
    prefers: str | None = None
    deadline_is_fixed: bool | None = None


class PersonaReply(BaseModel):
    """What a simulated counterpart returns: the line and what it had to assume."""

    reply: str
    assumes: dict[str, Any] = {}


CASES = {  # case -> (sampling, framing)
    "best": (Sampling(temperature=0.3, seed=1), "Answer as Dana on a good day."),
    "likely": (Sampling(temperature=0.7, seed=2), "Answer as Dana most likely would."),
    "worst": (Sampling(temperature=1.0, seed=3), "Answer as Dana on her worst day."),
}

LOCAL = dict(provider="vllm", tier="local")  # tier=local with no prices: cost 0
COACH = ModelSpec(name="coach", model="qwen3-32b", **LOCAL)
PERSONA = ModelSpec(name="persona", model="qwen3-8b", **LOCAL)

OPENING = "Dana, could we move Friday's report to Tuesday so I can include the audit numbers?"
PERSONA_REPLIES = {
    "good day": {"reply": "Sure, Tuesday works. Send me the outline Thursday.",
                 "assumes": {"relationship": "My manager"}},
    "most likely": {"reply": "Tuesday is tight. Can you send a draft Friday and the rest Tuesday?",
                    "assumes": {"relationship": "my  manager"}},
    "worst day": {"reply": "No. The client already has Friday; that date is fixed.",
                  "assumes": {"deadline_is_fixed": True}},
}


def fake_model(call: FakeCall) -> str:
    """Stand-in for both models: the coach suggests a line; a persona answers in JSON."""
    first = str(call.messages[0].get("content", ""))
    for mood, answer in PERSONA_REPLIES.items():
        if mood in first:
            return json.dumps(answer)
    return f'Try opening with: "{OPENING}"'


def persona_messages(convo: Session, framing: str, known: list[FactRecord]) -> list[dict[str, Any]]:
    """A persona prompt over a (forked) rehearsal transcript and the facts on Dana."""
    facts = "\n".join(f"- {f.source_ref}: {f.value}" for f in known)
    lines = [f"{m.get('name', m['role'])}: {m['content']}" for m in convo.messages]
    return [
        system(f"You play Dana. {framing}\nKnown about Dana:\n{facts}\n"
               'Reply as JSON {"reply": str, "assumes": {dossier field: value}}.'),
        {"role": "user", "content": "\n".join(lines)},
    ]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="moeka-coach-") as tmp:
        root = Path(tmp)
        env = Environment.for_host(
            state_dir=root / "state", work_dir=root / "work", credentials={},
            providers=[ProviderSpec(name="vllm", api_base="http://127.0.0.1:8000/v1")],
            models=[COACH, PERSONA], default_model="coach",
        )
        with Kernel(env) as kernel:
            fake = FakeProvider(default=fake_model)
            for spec in (COACH, PERSONA):
                kernel.llm.register_provider(spec.name, fake, spec)
            epi = kernel.epistemics

            # 1. What the user said about Dana, with provenance. The subject lives in
            #    the ref prefix: facts(ref_prefix="person:dana/") is all we know.
            name = epi.record_fact("Dana", source="user", ref="person:dana/name")
            rel = epi.record_fact("my manager", source="user", ref="person:dana/relationship")
            epi.record_fact("written updates", source="user", ref="person:dana/prefers")
            known = epi.facts(ref_prefix="person:dana/")
            epi.register_kind("counterpart", Counterpart)
            dossier = epi.propose(
                "counterpart",
                {"name": "Dana", "relationship": "my manager", "prefers": "written updates"},
                cites={"name": name, "relationship": rel},  # prefers: left provisional
            ).artifact_id

            # 2. The coach suggests an opening line in the user's session.
            coach = kernel.agent(AgentSpec(
                name="coach", model="coach", tools_allow=(),
                system_prompt="You coach the user through a hard conversation. Suggest "
                              "one short line at a time.",
            ))
            chat = kernel.sessions.create_sync("coach:demo")
            advice = coach.run_sync(
                "I need to ask my manager Dana to move Friday's deadline.", session=chat,
            )
            if advice.stop_reason != "completed":
                print(f"coach stopped: {advice.stop_reason} {advice.error}")
                return 1
            line = advice.content.split('"')[1]

            # 3. Rehearse: the user's line, checkpointed, then one fork per case.
            convo = kernel.sessions.create_sync("coach:demo/rehearsal")
            at = convo.append_sync({"role": "user", "name": "me", "content": line})
            forks = {case: convo.fork_sync(at, key=f"coach:demo/rehearsal/{case}")
                     for case in CASES}
            outcome = kernel.llm.batch_sync([
                Request(
                    messages=persona_messages(forks[case], framing, known),
                    model_cls=PersonaReply, retries=1,
                    opts=GenerateOptions(model="persona", sampling=sampling,
                                         tags={"case": case}),
                )
                for case, (sampling, framing) in CASES.items()
            ])
            if outcome.systemic is not None or outcome.errors:
                print(f"persona batch failed: {outcome.systemic or outcome.errors}")
                return 1
            replies: dict[str, PersonaReply] = {}
            for case, done in zip(CASES, outcome.outcomes, strict=True):
                replies[case] = done.parsed
                forks[case].append_sync(
                    {"role": "assistant", "name": "dana", "content": done.parsed.reply})

            # 4. Audit what the personas assumed about Dana against the dossier.
            question: Question | None = None
            committed = epi.artifact("counterpart", dossier)
            cites = epi.citations("counterpart", dossier)
            for case, reply in replies.items():
                for path, proposed in reply.assumes.items():
                    known_id = cites.get(path)
                    decision = epi.reconcile(Divergence(
                        kind="counterpart", artifact_id=dossier, path=path, proposed=proposed,
                        known=committed.get(path), known_trace_id=known_id,
                    ))
                    if isinstance(decision, CommitReady):  # formatting only: keep the fact
                        epi.propose("counterpart", decision.delta, cites=decision.cites,
                                    artifact_id=dossier)
                    elif question is None:  # one question per ambiguity, one at a time
                        question = decision
            if question is None:
                print("expected the worst case to rest on an unsupported assumption")
                return 1
            # The app would show question.prompt and wait; the user answers "no".
            epi.answer(question, False, turn_ref="coach:demo:turn-2")
            final = epi.artifact_model("counterpart", dossier)

            print(f"coach: {line}")
            for case, reply in replies.items():
                sampling = CASES[case][0]
                print(f"  {case:<6} (t={sampling.temperature}, seed={sampling.seed}): "
                      f"{reply.reply}")
            print(f"forks: {', '.join(sorted(f.key for f in forks.values()))}; "
                  f"rehearsal still has {len(convo.messages)} message(s)")
            print(f"asked: {question.prompt}")
            print(f"dossier: {final.model_dump()}  provisional: "
                  f"{sorted(epi.provisional('counterpart', dossier))}")
            if final.deadline_is_fixed is not False or len(convo.messages) != 1:
                return 1
    return 0


if __name__ == "__main__":
    from loguru import logger

    logger.disable("nanobot")  # the agent loop logs every turn stage at DEBUG
    raise SystemExit(main())
