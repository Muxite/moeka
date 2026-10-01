"""The interviewer host: a moeka kernel agent that drives ``awr`` and asks the user.

Kernel set-up (strict): ``Environment.for_host`` with its own state dir (never
``~/.nanobot``), no credentials, one local Ollama provider, a token-capped
``SharedCapBudget`` per run, and one agent whose ``tools_allow`` is exactly
:data:`TOOLS`: host actions wrapping ``awr`` plus ``ask_user``. No exec, no file
tools, no web.

The model proposes; the host enforces. ``propose_questions`` lints, ranks and caps
what the model offers (:mod:`.questions`); ``ask_user`` calls are only relayed when
they name an accepted question; ``write_answer_note`` only writes the answer the user
actually gave. awr's own header and confirm questions are listed for the human and
never answered here.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from moeka import Environment, Kernel, ModelSpec, ProviderSpec
from moeka.agents import AgentSpec, RunLimits, RunResult
from moeka.budget import SharedCapBudget
from moeka.tools import FunctionTool

from . import questions as qmod
from .awr import AwrClient, AwrError
from .notes import is_content_answer, report_diff, report_summary, write_notes

AGENT_NAME = "resume-interviewer"
CONSUMER = "resume-interviewer"
MODEL_ALIAS = "interviewer"
TOOLS: tuple[str, ...] = (
    "awr_build", "awr_report", "awr_questions", "propose_questions", "write_answer_note",
    "ask_user",
)
DEFAULT_HOME = Path.home() / ".moeka-resume-interviewer"
MAX_STRIKES = 4  # host corrections (unaccepted ask_user, ...) before the host takes over

SYSTEM_PROMPT = """\
You are a resume interviewer. A job seeker's resume was built by the awr tool from
their own documents. You find the few facts the documents are missing that would make
the resume stronger for this posting, and ask the user about them.

Steps:
1. Call awr_build. It returns the report summary and ranked candidate questions.
2. Call awr_questions. Those are the tool's own header and confirm questions: never
   answer them; the host shows them to the user.
3. Pick at most 5 questions (from the candidates, reworded plainly if needed, or new
   ones the report supports) and call propose_questions. Each has: kind (one of
   unlock, must_have_probe, stage_scope, number_context, disclosure,
   library_contradiction), text (one plain sentence ending in "?"), project (an entry
   id from the report, or ""), reason (why a true answer would strengthen the resume)
   and plausibility (0 to 1, how likely the answer is yes given the documents). If it
   returns problems, fix them and call it again.
4. Ask each accepted question with ask_user, one at a time, as "[q1] <text>" using the
   accepted id and text. After each answer, call write_answer_note with that id and
   the user's answer copied exactly.
5. When every accepted question is answered, reply with two or three sentences of
   summary. Do not rebuild; the host does.

Truth rules: the user's answers are the only facts you add, and the user owns them.
Never suggest what the user should claim, never answer for the user, never ask to drop
a qualifier such as paper trading, prototype, course or hackathon. Ask, don't lead.
"""

KICKOFF = "Interview the user to strengthen the resume for this posting. Start with awr_build."

_ID_PREFIX = re.compile(r"^\s*\[?\s*(q\d+)\s*\]?[:.)\s-]*", re.IGNORECASE)


def guard_state_dir(path: Path) -> Path:
    """Resolve *path*; refuse the live moeka state (``~/.nanobot``, ``~/.nanobot-sessions``)."""
    resolved = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    for guarded in (home / ".nanobot", home / ".nanobot-sessions"):
        if resolved == guarded or resolved.is_relative_to(guarded):
            raise ValueError(
                f"state dir {resolved} is inside {guarded}, the live moeka state; "
                "the interviewer keeps its own (default ~/.moeka-resume-interviewer)"
            )
    return resolved


@dataclass
class Settings:
    workspace: Path
    posting: Path
    awr_command: list[str]
    home: Path = DEFAULT_HOME
    model: str = "qwen3:8b"
    ollama_url: str = "http://127.0.0.1:11434"
    awr_ollama_url: str | None = None
    run_id: str | None = None
    seeds: Path | None = None
    cap: int = qmod.MAX_QUESTIONS
    token_cap: int = 400_000
    max_iterations: int = 30
    deadline_s: int | None = 1800
    fallback: bool = True
    summary: bool = True
    think: bool = False

    @property
    def state_dir(self) -> Path:
        return guard_state_dir(self.home / "state")

    @property
    def work_dir(self) -> Path:
        return Path(self.home).expanduser().resolve() / "work"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"


@dataclass
class RunState:
    """What survives between ``--questions-out`` and ``--answers-in`` (``runs/<id>.json``)."""

    run_id: str
    workspace: str
    posting: str
    session_key: str
    baseline_report_path: str | None = None
    baseline_exit_code: int | None = None
    accepted: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    awr_questions: dict[str, Any] = field(default_factory=dict)
    proposer: str = "model"
    pending_ask: str | None = None
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    agent_stops: list[dict[str, Any]] = field(default_factory=list)

    def save(self, runs_dir: Path) -> Path:
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{self.run_id}.json"
        path.write_text(json.dumps(self.__dict__, indent=2, ensure_ascii=False), encoding="utf-8")
        (runs_dir / "latest").write_text(self.run_id, encoding="utf-8")
        return path

    @classmethod
    def load(cls, runs_dir: Path, run_id: str | None) -> RunState:
        if not run_id:
            latest = runs_dir / "latest"
            if not latest.exists():
                raise FileNotFoundError(f"no previous run in {runs_dir}; pass --run-id")
            run_id = latest.read_text(encoding="utf-8").strip()
        path = runs_dir / f"{run_id}.json"
        return cls(**json.loads(path.read_text(encoding="utf-8")))


class Interviewer:
    """One interview run on one awr workspace and posting."""

    def __init__(
        self,
        settings: Settings,
        *,
        awr: AwrClient | None = None,
        provider: Any | None = None,
        state: RunState | None = None,
    ) -> None:
        self.settings = settings
        self.awr = awr or AwrClient(
            settings.awr_command, settings.workspace, ollama_url=settings.awr_ollama_url,
        )
        run_id = (state.run_id if state else settings.run_id) or _dt.datetime.now().strftime(
            "%Y%m%dT%H%M%S")
        self.state = state or RunState(
            run_id=run_id, workspace=str(settings.workspace), posting=str(settings.posting),
            session_key=f"interview:{run_id}",
        )
        self.seeds = qmod.load_seeds(settings.seeds)
        self.baseline_report: dict[str, Any] | None = None
        if self.state.baseline_report_path and Path(self.state.baseline_report_path).exists():
            self.baseline_report = json.loads(
                Path(self.state.baseline_report_path).read_text(encoding="utf-8"))
        self._provider = provider
        self.kernel: Kernel | None = None
        self.agent = None
        self.strikes = 0

    # -- kernel ---------------------------------------------------------------------

    def open(self) -> Interviewer:
        s = self.settings
        state_dir = s.state_dir
        data_dir = state_dir / "data"
        spec = ModelSpec(
            name=MODEL_ALIAS, model=s.model, provider="ollama", tier="local",
            context_window=32768, max_tokens=2048,
        )
        env = Environment.for_host(
            state_dir=state_dir, work_dir=s.work_dir, data_dir=data_dir, credentials={},
            providers=[ProviderSpec(name="ollama", api_base=s.ollama_url.rstrip("/") + "/v1")],
            models=[spec], default_model=MODEL_ALIAS, strict=True,
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        budget = SharedCapBudget(
            data_dir, budget_id=f"run-{self.state.run_id}", limit_tokens=s.token_cap,
            allow_unpriced=True, reset_caps=True,
        )
        self.kernel = Kernel(env, budget=budget, consumer=CONSUMER)
        if self._provider is not None:
            self.kernel.llm.register_provider(MODEL_ALIAS, self._provider, spec)
        prompt = SYSTEM_PROMPT if s.think else SYSTEM_PROMPT + "\n/no_think\n"
        self.agent = self.kernel.agent(AgentSpec(
            name=AGENT_NAME, system_prompt=prompt, model=MODEL_ALIAS, tools_allow=TOOLS,
            actions=self._actions(),
            limits=RunLimits(max_iterations=s.max_iterations, deadline_s=s.deadline_s),
        ))
        return self

    def close(self) -> None:
        if self.kernel is not None:
            self.kernel.close()
            self.kernel = None

    def __enter__(self) -> Interviewer:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _actions(self) -> tuple[FunctionTool, ...]:
        item = {
            "type": "object",
            "properties": {
                # No enum: the host lints each item and says what is wrong with it, so
                # one bad kind does not reject the whole call at schema validation.
                "kind": {"type": "string", "description": "One of " + ", ".join(qmod.KINDS)},
                "text": {"type": "string", "description": "One plain sentence ending in '?'."},
                "project": {"type": "string", "description": "Entry id from the report, or ''."},
                "reason": {"type": "string",
                           "description": "Why a true answer would strengthen the resume."},
                "plausibility": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["kind", "text", "project", "reason"],
        }
        return (
            FunctionTool(self.awr_build, name="awr_build", parameters={
                "type": "object", "properties": {}}),
            FunctionTool(self.awr_report, name="awr_report", read_only=True, parameters={
                "type": "object", "properties": {}}),
            FunctionTool(self.awr_questions, name="awr_questions", read_only=True, parameters={
                "type": "object", "properties": {}}),
            FunctionTool(self.propose_questions, name="propose_questions", parameters={
                "type": "object",
                "properties": {"questions": {"type": "array", "items": item, "maxItems": 12}},
                "required": ["questions"],
            }),
            FunctionTool(self.write_answer_note, name="write_answer_note", parameters={
                "type": "object",
                "properties": {"question_id": {"type": "string"}, "answer": {"type": "string"}},
                "required": ["question_id", "answer"],
            }),
        )

    # -- actions (run in the kernel's action threads) --------------------------------

    def _ensure_baseline(self) -> dict[str, Any]:
        if self.baseline_report is None:
            envelope = self.awr.build(Path(self.state.posting))
            self.baseline_report = AwrClient.load_report(envelope)
            self.state.baseline_report_path = envelope["data"]["report_path"]
            self.state.baseline_exit_code = envelope.get("exit_code")
        return self.baseline_report

    def _candidates(self, report: Mapping[str, Any]) -> list[qmod.Candidate]:
        return qmod.candidates_from_report(report) + list(self.seeds)

    def _summary_payload(self) -> str:
        report = self.baseline_report or {}
        cands = sorted(self._candidates(report), key=lambda c: -c.score)[:10]
        payload = {
            "report": report_summary(report, {"exit_code": self.state.baseline_exit_code}),
            "projects": qmod.known_projects(report),
            "candidates": [c.as_dict() for c in cands],
        }
        return json.dumps(payload, ensure_ascii=False)

    def awr_build(self) -> str:
        """Build the resume for the posting with awr and return the report summary and
        ranked candidate questions. Builds once; later calls return the same build."""
        try:
            self._ensure_baseline()
        except AwrError as exc:
            return f"Error: {exc}"
        return self._summary_payload()

    def awr_report(self) -> str:
        """Return the summary of the last awr build and the ranked candidate questions."""
        if self.baseline_report is None:
            return "Error: no build yet; call awr_build first."
        return self._summary_payload()

    def awr_questions(self) -> str:
        """List awr's own header and confirm questions. The host relays them to the user;
        never answer them."""
        try:
            self._load_awr_questions()
        except AwrError as exc:
            return f"Error: {exc}"
        return json.dumps({
            "note": "These are for the user to answer with `awr answer`. Never answer them.",
            **self.state.awr_questions,
        }, ensure_ascii=False)

    def _load_awr_questions(self) -> dict[str, Any]:
        data = self.awr.questions().get("data") or {}
        self.state.awr_questions = {
            "questions": data.get("questions") or [],
            "deferred": data.get("deferred") or [],
            "open": data.get("open"),
        }
        return self.state.awr_questions

    def propose_questions(self, questions: list[dict[str, Any]]) -> str:
        """Submit at most 5 questions; the host lints, ranks and caps them and returns the
        accepted ones (ids q1..q5) and the problems of the rejected ones."""
        if any(a in self.state.answers for a in (q["id"] for q in self.state.accepted)):
            return "Error: questions were already asked; the accepted set is fixed."
        report = self.baseline_report
        if report is None:
            return "Error: no build yet; call awr_build first."
        if not isinstance(questions, list):
            return "Error: questions must be a list of objects."
        cands = [qmod.candidate_from_model(q, report) for q in questions if isinstance(q, dict)]
        accepted, rejected = qmod.select(cands, qmod.known_projects(report),
                                         cap=self.settings.cap)
        self.state.rejected.extend(rejected)
        if accepted:
            self.state.accepted = [q.model_dump() for q in accepted]
            self.state.proposer = "model"
        return json.dumps({
            "accepted": self.state.accepted,
            "rejected": rejected,
            "next": "Ask each accepted question with ask_user as '[id] text'."
            if accepted else "Nothing was accepted; fix the problems and call again.",
        }, ensure_ascii=False)

    def write_answer_note(self, question_id: str, answer: str) -> str:
        """Write the user's answer to an accepted question into the dated notes doc.
        The answer must be exactly what the user said."""
        qid = question_id.strip().strip("[]").lower()
        got = self.state.answers.get(qid)
        if got is None:
            return f"Error: the user has not answered {qid}; never write an answer for the user."
        if " ".join(answer.split()) != " ".join(str(got["answer"]).split()):
            return "Error: the answer must be the user's words exactly; nothing was written."
        path = self._write_notes()
        return f"Written to {path.name}." if path else "Nothing to write (not a content answer)."

    # -- answers ----------------------------------------------------------------------

    def accepted(self) -> list[qmod.Question]:
        return [qmod.Question(**q) for q in self.state.accepted]

    def match(self, asked: str) -> qmod.Question | None:
        """The accepted question an ``ask_user`` call names (by ``[qN]`` or by its text)."""
        accepted = {q.id: q for q in self.accepted()}
        m = _ID_PREFIX.match(asked or "")
        if m and m.group(1).lower() in accepted:
            return accepted[m.group(1).lower()]
        norm = qmod._norm(asked or "")
        for q in accepted.values():
            if norm and (qmod._norm(q.text) == norm or qmod._norm(q.text) in norm):
                return q
        return None

    def record_answer(self, question: qmod.Question, answer: str) -> str:
        """Record the user's answer as a ``source="user"`` fact; return its fact id."""
        assert self.kernel is not None
        answer = str(answer).strip()
        fact_id = self.kernel.epistemics.record_fact(
            {"question_id": question.id, "kind": question.kind, "project": question.project,
             "question": question.text, "answer": answer},
            source="user", ref=f"resume-interviewer:{self.state.run_id}/{question.id}",
        )
        self.state.answers[question.id] = {"answer": answer, "fact_id": fact_id}
        return fact_id

    def _answer_rows(self) -> list[dict[str, Any]]:
        rows = []
        for q in self.accepted():
            got = self.state.answers.get(q.id)
            if got is not None:
                rows.append({**q.model_dump(), **got})
        return rows

    def _write_notes(self) -> Path | None:
        return write_notes(self.settings.workspace, self.state.run_id, self._answer_rows())

    # -- agent driving ------------------------------------------------------------------

    def _run(self, message: str) -> RunResult:
        assert self.agent is not None
        result = self.agent.run_sync(message, session=self.state.session_key)
        self.state.agent_stops.append({
            "stop_reason": result.stop_reason,
            "error": str(result.error) if result.error else None,
            "iterations": result.iterations,
        })
        return result

    def asked(self, result: RunResult | None) -> qmod.Question | None:
        """The accepted question a run ended on: an ``ask_user`` call, or (small local
        models do this) a final text that is just an accepted question."""
        if result is None or not self.state.accepted:
            return None
        if result.stop_reason == "ask_user":
            return self.match(result.question.question if result.question else "")
        if result.stop_reason == "completed":
            q = self.match(result.content or "")
            if q is not None and q.id in self.state.answers:
                return None  # a summary quoting an answered question is not an ask
            if q is not None:
                self.state.agent_stops[-1]["asked_as_text"] = q.id
            return q
        return None

    def _fallback(self, why: str) -> bool:
        """Code-ranked questions when the model gave none; False if not allowed."""
        if self.state.accepted:
            return True
        if not self.settings.fallback:
            return False
        report = self._ensure_baseline()
        accepted, rejected = qmod.select(self._candidates(report), qmod.known_projects(report),
                                         cap=self.settings.cap)
        self.state.accepted = [q.model_dump() for q in accepted]
        self.state.rejected.extend(rejected)
        self.state.proposer = f"code-fallback ({why})"
        return True

    def _propose_phase(self) -> RunResult | None:
        """Run the agent until it asks its first accepted question or stops."""
        result = self._run(KICKOFF)
        while True:
            if result.stop_reason == "ask_user":
                if not self.state.accepted:
                    if not self._strike():
                        return result
                    result = self._run("Host: call propose_questions first; only accepted "
                                       "questions are relayed to the user.")
                    continue
                if self.asked(result) is None:
                    if not self._strike():
                        return result
                    result = self._run(self._not_accepted_message())
                    continue
                return result
            if self.asked(result) is not None:
                return result
            if result.stop_reason == "completed" and not self.state.accepted and self._strike():
                result = self._run("Host: no questions were accepted yet. Call awr_build if "
                                   "you have not, then propose_questions.")
                continue
            return result

    def _strike(self) -> bool:
        self.strikes += 1
        return self.strikes <= MAX_STRIKES

    def _not_accepted_message(self) -> str:
        ids = ", ".join(f"[{q.id}] {q.text}" for q in self.accepted()
                        if q.id not in self.state.answers)
        return ("Host: that question is not an accepted one and was not shown to the user. "
                f"Ask only these, one at a time: {ids or 'none left; finish with a summary'}")

    # -- modes --------------------------------------------------------------------------

    def questions_out(self, out: Path) -> dict[str, Any]:
        """Batch, part 1: build, propose, and write the questions (``{id, kind, text,
        project, reason}`` per line); stop where the agent asks."""
        result = self._propose_phase()
        stop = result.stop_reason if result else "none"
        asked = self.asked(result)
        self.state.pending_ask = asked.id if asked else None
        if not self._fallback(stop):
            self.state.save(self.settings.runs_dir)
            return self._outcome(ok=False, error=f"no questions: agent stopped with {stop}")
        if not self.state.awr_questions:
            try:
                self._load_awr_questions()
            except AwrError as exc:
                self.state.awr_questions = {"error": str(exc)}
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n"
                               for q in self.state.accepted), encoding="utf-8")
        self.state.save(self.settings.runs_dir)
        return self._outcome(ok=True, questions_out=str(out))

    def answers_in(self, answers: Path, out: Path) -> dict[str, Any]:
        """Batch, part 2: record the answers, write the notes doc, rebuild, diff."""
        rows, ignored = _read_answers(Path(answers))
        by_id = {q.id: q for q in self.accepted()}
        for row in rows:
            q = by_id.get(row["id"])
            if q is None:
                ignored.append({**row, "problem": "unknown question id"})
                continue
            self.record_answer(q, row["answer"])
        return self._finish(out, ignored=ignored, batch=True)

    def interview(self, ask: Callable[[qmod.Question], str], out: Path | None = None) -> dict:
        """Interactive: relay each accepted ``ask_user`` to *ask* and resume the session."""
        result = self._propose_phase()
        while result is not None and (
                result.stop_reason == "ask_user" or self.asked(result) is not None):
            q = self.asked(result)
            if q is None or q.id in self.state.answers:
                if not self._strike():
                    break
                result = self._run(self._not_accepted_message())
                continue
            answer = ask(q)
            self.record_answer(q, answer)
            result = self._run(f"[{q.id}] The user answered: {answer}")
        self._fallback(result.stop_reason if result else "none")
        for q in self.accepted():  # the host asks whatever the agent left unasked
            if q.id not in self.state.answers:
                self.record_answer(q, ask(q))
        if not self.state.awr_questions:
            try:
                self._load_awr_questions()
            except AwrError as exc:
                self.state.awr_questions = {"error": str(exc)}
        return self._finish(out, ignored=[], batch=False)

    def _finish(self, out: Path | None, *, ignored: list, batch: bool) -> dict[str, Any]:
        notes = self._write_notes()
        before = self._ensure_baseline()
        try:
            envelope = self.awr.build(Path(self.state.posting))
            after = AwrClient.load_report(envelope)
            rebuild = {"exit_code": envelope.get("exit_code"),
                       "report_path": envelope["data"]["report_path"]}
            diff = report_diff(before, after)
        except AwrError as exc:
            rebuild, diff = {"error": str(exc)}, None
        try:
            self._load_awr_questions()
        except AwrError as exc:
            self.state.awr_questions = {"error": str(exc)}
        summary = None
        if batch and self.settings.summary and self.state.agent_stops:
            message = ("Host: the user answered every question in one batch:\n" + "\n".join(
                f"[{r['id']}] {r['answer']}" for r in self._answer_rows())
                + "\nThe answers are recorded and the notes doc is written; the host rebuilt "
                  "the resume. Reply with a short summary. Ask nothing more.")
            result = self._run(message)
            summary = result.content if result.stop_reason == "completed" else None
            self.state.pending_ask = None
        self.state.save(self.settings.runs_dir)
        outcome = self._outcome(
            ok=diff is not None, notes_doc=str(notes) if notes else None, rebuild=rebuild,
            diff=diff, ignored_answers=ignored, agent_summary=summary,
            answers=[{**r, "written": is_content_answer(str(r["answer"]))}
                     for r in self._answer_rows()],
        )
        if out is not None:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(json.dumps(outcome, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
        return outcome

    def _outcome(self, *, ok: bool, **extra: Any) -> dict[str, Any]:
        return {
            "ok": ok,
            "run_id": self.state.run_id,
            "proposer": self.state.proposer,
            "questions": self.state.accepted,
            "rejected": self.state.rejected,
            "awr_questions": self.state.awr_questions,
            "awr_questions_note": "Answer these yourself with `awr answer ID VALUE`; "
                                  "the interviewer never answers them.",
            "agent_stops": self.state.agent_stops,
            **extra,
        }


def _read_answers(path: Path) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    rows: list[dict[str, str]] = []
    ignored: list[dict[str, Any]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            ignored.append({"line": n, "problem": "not JSON"})
            continue
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(
                row.get("answer"), str):
            ignored.append({"line": n, "problem": "expected {id: str, answer: str}"})
            continue
        rows.append({"id": row["id"].strip().lower(), "answer": row["answer"]})
    return rows, ignored
