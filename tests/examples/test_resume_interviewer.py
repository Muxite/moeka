"""The resume interviewer example (examples/resume_interviewer), offline.

A fake ``awr`` executable returns canned ``--json`` envelopes and writes canned
``report.json`` files (fictional data only); a ``FakeProvider`` plays the model by
reading the conversation. Covered: the cap of 5, kind and truth lint, awr's own
questions never answered, ``ask_user`` stop then resume in the same session (also
across processes), the batch out/in round trip (notes doc + diff), the state dir
guard, and a budget refusal.
"""

from __future__ import annotations

import io
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from moeka.testing import FakeCall, FakeProvider
from nanobot.providers.base import LLMResponse, LLMUsage, ToolCallRequest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.resume_interviewer import questions as qmod  # noqa: E402
from examples.resume_interviewer.cli import main  # noqa: E402
from examples.resume_interviewer.host import (  # noqa: E402
    TOOLS,
    Interviewer,
    RunState,
    Settings,
    guard_state_dir,
)
from examples.resume_interviewer.notes import report_diff, write_notes  # noqa: E402

logger.disable("nanobot")

# -- the fake awr ------------------------------------------------------------------------

REPORT_V1 = {
    "schema_version": "1.0", "job_id": "job",
    "posting": {"title": "Platform Engineer (fictional)", "company": "Quillmark (fictional)",
                "requirements": [
                    {"id": "req-k8s", "kind": "must",
                     "text": "Experience running services on Kubernetes"},
                    {"id": "req-py", "kind": "must", "text": "Python programming"},
                    {"id": "req-prom", "kind": "nice", "text": "Monitoring with Prometheus"},
                ]},
    "pages": 1, "preset": "standard", "compiles": 1,
    "bullets": [
        {"fact_key": "tern/build:-:api", "entry": "tern-tracker_2025", "status": "ok",
         "flags": [], "text": "Built a Python API for logging bird sightings.",
         "draft": "Built a Python API for logging bird sightings."},
        {"fact_key": "tern/cut:latency:ingest", "entry": "tern-tracker_2025", "status": "ok",
         "flags": [], "text": "Cut ingest time from 40 s to 9 s by batching writes."},
        {"fact_key": "gull/proto:-:bot", "entry": "gullwing-hackathon_2024",
         "status": "stronger", "flags": ["stronger_than_source"],
         "text": "Prototyped a Python alerting bot at a weekend hackathon, handling 300 alerts."},
    ],
    "blocked": [{"fact_key": "tern/scale:-:users", "entry": "tern-tracker_2025",
                 "draft": "Scaled the service to 10,000 daily users.",
                 "flags": ["never_measured"]}],
    "dropped_for_fit": [], "flag_counts": {"stronger_than_source": 1},
    "uncovered_must": [{"id": "req-k8s", "text": "Experience running services on Kubernetes"}],
    "header_placeholders": ["phone"], "cost_usd": 0.0, "calls": 9, "cache_hits": 0,
    "cache_misses": 9, "usage": {}, "measurements": {}, "omitted_sections": [],
}
K8S_BULLET = {"fact_key": "tern/ran:-:k8s", "entry": "tern-tracker_2025", "status": "ok",
              "flags": [], "text": "Ran the tern-tracker API on a three-node Kubernetes cluster."}

FAKE_AWR = textwrap.dedent('''\
    import json, sys, pathlib
    argv = sys.argv[1:]
    cmd = argv[0]
    ws = pathlib.Path(argv[argv.index("--workspace") + 1])
    ws.mkdir(parents=True, exist_ok=True)
    with open(ws / "fake_awr_calls.jsonl", "a") as log:
        log.write(json.dumps(argv) + "\\n")
    base = json.loads(pathlib.Path(__file__).with_name("report_v1.json").read_text())
    def env(command, code, data, error=None):
        print(json.dumps({"schema_version": "1.0", "command": command,
                          "status": {0: "ok", 4: "needs_user"}.get(code, "error"),
                          "exit_code": code, "error": error, "retryable": False,
                          "hint": None, "data": data}))
        sys.exit(code)
    if (ws / "BROKEN").exists():
        env(cmd, 3, None, {"code": "provider_down", "message": "ollama is down"})
    if cmd == "build":
        n = len(list((ws / "out").glob("job-*"))) + 1 if (ws / "out").exists() else 1
        out = ws / "out" / f"job-{n}"
        out.mkdir(parents=True)
        report = dict(base)
        notes = "".join(p.read_text() for p in (ws / "docs").glob("answers-*.md")) \\
            if (ws / "docs").exists() else ""
        if "Kubernetes" in notes:
            report["uncovered_must"] = []
            report["bullets"] = base["bullets"] + [K8S]
        (out / "report.json").write_text(json.dumps(report))
        env("build", 4, {"job_id": f"job-{n}", "report_path": str(out / "report.json"),
                         "needs_user": [{"kind": "question", "id": "header.phone"}],
                         "questions_open": 2})
    if cmd == "questions":
        env("questions", 0, {"questions": [
            {"id": "header.phone", "kind": "header", "text": "What phone number?"},
            {"id": "confirm.v1", "kind": "confirm", "text": "Did you lead tern-tracker?",
             "options": ["yes", "no"]}], "deferred": [], "open": 2, "cap": 5})
    env(cmd, 0, {"answered": []})
''').replace("[K8S]", "[" + json.dumps(K8S_BULLET) + "]")


@pytest.fixture
def fake_awr(tmp_path: Path) -> list[str]:
    bindir = tmp_path / "awrbin"
    bindir.mkdir()
    (bindir / "report_v1.json").write_text(json.dumps(REPORT_V1))
    script = bindir / "fake_awr.py"
    script.write_text(FAKE_AWR)
    return [sys.executable, str(script)]


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    ws = tmp_path / "W"
    (ws / "docs").mkdir(parents=True)
    posting = tmp_path / "posting.md"
    posting.write_text("# Platform Engineer (fictional)\nKubernetes, Python.\n")
    return {"ws": ws, "posting": posting, "home": tmp_path / "iv-home"}


def awr_calls(ws: Path) -> list[list[str]]:
    log = ws / "fake_awr_calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# -- the scripted model -------------------------------------------------------------------

PROPOSALS = [
    {"kind": "must_have_probe", "project": "",
     "text": "Did any of your projects run on Kubernetes?",
     "reason": "Kubernetes is a must-have the resume does not cover.", "plausibility": 0.6},
    {"kind": "unlock", "project": "tern-tracker_2025",
     "text": "Did you measure how many sightings the tern-tracker API logged?",
     "reason": "A number would make the API line a lead line.", "plausibility": 0.5},
    {"kind": "number_context", "project": "gullwing-hackathon_2024",
     "text": "Were the 300 alerts per day or in total?",
     "reason": "A number with its scale is believable.", "plausibility": 0.7},
    {"kind": "stage_scope", "project": "tern-tracker_2025",
     "text": "Did real birders use tern-tracker, and roughly how many?",
     "reason": "Real users show the work left the lab.", "plausibility": 0.4},
    {"kind": "unlock", "project": "tern-tracker_2025",
     "text": "Did you add Prometheus monitoring to tern-tracker?",
     "reason": "Monitoring is a nice-to-have of the posting.", "plausibility": 0.3},
    {"kind": "stage_scope", "project": "gullwing-hackathon_2024",
     "text": "Did anyone keep using the alerting bot after the hackathon?",
     "reason": "Use after the event shows real scope.", "plausibility": 0.2},
    # rejected: bad kind, leading, drops a qualifier, unknown project
    {"kind": "brag", "project": "", "text": "Anything else?", "reason": "More."},
    {"kind": "unlock", "project": "tern-tracker_2025",
     "text": "You should say you scaled it to 10,000 users, right?",
     "reason": "Bigger is better."},
    {"kind": "stage_scope", "project": "gullwing-hackathon_2024",
     "text": "Can we drop the word hackathon from the bot line?",
     "reason": "It sounds more professional."},
    {"kind": "unlock", "project": "made-up-co", "text": "Did you work at made-up-co?",
     "reason": "It is a known name."},
]


def _tool(name: str, args: dict[str, Any], n: int) -> LLMResponse:
    return LLMResponse(
        content="", finish_reason="tool_calls",
        tool_calls=[ToolCallRequest(id=f"call{n}", name=name, arguments=args)],
        usage=LLMUsage.reported(input_tokens=20, output_tokens=10),
    )


class ScriptedInterviewer:
    """Plays the model: build, awr questions, propose, then ask/write per answer."""

    def __init__(self, proposals: list[dict[str, Any]] = PROPOSALS, *,
                 rogue_ask: bool = False) -> None:
        self.proposals = proposals
        self.rogue_ask = rogue_ask
        self.n = 0

    def __call__(self, call: FakeCall) -> LLMResponse | str:
        self.n += 1
        msgs = call.messages
        tools_done = {m.get("name") for m in msgs if m.get("role") == "tool"}
        last = msgs[-1]
        if last.get("role") == "user" and str(last.get("content", "")).startswith(
                "Host: the user answered every question"):
            return "Summary: the answers were recorded and the resume was rebuilt."
        if "awr_build" not in tools_done:
            return _tool("awr_build", {}, self.n)
        if "awr_questions" not in tools_done:
            return _tool("awr_questions", {}, self.n)
        proposed = [m for m in msgs if m.get("role") == "tool"
                    and m.get("name") == "propose_questions"]
        if not proposed:
            return _tool("propose_questions", {"questions": self.proposals}, self.n)
        accepted = json.loads(proposed[-1]["content"])["accepted"]
        if self.rogue_ask:
            self.rogue_ask = False
            return _tool("ask_user", {"question": "What is your phone number?"}, self.n)
        answered = {}
        for m in msgs:
            content = str(m.get("content", ""))
            if m.get("role") == "user" and content.startswith("[q"):
                qid, _, rest = content.partition("] The user answered: ")
                answered[qid.strip("[")] = rest
        written = {json.loads(tc["function"]["arguments"])["question_id"]
                   for m in msgs if m.get("role") == "assistant"
                   for tc in m.get("tool_calls") or []
                   if tc["function"]["name"] == "write_answer_note"}
        for qid, answer in answered.items():
            if qid not in written:
                return _tool("write_answer_note", {"question_id": qid, "answer": answer}, self.n)
        for q in accepted:
            if q["id"] not in answered:
                return _tool("ask_user", {"question": f"[{q['id']}] {q['text']}"}, self.n)
        return "All five questions are answered; the host will rebuild."


def settings(paths: dict[str, Path], fake_awr: list[str], **kw: Any) -> Settings:
    return Settings(workspace=paths["ws"], posting=paths["posting"], awr_command=fake_awr,
                    home=paths["home"], **kw)


# -- question rules -----------------------------------------------------------------------


def test_kinds_and_lint_are_enforced() -> None:
    ok = {"kind": "unlock", "text": "Did you measure the speed-up?", "project": "",
          "reason": "A number makes the line stronger."}
    assert qmod.question_problems(ok) == []
    assert any("kind" in p for p in qmod.question_problems({**ok, "kind": "brag"}))
    assert qmod.question_problems({**ok, "text": "Did you? Really?"})
    assert qmod.question_problems({**ok, "text": "It was fast. Did you measure it?"})
    assert qmod.question_problems({**ok, "reason": ""})
    assert any("leads" in p for p in qmod.question_problems(
        {**ok, "text": "You should say it doubled throughput, right?"}))
    assert any("qualifier" in p for p in qmod.question_problems(
        {**ok, "text": "Can we call it a product instead of a prototype?"}))
    assert any("qualifier" in p for p in qmod.question_problems(
        {**ok, "text": "Could the line leave out paper trading?"}))
    assert qmod.question_problems({**ok, "project": "nope"}, ["tern-tracker_2025"])
    # mentioning a qualifier is fine; only asking to lose it is not
    assert qmod.question_problems(
        {**ok, "text": "Was the hackathon bot used after the event?"}) == []


def test_cap_of_five_is_enforced_even_if_asked_for_more() -> None:
    cands = [qmod.Candidate(kind="unlock", text=f"Did you measure result {i}?", project="",
                            reason="A number helps.", plausibility=0.1 * (i + 1))
             for i in range(8)]
    chosen, rejected = qmod.select(cands, [], cap=9)
    assert [q.id for q in chosen] == ["q1", "q2", "q3", "q4", "q5"]
    assert chosen[0].text == "Did you measure result 7?"  # ranked by score
    assert sum("over the cap" in r["problems"][0] for r in rejected) == 3


def test_report_candidates_are_valid_and_ranked() -> None:
    cands = qmod.candidates_from_report(REPORT_V1)
    kinds = {c.kind for c in cands}
    assert {"must_have_probe", "unlock", "number_context", "stage_scope"} <= kinds
    chosen, _ = qmod.select(cands, qmod.known_projects(REPORT_V1))
    assert 1 <= len(chosen) <= 5
    assert chosen[0].kind == "must_have_probe"  # the uncovered must-have ranks first
    for q in chosen:
        assert set(q.model_dump()) == {"id", "kind", "text", "project", "reason"}


def test_state_dir_never_under_nanobot(tmp_path: Path) -> None:
    for bad in (Path.home() / ".nanobot", Path.home() / ".nanobot" / "x",
                Path.home() / ".nanobot-sessions" / "y"):
        with pytest.raises(ValueError, match="live moeka state"):
            guard_state_dir(bad)
    assert guard_state_dir(tmp_path / "s") == (tmp_path / "s").resolve()
    out = io.StringIO()
    code = main(["--workspace", str(tmp_path), "--posting", str(tmp_path / "p"),
                 "--home", str(Path.home() / ".nanobot"), "--questions-out",
                 str(tmp_path / "q.jsonl")], stdout=out)
    assert code == 2 and "live moeka state" in out.getvalue()


# -- the agent ----------------------------------------------------------------------------


def test_agent_tools_are_exactly_the_allow_list(paths, fake_awr) -> None:
    with Interviewer(settings(paths, fake_awr), provider=FakeProvider(default="x")) as iv:
        names = {t.name for t in iv.agent.tools}
    assert names == set(TOOLS)
    assert "exec" not in names and not any("answer" == n for n in names)


def test_batch_round_trip_resumes_the_asking_session(paths, fake_awr, tmp_path) -> None:
    model = ScriptedInterviewer()
    q_out = tmp_path / "q.jsonl"
    fake = FakeProvider(default=model)
    code = main(["--workspace", str(paths["ws"]), "--posting", str(paths["posting"]),
                 "--awr", " ".join(fake_awr), "--home", str(paths["home"]),
                 "--run-id", "r1", "--questions-out", str(q_out)],
                provider=fake, stdout=io.StringIO())
    assert code == 0
    rows = [json.loads(line) for line in q_out.read_text().splitlines()]
    assert 1 <= len(rows) <= 5 and len(rows) == 5
    for row in rows:
        assert set(row) == {"id", "kind", "text", "project", "reason"}
        assert row["kind"] in qmod.KINDS and row["reason"]
    texts = " ".join(r["text"] for r in rows)
    assert "should say" not in texts and "drop the word" not in texts
    state = RunState.load(paths["home"] / "state" / "runs", None)
    assert state.pending_ask == "q1" and state.proposer == "model"
    assert {"header.phone", "confirm.v1"} == {q["id"] for q in state.awr_questions["questions"]}
    calls_before = len(fake.calls)

    # Part 2 in a fresh kernel (a new process in real use) on the same state dir.
    answers = tmp_path / "a.jsonl"
    by_kind = {r["kind"]: r["id"] for r in rows}
    answers.write_text("\n".join(json.dumps(a) for a in [
        {"id": by_kind["must_have_probe"],
         "answer": "Yes, tern-tracker ran on a three-node Kubernetes cluster."},
        {"id": by_kind["number_context"], "answer": "no"},
        {"id": "q99", "answer": "stray"},
    ]) + "\n")
    out = tmp_path / "result.json"
    fake2 = FakeProvider(default=model)
    code = main(["--workspace", str(paths["ws"]), "--awr", " ".join(fake_awr),
                 "--home", str(paths["home"]), "--answers-in", str(answers), "--out", str(out)],
                provider=fake2, stdout=io.StringIO())
    assert code == 0, out.read_text() if out.exists() else ""
    result = json.loads(out.read_text())

    # notes doc: dated, user-provided, traceable, "no" answers not written
    notes = Path(result["notes_doc"])
    assert notes.parent == paths["ws"] / "docs" and notes.name.startswith("answers-")
    text = notes.read_text()
    assert "three-node Kubernetes cluster" in text and "source=user" in text
    assert "user-provided" in text and "run=r1" in text
    assert "300 alerts" not in text
    written = {a["id"]: a for a in result["answers"]}
    assert written[by_kind["number_context"]]["written"] is False
    assert all(a["fact_id"].startswith("fact-") for a in result["answers"])
    assert result["ignored_answers"][0]["id"] == "q99"

    # diff: the must-have is resolved and the new bullet is there
    diff = result["diff"]
    assert diff["uncovered_must"]["resolved"] == ["Experience running services on Kubernetes"]
    assert [b["fact_key"] for b in diff["bullets"]["added"]] == ["tern/ran:-:k8s"]

    # ask_user resume: the summary run continued the session that asked q1
    assert result["agent_summary"].startswith("Summary:")
    resumed = fake2.calls[-1].messages
    asks = [tc for m in resumed if m.get("role") == "assistant"
            for tc in m.get("tool_calls") or [] if tc["function"]["name"] == "ask_user"]
    assert len(asks) == 1 and "[q1]" in asks[0]["function"]["arguments"]
    ask_result = [m for m in resumed if m.get("tool_call_id") == asks[0]["id"]]
    assert len(ask_result) == 1  # the dangling call is closed before the next turn
    assert resumed[-1]["role"] == "user" and "answered every question" in resumed[-1]["content"]
    assert calls_before >= 4

    # the user answers are user facts in the kernel's epistemics
    st = Settings(workspace=paths["ws"], posting=paths["posting"], awr_command=fake_awr,
                  home=paths["home"])
    with Interviewer(st, provider=FakeProvider(default="x"),
                     state=RunState.load(st.runs_dir, "r1")) as iv:
        facts = iv.kernel.epistemics.facts(ref_prefix="resume-interviewer:r1/", source="user")
    assert len(facts) == 2

    # awr's own questions were listed, never answered
    assert not any(c[0] == "answer" for c in awr_calls(paths["ws"]))
    assert result["awr_questions"]["questions"][0]["id"] == "header.phone"


def test_interactive_relays_ask_user_and_resumes(paths, fake_awr) -> None:
    model = ScriptedInterviewer(rogue_ask=True)
    fake = FakeProvider(default=model)
    answers = iter(["Yes, on Kubernetes at a club project.", "About 2,000 sightings.",
                    "Total.", "no", "no"])
    asked: list[str] = []

    def ask(q: qmod.Question) -> str:
        asked.append(q.id)
        return next(answers)

    with Interviewer(settings(paths, fake_awr, run_id="i1"), provider=fake) as iv:
        result = iv.interview(ask)
        stops = [s["stop_reason"] for s in iv.state.agent_stops]
    assert asked == ["q1", "q2", "q3", "q4", "q5"]  # the rogue question never reached the user
    assert stops.count("ask_user") >= 5 and stops[-1] == "completed"
    # the agent wrote each content answer with write_answer_note, verbatim
    notes = Path(result["notes_doc"]).read_text()
    assert "About 2,000 sightings." in notes and notes.count("<!-- resume-interviewer") == 3
    assert result["diff"]["uncovered_must"]["after"] == []
    assert not any(c[0] == "answer" for c in awr_calls(paths["ws"]))
    builds = [c for c in awr_calls(paths["ws"]) if c[0] == "build"]
    assert len(builds) == 2  # one baseline (cached across awr_build calls) + one rebuild


def test_write_answer_note_refuses_words_the_user_did_not_say(paths, fake_awr) -> None:
    with Interviewer(settings(paths, fake_awr), provider=FakeProvider(default="x")) as iv:
        iv.awr_build()
        iv.propose_questions(PROPOSALS[:2])
        assert "has not answered" in iv.write_answer_note("q1", "Yes, 50 clusters.")
        iv.record_answer(iv.accepted()[0], "Yes, one cluster.")
        assert "exactly" in iv.write_answer_note("q1", "Yes, fifty clusters.")
        assert "Written" in iv.write_answer_note("q1", "Yes, one cluster.")


def test_budget_refusal_falls_back_or_fails_cleanly(paths, fake_awr, tmp_path) -> None:
    fake = FakeProvider(default=ScriptedInterviewer())
    with Interviewer(settings(paths, fake_awr, token_cap=1, run_id="b1"), provider=fake) as iv:
        outcome = iv.questions_out(tmp_path / "q.jsonl")
    assert outcome["ok"] and outcome["proposer"].startswith("code-fallback (budget)")
    assert outcome["agent_stops"][0]["stop_reason"] == "budget"
    assert len(fake.calls) == 0  # refused before any provider call
    rows = (tmp_path / "q.jsonl").read_text().splitlines()
    assert 1 <= len(rows) <= 5

    out = io.StringIO()
    code = main(["--workspace", str(paths["ws"]), "--posting", str(paths["posting"]),
                 "--awr", " ".join(fake_awr), "--home", str(tmp_path / "h2"),
                 "--token-cap", "1", "--no-fallback", "--questions-out",
                 str(tmp_path / "q2.jsonl")], provider=FakeProvider(default="x"), stdout=out)
    assert code == 5 and "budget" in out.getvalue()


def test_awr_infra_error_is_reported(paths, fake_awr) -> None:
    (paths["ws"] / "BROKEN").write_text("")
    out = io.StringIO()
    code = main(["--workspace", str(paths["ws"]), "--posting", str(paths["posting"]),
                 "--awr", " ".join(fake_awr), "--home", str(paths["home"]), "--no-fallback",
                 "--questions-out", str(paths["home"] / "q.jsonl")],
                provider=FakeProvider(default=ScriptedInterviewer()), stdout=out)
    assert code in (3, 5)
    assert "provider_down" in out.getvalue() or "no questions" in out.getvalue()


def test_notes_are_idempotent_and_diff_is_keyed(tmp_path) -> None:
    row = {"id": "q1", "kind": "unlock", "text": "Did you measure it?", "project": "p",
           "answer": "Yes, 3x.", "fact_id": "fact-1"}
    p1 = write_notes(tmp_path, "r", [row])
    first = p1.read_text()
    assert write_notes(tmp_path, "r", [row]).read_text() == first
    write_notes(tmp_path, "r", [{**row, "answer": "Yes, 4x."}])
    assert "4x" in p1.read_text() and "3x" not in p1.read_text()
    assert write_notes(tmp_path, "r", [{**row, "answer": "no"}]) is None
    after = {**REPORT_V1, "bullets": REPORT_V1["bullets"][:2] + [K8S_BULLET], "blocked": []}
    diff = report_diff(REPORT_V1, after)
    assert [b["fact_key"] for b in diff["bullets"]["added"]] == ["tern/ran:-:k8s"]
    assert [b["fact_key"] for b in diff["bullets"]["removed"]] == ["gull/proto:-:bot"]
    assert len(diff["blocked"]["removed"]) == 1


def test_no_state_written_to_real_home(paths, fake_awr) -> None:
    fake = FakeProvider(default=ScriptedInterviewer())
    with Interviewer(settings(paths, fake_awr), provider=fake) as iv:
        iv.questions_out(paths["home"] / "q.jsonl")
        state_dir = iv.settings.state_dir
    assert state_dir.is_relative_to(paths["home"].resolve())
    assert (state_dir / "runs").is_dir()
    assert os.environ.get("HOME") is None or not str(state_dir).startswith(
        str(Path.home() / ".nanobot"))


def test_quotes_are_never_cut_mid_number() -> None:
    long = ("Designed a constraint-based shift scheduler in Python for a volunteer roster "
            "of a community kitchen that plans 300 shifts in under 2 seconds.")
    short = qmod._short(long, limit=110)
    assert short.endswith(" ...") and not any(ch.isdigit() for ch in short.split()[-2])
    ok = {"kind": "number_context", "project": "", "reason": "Context helps.",
          "text": 'What was the baseline in "plans 300 shifts in under 2..."?'}
    assert any("after a number" in p for p in qmod.question_problems(ok))


def test_question_asked_as_plain_text_counts_as_the_ask(paths, fake_awr, tmp_path) -> None:
    """Small local models often end the turn with the question as text, not ask_user."""
    script = ScriptedInterviewer()

    def model(call: FakeCall) -> LLMResponse | str:
        reply = script(call)
        if isinstance(reply, LLMResponse) and reply.tool_calls[0].name == "ask_user":
            return reply.tool_calls[0].arguments["question"]
        return reply

    with Interviewer(settings(paths, fake_awr, run_id="t1"),
                     provider=FakeProvider(default=model)) as iv:
        outcome = iv.questions_out(tmp_path / "q.jsonl")
        assert iv.state.pending_ask == "q1"
    assert outcome["agent_stops"][-1]["stop_reason"] == "completed"
    assert outcome["agent_stops"][-1]["asked_as_text"] == "q1"
