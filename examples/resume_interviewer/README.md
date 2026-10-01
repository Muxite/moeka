# Resume interviewer

A moeka-kernel app that drives the awork-resume CLI (`awr`) the way any user's agent would. It builds the resume,
reads `report.json` and asks the user at most five questions that would make the resume stronger. Each question
says why it matters. The content answers go into a dated notes doc in the workspace. Then it rebuilds and diffs
the two reports.

awork-resume stays independent of moeka. This app only runs `awr ... --json` as a subprocess and reads the
envelope and `report.json` (see awork-resume `AGENTS.md`).

## Run

From the repo root:

```bash
# interactive: questions on stdout, answers on stdin
uv run python -m examples.resume_interviewer --workspace W --posting posting.md \
    --awr-project ~/projects/awork-resume

# batch, part 1: build, choose questions, write them, stop
uv run python -m examples.resume_interviewer --workspace W --posting posting.md \
    --awr-project ~/projects/awork-resume --run-id r1 --questions-out q.jsonl

# batch, part 2: record the answers, write the notes doc, rebuild, diff
uv run python -m examples.resume_interviewer --workspace W --awr-project ~/projects/awork-resume \
    --run-id r1 --answers-in a.jsonl --out result.json
```

Options:

- `--awr CMD` or `--awr-project DIR` picks the awr binary. Without either, `awr` is taken from `PATH`.
- `--awr-ollama-url` sets `AWR_OLLAMA_URL` for awr. The night run uses the router `http://127.0.0.1:11450`.
- `--ollama-url` and `--model` pick the interviewer's own model. The default is `qwen3:8b` on `:11434`. Do not use
  llama3.1 here: Ollama 0.35 breaks its JSON output.
- `--seeds FILE` adds seed questions (a JSON list or JSONL in the question shape). `disclosure` and
  `library_contradiction` questions come in this way.
- `--token-cap N` caps the run's tokens (default 400000).
- `--no-fallback` exits 5 when the model offers no acceptable question. Without it, the code-ranked candidates are
  used.
- `--no-summary` skips resuming the agent after batch answers.

## Formats

The batch formats are fixed, and the bench oracle writes answers in the same shape:

- **Question** (one per line of `--questions-out`): `{"id", "kind", "text", "project", "reason"}`
  - `kind` is one of `unlock`, `must_have_probe`, `stage_scope`, `number_context`, `disclosure` or
    `library_contradiction`.
  - `project` is an entry id from the report, or `""`.
- **Answer** (one per line of `--answers-in`): `{"id", "answer"}`. Unknown ids are listed under `ignored_answers`.
- **Result** (`--out`) holds:
  - `questions` and `rejected`, with the reason for each rejection;
  - `answers`, each with its `fact_id` and whether it was written;
  - `notes_doc`;
  - `diff`: `uncovered_must` before, after, resolved and new; `bullets` added, removed and changed; `blocked` added
    and removed; `header_placeholders`;
  - `awr_questions` and `agent_stops`.

awr's own questions (`header.*` and `confirm.*` from `awr questions`) are listed under `awr_questions` and printed
for the human. The interviewer never answers them, and the agent has no tool that could.

## How it works

- **Kernel.** The app calls `Environment.for_host` with:
  - strict mode and no credentials;
  - one local Ollama provider;
  - the state dir `~/.moeka-resume-interviewer/state`, set with `--home`. It refuses anything under `~/.nanobot`
    or `~/.nanobot-sessions`.

  The budget is a token-capped `SharedCapBudget`, one `budget_id` per run. The consumer is `resume-interviewer`.
- **Agent.** `tools_allow` is exactly `awr_build`, `awr_report`, `awr_questions`, `propose_questions`,
  `write_answer_note` and `ask_user`. There is no exec tool, no file tools and no web.
- **The model proposes and the code decides** (`questions.py`). The code enforces:
  - the six kinds;
  - one sentence ending in `?`;
  - a non-empty reason;
  - that `project` is a known entry;
  - no quote cut right after a number;
  - no leading or claim-suggesting wording;
  - no request to drop a truth-bearing qualifier (paper trading, prototype, course, hackathon);
  - no duplicates.

  It ranks by must-have weight × strength gain × plausibility and caps the list at 5.
- **Relaying.** An `ask_user` call is relayed only when it names an accepted question (`[q1] ...`). Anything else is
  sent back to the agent and never reaches the user. `write_answer_note` writes only the words the user actually
  gave.
- **Answers.** Every answer is recorded with
  `kernel.epistemics.record_fact(..., source="user", ref="resume-interviewer:<run>/<id>")`.
  - Content answers go to `W/docs/answers-YYYYMMDD.md`. Each section carries a marker with the run, the question id,
    the fact id and `source=user`.
  - A plain "no" is recorded as a fact but not written to the doc.
  - awr takes docs as true, so the next `awr build` ingests the doc. The user is responsible for their answers, and
    every answer can be traced back to them.
- **Fallbacks.** The agent may stop early, for example on a budget refusal, a model error or no accepted question.
  Then the host asks the code-ranked candidates (`proposer: "code-fallback (<stop>)"`) unless `--no-fallback` is
  set.

## Tests

`tests/examples/test_resume_interviewer.py` runs offline. It uses a fake `awr` script with fictional data and a
`FakeProvider` that plays the model.

## Kernel gaps found

- **Resuming after `ask_user`.** A run that stops on `ask_user` leaves the call unanswered in the session. The next
  `agent.run(answer, session=same)` works, also from a fresh kernel on the same state dir. But the kernel closes the
  dangling call with a synthetic tool result, `[Tool result unavailable — call was interrupted or lost]`, and the
  answer arrives as a plain user turn.
  - The helpers in `nanobot/agent/tools/ask.py` that would deliver the answer as the `ask_user` tool result
    (`pending_ask_user_id`, `ask_user_tool_result_messages`) are not used by the kernel `Agent`.
  - There is no API for this. The host works around it by sending `[qN] The user answered: ...`.
- **Questions as plain text.** `qwen3:8b` tends to end its turn with the question as plain text instead of calling
  `ask_user`, and there is no per-agent `tool_choice` to force the call. The host treats a final text that matches
  an accepted, unanswered question as the ask.
- **Schema validation.** It rejects the whole call when one array item breaks an `enum`. So `propose_questions`
  declares `kind` without an enum, and the host reports per-item problems instead.
- **Persistent budget caps.** `SharedCapBudget` caps persist per `budget_id`, and a changed cap raises unless
  `reset_caps=True`. The host uses one budget id per run.
