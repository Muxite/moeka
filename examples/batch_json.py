"""Fan-out JSON calls under a spend cap, with one trace file per run.

The awork shape: many independent ``complete_json`` calls (here: score five job
postings against a profile), each typed by a pydantic model, fanned out with
``kernel.llm.batch``, capped by a ``CapBudget`` per pipeline stage, and every kernel
event written to a JSONL trace.

Runs offline: a ``FakeProvider`` answers for the model alias, so there is no
network and no API key. Swap it for a real ``ProviderSpec`` to go live.

    python examples/batch_json.py
"""

from __future__ import annotations

import json
import re
import tempfile
from collections import Counter
from pathlib import Path

from pydantic import BaseModel

from moeka import Environment, Kernel, ModelSpec, ProviderSpec, Sampling
from moeka.budget import CapBudget
from moeka.errors import BudgetExceeded
from moeka.llm import GenerateOptions, Request, system, user
from moeka.testing import FakeCall, FakeProvider
from moeka.trace import JsonlTraceSink

POSTINGS = [
    "Senior Python engineer, asyncio, SQLite, remote",
    "Frontend developer, React, TypeScript",
    "Platform engineer, Linux, systemd, homelab experience a plus",
    "Data analyst, Excel, dashboards",
    "Backend engineer, Python, LLM tooling",
]
PROFILE = "Python backend developer; asyncio; Linux admin; builds LLM agents."


class JobFit(BaseModel):
    """What each call must return."""

    score: int
    reason: str


# Model prices in USD per million tokens (the host owns prices; the kernel asks).
FAST = ModelSpec(
    name="fast", model="qwen3-8b", provider="vllm", tier="fast",
    max_tokens=200, price_in=0.5, price_out=2.0,
)


def fake_scorer(call: FakeCall) -> str:
    """Stand-in for the model: score by keyword overlap, reply with JSON."""
    prompt = str(call.messages[-1]["content"])
    posting = prompt.split("Posting:", 1)[-1].lower()
    hits = [w for w in ("python", "asyncio", "linux", "llm") if re.search(rf"\b{w}\b", posting)]
    return json.dumps({"score": min(10, 2 + 3 * len(hits)), "reason": ", ".join(hits) or "none"})


def build_env(state: Path, work: Path, trace: JsonlTraceSink) -> Environment:
    return Environment.for_host(
        state_dir=state,
        work_dir=work,
        credentials={},  # a local vLLM endpoint needs no key
        providers=[ProviderSpec(name="vllm", api_base="http://127.0.0.1:8000/v1")],
        models=[FAST],
        default_model="fast",
        trace=trace,
    )


def requests_for(postings: list[str], *, max_tokens: int | None = None) -> list[Request]:
    opts = GenerateOptions(sampling=Sampling(temperature=0.0, seed=7, max_tokens=max_tokens))
    return [
        Request(
            messages=[
                system("You score job postings 0-10 for this candidate: " + PROFILE),
                user(f"Posting: {text}"),
            ],
            model_cls=JobFit,
            retries=1,
            opts=opts,
        )
        for text in postings
    ]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="moeka-batch-") as tmp:
        root = Path(tmp)
        trace = JsonlTraceSink(root / "trace" / "run.jsonl")
        # Each stage gets its own 2-cent cap; there is no overall cap here.
        budget = CapBudget(per_tag={"stage": 0.02})
        with trace, Kernel(build_env(root / "state", root / "work", trace), budget=budget,
                           max_concurrency=4) as kernel:
            kernel.llm.register_provider("fast", FakeProvider(default=fake_scorer), FAST)

            # Stage 1: every call made inside the span carries stage=screen.
            with kernel.trace.span("screen", stage="screen"):
                screened = kernel.llm.batch_sync(requests_for(POSTINGS))
            if screened.systemic is not None or screened.errors:
                print(f"screen failed: {screened.systemic or screened.errors[0]}")
                return 1
            ranked = sorted(
                zip(POSTINGS, screened.completions, strict=True),
                key=lambda pair: pair[1].parsed.score, reverse=True,
            )
            for posting, done in ranked:
                print(f"{done.parsed.score:>2}  {posting}  ({done.parsed.reason})")
            cost = sum(c.cost_usd or 0.0 for c in screened.completions)
            print(f"screen: {len(screened.completions)} calls, ${cost:.6f}")

            # Stage 2 asks for long answers: the worst case (4000 output tokens per
            # round) is over the stage cap, so the budget refuses before anything is
            # sent and the batch stops as one systemic error.
            with kernel.trace.span("rewrite", stage="rewrite"):
                rewrite = kernel.llm.batch_sync(
                    requests_for([p for p, _ in ranked[:2]], max_tokens=4000)
                )
            stopped = rewrite.systemic
            if not isinstance(stopped, BudgetExceeded):
                print("rewrite was expected to stop on the budget")
                return 1
            print(f"rewrite: stopped ({stopped.kind}): {stopped}")
            print(f"spent ${budget.spent_usd:.6f}; screen exposure "
                  f"${budget.exposure('stage', 'screen'):.6f}")

        events = [json.loads(line) for line in trace.path.read_text("utf-8").splitlines()]
        counts = Counter(e["event"] for e in events)
        print("trace:", ", ".join(f"{name}={n}" for name, n in sorted(counts.items())))
        if counts["model.call"] != len(POSTINGS) or counts["budget.refuse"] < 1:
            print("unexpected trace contents")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
