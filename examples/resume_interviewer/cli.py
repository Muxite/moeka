"""Command line: interactive interview, or batch ``--questions-out`` / ``--answers-in``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from .awr import AwrError, resolve_command
from .host import DEFAULT_HOME, Interviewer, RunState, Settings
from .questions import MAX_QUESTIONS, Question

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AWR = 3
EXIT_NO_QUESTIONS = 5


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="resume_interviewer",
        description="Ask the user at most 5 questions that would strengthen an awr resume.",
    )
    p.add_argument("--workspace", type=Path, required=True, help="the awr workspace W")
    p.add_argument("--posting", type=Path, help="the posting file (required unless --answers-in)")
    p.add_argument("--awr", help="the awr command (default: awr on PATH)")
    p.add_argument("--awr-project", type=Path,
                   help="an awork-resume checkout: runs `uv run --project DIR awr`")
    p.add_argument("--awr-ollama-url", help="AWR_OLLAMA_URL for awr (default: inherited)")
    p.add_argument("--ollama-url", default="http://127.0.0.1:11434",
                   help="Ollama server for the interviewer model")
    p.add_argument("--model", default="qwen3:8b",
                   help="interviewer model (not llama3.1: Ollama 0.35 breaks its JSON)")
    p.add_argument("--home", type=Path, default=DEFAULT_HOME,
                   help="interviewer home; state lives in HOME/state (never ~/.nanobot)")
    p.add_argument("--run-id", help="run id (default: a timestamp; --answers-in: the latest)")
    p.add_argument("--seeds", type=Path, help="seed questions (JSON list or JSONL)")
    p.add_argument("--cap", type=int, default=MAX_QUESTIONS, help="questions to ask (max 5)")
    p.add_argument("--token-cap", type=int, default=400_000, help="token budget for the run")
    p.add_argument("--no-fallback", action="store_true",
                   help="fail instead of asking code-ranked questions when the model gives none")
    p.add_argument("--no-summary", action="store_true",
                   help="batch: do not resume the agent for a summary after the answers")
    p.add_argument("--think", action="store_true", help="let qwen3 think (slower)")
    p.add_argument("--questions-out", type=Path, help="batch: write questions JSONL and stop")
    p.add_argument("--answers-in", type=Path, help="batch: read answers JSONL and rebuild")
    p.add_argument("--out", type=Path, help="write the result (diff, answers) as JSON here")
    return p


def _settings(args: argparse.Namespace, posting: Path) -> Settings:
    return Settings(
        workspace=args.workspace.resolve(), posting=posting.resolve(),
        awr_command=resolve_command(args.awr, args.awr_project), home=args.home,
        model=args.model, ollama_url=args.ollama_url, awr_ollama_url=args.awr_ollama_url,
        run_id=args.run_id, seeds=args.seeds, cap=min(args.cap, MAX_QUESTIONS),
        token_cap=args.token_cap, fallback=not args.no_fallback, summary=not args.no_summary,
        think=args.think,
    )


def _stdin_ask(stdin: TextIO, stdout: TextIO) -> Any:
    def ask(q: Question) -> str:
        where = f" ({q.project})" if q.project else ""
        stdout.write(f"\n[{q.id}]{where} {q.text}\n  why: {q.reason}\n> ")
        stdout.flush()
        return stdin.readline().strip()
    return ask


def _print_awr_questions(outcome: dict[str, Any], stream: TextIO) -> None:
    rows = (outcome.get("awr_questions") or {}).get("questions") or []
    if not rows:
        return
    stream.write("\nawr's own questions (answer them yourself with `awr answer ID VALUE`):\n")
    for row in rows:
        stream.write(f"  {row.get('id')}: {row.get('text')}\n")


def main(argv: Sequence[str] | None = None, *, provider: Any = None,
         stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    args = build_parser().parse_args(argv)
    if args.questions_out and args.answers_in:
        stdout.write("pass --questions-out or --answers-in, not both\n")
        return EXIT_USAGE
    state = None
    try:
        if args.answers_in:
            probe = Settings(workspace=args.workspace, posting=Path("."), awr_command=[],
                             home=args.home)
            state = RunState.load(probe.runs_dir, args.run_id)
            posting = Path(state.posting)
        elif args.posting is None:
            stdout.write("--posting is required\n")
            return EXIT_USAGE
        else:
            posting = args.posting
        settings = _settings(args, posting)
        _ = settings.state_dir  # refuses ~/.nanobot before anything opens
    except (ValueError, FileNotFoundError) as exc:
        stdout.write(f"error: {exc}\n")
        return EXIT_USAGE

    try:
        with Interviewer(settings, provider=provider, state=state) as iv:
            if args.questions_out:
                outcome = iv.questions_out(args.questions_out)
                for q in outcome["questions"]:
                    stdout.write(f"[{q['id']}] ({q['kind']}) {q['text']}\n  why: {q['reason']}\n")
            elif args.answers_in:
                outcome = iv.answers_in(args.answers_in, args.out or args.answers_in.with_suffix(
                    ".result.json"))
            else:
                outcome = iv.interview(_stdin_ask(stdin, stdout), args.out)
    except AwrError as exc:
        stdout.write(f"awr error: {exc}\n")
        return EXIT_AWR
    if args.out and args.questions_out:
        args.out.write_text(json.dumps(outcome, indent=2, ensure_ascii=False), encoding="utf-8")
    _print_awr_questions(outcome, stdout)
    if not args.questions_out:
        diff = outcome.get("diff") or {}
        unc = diff.get("uncovered_must") or {}
        bullets = diff.get("bullets") or {}
        stdout.write(
            f"\nnotes doc: {outcome.get('notes_doc')}\n"
            f"uncovered must-haves resolved: {unc.get('resolved', [])}\n"
            f"bullets added: {len(bullets.get('added', []))}, "
            f"changed: {len(bullets.get('changed', []))}\n"
        )
    if not outcome.get("ok"):
        stdout.write(f"failed: {outcome.get('error') or outcome.get('rebuild')}\n")
        return EXIT_NO_QUESTIONS
    return EXIT_OK
