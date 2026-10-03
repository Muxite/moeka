"""Selection benchmark (spec 011 NFR-001, NFR-002, SC-004).

``cases.jsonl``: one labelled turn per line (``message``, ``attachments`` with
``name``/``mime``, ``expected`` capability names, ``source`` synthetic or moeka-style).
``library/``: the fixture capability library (skills with and without ``io``).

The quality test runs by default: with the deterministic lexical ranker, recall@8 (every
expected name in ``selected``) must be >= 0.95 and the skills summary must be >= 50%
smaller than the ``"all"`` listing on average. Ambient types are the gateway's
(``workspace`` and ``host``). Tokens are counted as word/punctuation runs, a stable
stand-in for a BPE tokenizer (no download, no network).

The latency test (NFR-001: filter p99 < 5 ms over 1000 typed capabilities and 10 task
types) is marked ``bench`` and only runs with ``-m bench``.
"""

from __future__ import annotations

import json
import random
import re
import statistics
import time
from pathlib import Path

import pytest

from nanobot.agent.skills import SkillsLoader
from nanobot.capabilities.index import CapabilityEntry, CapabilityIndex
from nanobot.capabilities.library import skill_entries
from nanobot.capabilities.manifest import EFFECTS, IOSpec
from nanobot.capabilities.select import select
from nanobot.capabilities.task_types import Attachment, extract
from nanobot.capabilities.types import load_vocabulary

HERE = Path(__file__).parent
LIBRARY = HERE / "library"
CASES = HERE / "cases.jsonl"
AMBIENT = ("workspace", "host")
K = 8
_TOKEN = re.compile(r"\w+|[^\w\s]")


def _tokens(text: str) -> int:
    return len(_TOKEN.findall(text))


def _cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("bench-ws")
    loader = SkillsLoader(workspace, builtin_skills_dir=LIBRARY)
    index = CapabilityIndex(":memory:")
    index.sync(skill_entries(loader))
    yield loader, index
    index.close()


def run_benchmark(loader: SkillsLoader, index: CapabilityIndex) -> dict:
    names = [entry["name"] for entry in loader.list_skills(filter_unavailable=False)]
    all_tokens = _tokens(loader.build_skills_summary())
    hits, reductions, misses = 0, [], []
    for case in _cases():
        attachments = [
            Attachment(name=a.get("name"), mime=a.get("mime")) for a in case["attachments"]
        ]
        typing = extract(case["message"], attachments)
        selection = select(
            index, typing, case["message"], ambient=AMBIENT, allowed_effects=EFFECTS, k=K,
        )
        if set(case["expected"]) <= set(selection.selected):
            hits += 1
        else:
            misses.append((case["message"], case["expected"], selection.selected))
        keep = set(selection.selected)
        summary = loader.build_skills_summary(exclude={n for n in names if n not in keep})
        reductions.append(1 - _tokens(summary) / all_tokens)
    total = len(reductions)
    return {
        "cases": total,
        "recall_at_8": hits / total,
        "token_reduction": statistics.fmean(reductions),
        "misses": misses,
    }


def test_fixture_sizes():
    assert len(_cases()) >= 150
    assert sum(1 for path in LIBRARY.iterdir() if (path / "SKILL.md").is_file()) >= 100
    known = {path.name for path in LIBRARY.iterdir()}
    for case in _cases():
        assert set(case["expected"]) <= known, case


def test_recall_and_token_reduction_meet_nfr_002(library):
    loader, index = library
    report = run_benchmark(loader, index)
    print(
        f"\nbench: cases={report['cases']} recall@8={report['recall_at_8']:.3f} "
        f"token_reduction={report['token_reduction']:.3f}"
    )
    for message, expected, selected in report["misses"]:
        print(f"  miss: {message!r} expected={expected} selected={list(selected)}")
    assert report["recall_at_8"] >= 0.95
    assert report["token_reduction"] >= 0.50


def _synthetic_index(count: int, seed: int) -> CapabilityIndex:
    rng = random.Random(seed)
    types = [t for t in load_vocabulary() if t != "none"]
    outputs = [*types, "none"]
    index = CapabilityIndex(":memory:")
    entries = []
    for i in range(count):
        io = IOSpec(
            inputs_all=tuple(sorted(rng.sample(types, rng.randint(0, 2)))),
            inputs_any=tuple(sorted(rng.sample(types, rng.choice((0, 0, 2, 3))))),
            outputs=tuple(sorted(rng.sample(outputs, rng.randint(1, 2)))),
            effects=tuple(sorted(rng.sample(EFFECTS, rng.randint(1, 3)))),
        )
        entries.append(CapabilityEntry("skill", f"cap-{i:04d}", f"capability {i}", io, f"{i:064x}"))
    index.sync(entries)
    return index


@pytest.mark.bench
def test_filter_latency_p99_under_5ms():
    """NFR-001: the SQL filter over 1000 typed capabilities, 10 task types, p99 < 5 ms."""
    index = _synthetic_index(1000, seed=11)
    rng = random.Random(7)
    types = [t for t in load_vocabulary() if t != "none"]
    samples = []
    for _ in range(300):
        task = rng.sample(types, 10)
        requested = rng.choice([None, *types])
        start = time.perf_counter()
        index.filter(task, requested, EFFECTS)
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    p99 = samples[int(len(samples) * 0.99) - 1]
    print(f"\nbench: filter p50={statistics.median(samples):.2f}ms p99={p99:.2f}ms")
    assert p99 < 5.0
