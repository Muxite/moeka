"""Helpers for the 011 held-out tests: lazy access to ``nanobot.capabilities``, fixture
libraries, kernel / ContextBuilder construction, and independent reference implementations of
the spec's rules (FR-001 vocabulary, FR-007 filter rules 1-4, FR-008 lexical scorer, FR-004
sync counts)."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

# -- lazy feature access ----------------------------------------------------------------------


def cap(module: str) -> Any:
    """``nanobot.capabilities.<module>`` (imported at call time, so a missing feature fails the
    calling test, not collection)."""
    return importlib.import_module(f"nanobot.capabilities.{module}")


def repo_root() -> Path:
    import importlib.util

    spec = importlib.util.find_spec("nanobot")
    assert spec is not None and spec.origin is not None
    return Path(os.environ.get("MOEKA_HELDOUT_REPO") or Path(spec.origin).resolve().parents[1])


# -- FR-001 reference vocabulary (spec list, parent in brackets) -------------------------------

PARENT: dict[str, str | None] = {
    "any": None,
    "text": "any", "text/query": "text", "text/markdown": "text", "text/code": "text",
    "text/code:python": "text/code", "text/code:shell": "text/code", "text/log": "text",
    "data": "any", "data/json": "data", "data/csv": "data", "data/yaml": "data",
    "file": "any", "file:pdf": "file", "file:image": "file", "file:audio": "file",
    "file:video": "file", "file:archive": "file", "file:office": "file",
    "url": "any", "url/web": "url", "url/git": "url", "url/video": "url",
    "path": "any", "path/dir": "path", "repo:git": "path/dir",
    "workspace": "any", "host": "any",
    "none": None,
}
TYPES: tuple[str, ...] = tuple(PARENT)
INPUT_TYPES: tuple[str, ...] = tuple(t for t in TYPES if t != "none")
EFFECTS: tuple[str, ...] = ("none", "read-workspace", "write-workspace", "network", "exec", "sudo")


def ref_ancestors(t: str) -> tuple[str, ...]:
    out = [t]
    while PARENT[out[-1]] is not None:
        out.append(PARENT[out[-1]])  # type: ignore[arg-type]
    return tuple(out)


def ref_satisfies(task_type: str, required: str) -> bool:
    return required in ref_ancestors(task_type)


# -- manifests ---------------------------------------------------------------------------------


def norm_io(raw: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]] | None:
    """Reference normalisation of a *valid* io block (FR-002 defaults)."""
    if raw is None:
        return None
    inputs = raw.get("inputs")
    if inputs is None:
        all_, any_ = [], []
    elif isinstance(inputs, list):
        all_, any_ = list(inputs), []
    else:
        all_, any_ = list(inputs.get("all") or []), list(inputs.get("any") or [])
    outputs = raw["outputs"] if "outputs" in raw else ["any"]
    effects = raw["effects"] if "effects" in raw else ["exec"]
    return {
        "inputs_all": tuple(sorted(all_)), "inputs_any": tuple(sorted(any_)),
        "outputs": tuple(sorted(outputs)), "effects": tuple(sorted(effects)),
    }


def ref_survives(kind: str, available: bool, io: Mapping[str, Any] | None,
                 task_types: Iterable[str], requested: str | None,
                 allowed: Iterable[str]) -> bool:
    """FR-007 rules 1-4, evaluated straightforwardly in Python."""
    tts = set(task_types)
    allowed = set(allowed)
    if not available and kind != "skill":  # rule 1
        return False
    n = norm_io(io)
    if n is None:  # untyped: rules 3-4 pass (callers keep "exec" allowed, see SPEC-MAP)
        return True
    for e in n["effects"]:  # rule 2
        if e not in allowed:
            return False
    if "sudo" in n["effects"] and "exec" not in allowed:
        return False
    for req in n["inputs_all"]:  # rule 3
        if not any(ref_satisfies(t, req) for t in tts):
            return False
    if n["inputs_any"] and not any(ref_satisfies(t, req) for req in n["inputs_any"] for t in tts):
        return False
    if requested is None:  # rule 4
        return True
    return any(
        o == "any" or ref_satisfies(o, requested) or ref_satisfies(requested, o)
        for o in n["outputs"]
    )


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def entry(kind: str, name: str, description: str = "", io: Mapping[str, Any] | None = None,
          available: bool = True, sha: str | None = None) -> Any:
    """A ``CapabilityEntry`` whose ``io`` is ``parse_io(io)`` and whose sha is derived from the
    content (so equal content <=> equal sha)."""
    idx = cap("index")
    parsed = cap("manifest").parse_io(dict(io)) if io is not None else None
    if sha is None:
        sha = sha_text(json.dumps([kind, name, description, io], sort_keys=True))
    return idx.CapabilityEntry(
        kind=kind, name=name, description=description, io=parsed, content_sha256=sha,
        available=available,
    )


def open_index(path: Path) -> Any:
    return cap("index").CapabilityIndex(path)


def close_index(index: Any) -> None:
    close = getattr(index, "close", None)
    if callable(close):
        close()


def typing_of(types: Iterable[str] = (), requested: str | None = None) -> Any:
    return cap("task_types").TaskTyping(types=frozenset(types), requested_output=requested)


def run_select(index: Any, types: Iterable[str] = (), requested: str | None = None, *,
               query: str = "", ambient: Sequence[str] = ("text/query", "workspace"),
               allowed: Iterable[str] = EFFECTS, k: int = 8, ranker: Any = None) -> Any:
    return cap("select").select(
        index, typing_of(types, requested), query, ambient=tuple(ambient),
        allowed_effects=tuple(allowed), k=k, ranker=ranker,
    )


def _name_of(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        return str(item["name"])
    if hasattr(item, "name"):
        return str(item.name)
    if isinstance(item, (tuple, list)) and len(item) >= 2:
        return str(item[1])
    raise TypeError(f"cannot find a name in survivor {item!r}")


def table_ranker(scores: Mapping[str, float], calls: list | None = None) -> Any:
    """A custom ranker returning ``scores[name]`` (0.0 when absent) for every survivor."""

    def rank(query: str, survivors: Any) -> dict[str, float]:
        names = [_name_of(s) for s in survivors]
        if calls is not None:
            calls.append((query, names))
        return {n: float(scores.get(n, 0.0)) for n in names}

    return rank


def const_ranker(query: str, survivors: Any) -> dict[str, float]:
    return {_name_of(s): 0.0 for s in survivors}


# -- FR-008 lexical reference ------------------------------------------------------------------

_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def ref_lexical_order(query: str, caps: Sequence[tuple[str, str, str]]) -> list[str]:
    """``caps`` = ``(kind, name, description)`` survivors -> names ordered per FR-008."""
    q = tokens(query)
    cap_tokens = {(k, n): tokens(n.replace("-", " ").replace("_", " ")) | tokens(d)
                  for k, n, d in caps}
    df = {t: sum(1 for toks in cap_tokens.values() if t in toks) for t in q}
    scores = {
        key: sum(1.0 / math.log2(2 + df[t]) for t in q & toks) for key, toks in cap_tokens.items()
    }
    return [n for (k, n) in sorted(scores, key=lambda key: (-scores[key], key))]


def ref_lexical_scores(query: str, caps: Sequence[tuple[str, str, str]]) -> dict[str, float]:
    q = tokens(query)
    cap_tokens = {n: tokens(n.replace("-", " ").replace("_", " ")) | tokens(d) for _, n, d in caps}
    df = {t: sum(1 for toks in cap_tokens.values() if t in toks) for t in q}
    return {n: sum(1.0 / math.log2(2 + df[t]) for t in q & toks) for n, toks in cap_tokens.items()}


# -- FR-004 reference sync counts --------------------------------------------------------------


def ref_sync_counts(prev: Mapping[tuple[str, str], tuple[str, bool]],
                    new: Sequence[Any]) -> tuple[int, int, int, int]:
    """``prev`` = {(kind, name): (sha, available)} -> (added, updated, removed, unchanged)."""
    cur = {(e.kind, e.name): (e.content_sha256, bool(e.available)) for e in new}
    added = sum(1 for k in cur if k not in prev)
    removed = sum(1 for k in prev if k not in cur)
    updated = sum(1 for k in cur if k in prev and prev[k] != cur[k])
    unchanged = sum(1 for k in cur if k in prev and prev[k] == cur[k])
    return added, updated, removed, unchanged


def sync_tuple(result: Any) -> tuple[int, int, int, int]:
    return (result.added, result.updated, result.removed, result.unchanged)


# -- skills on disk ----------------------------------------------------------------------------

UNAVAILABLE = 'metadata: {"nanobot": {"requires": {"bins": ["h011-no-such-binary-zz"]}}}\n'


def write_skill(root: Path, name: str, description: str | None = None, *,
                io: Mapping[str, Any] | None = None, body: str | None = None,
                extra_front: str = "", front_name: str | None = None) -> Path:
    """``root/<name>/SKILL.md``; returns the SKILL.md path."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    front = f"name: {front_name or name}\n"
    if description is not None:
        front += f"description: {json.dumps(description)}\n"
    if io is not None:
        front += yaml.safe_dump({"io": json.loads(json.dumps(io))}, default_flow_style=None,
                                sort_keys=False)
    front += extra_front
    path = d / "SKILL.md"
    path.write_text(f"---\n{front}---\n\n{body or f'Do {name}.'}\n", encoding="utf-8")
    return path


# The fixture library used by the prompt-integration tests (FR-010/011/015, SC-001).
BUILTIN_LIBRARY: list[dict[str, Any]] = [
    {"name": "pdf-summarizer", "description": "Summarise a PDF document as markdown",
     "io": {"inputs": ["file:pdf"], "outputs": ["text/markdown"], "effects": ["read-workspace"]}},
    {"name": "git-reviewer", "description": "Review a git repository from its URL",
     "io": {"inputs": ["url/git"], "outputs": ["text/markdown"], "effects": ["network"]}},
    {"name": "csv-stats", "description": "Column statistics for CSV tables",
     "io": {"inputs": ["data/csv"], "outputs": ["data/json"], "effects": ["none"]}},
    {"name": "video-clipper", "description": "Cut clips out of online videos",
     "io": {"inputs": ["url/video"], "outputs": ["file:video"], "effects": ["network", "exec"]}},
    {"name": "image-captioner", "description": "Caption pictures",
     "io": {"inputs": ["file:image"], "outputs": ["text/markdown"], "effects": ["read-workspace"]}},
    {"name": "log-triage", "description": "Triage service logs",
     "io": {"inputs": ["text/log"], "outputs": ["text/markdown"], "effects": ["none"]}},
    {"name": "py-runner", "description": "Run python snippets",
     "io": {"inputs": ["text/code:python"], "outputs": ["text/log"], "effects": ["exec"]}},
    {"name": "web-reader", "description": "Read web pages",
     "io": {"inputs": ["url/web"], "outputs": ["text/markdown"], "effects": ["network"]}},
    {"name": "yaml-to-json", "description": "Convert YAML documents to JSON",
     "io": {"inputs": ["data/yaml"], "outputs": ["data/json"], "effects": ["none"]}},
    {"name": "audio-transcriber", "description": "Transcribe recordings",
     "io": {"inputs": ["file:audio"], "outputs": ["text/markdown"],
            "effects": ["read-workspace"]}},
    {"name": "archive-lister", "description": "List archive members",
     "io": {"inputs": ["file:archive"], "outputs": ["text/markdown"],
            "effects": ["read-workspace"]}},
    {"name": "office-converter", "description": "Convert office files",
     "io": {"inputs": {"any": ["file:office", "file:pdf"]}, "outputs": ["file:pdf"],
            "effects": ["read-workspace", "write-workspace"]}},
    {"name": "notes-helper", "description": "General note taking helper"},
    {"name": "always-guide", "description": "House rules for every turn",
     "extra_front": "always: true\n", "body": "ALWAYS-GUIDE-BODY follow the house rules."},
    {"name": "needs-bin", "description": "Inspect git history with a missing binary",
     "io": {"inputs": ["url/git"], "outputs": ["text/markdown"], "effects": ["exec"]},
     "extra_front": UNAVAILABLE},
]
WORKSPACE_LIBRARY: list[dict[str, Any]] = [
    {"name": "ws-pdf-tagger", "description": "Tag PDF files with JSON labels",
     "io": {"inputs": ["file:pdf"], "outputs": ["data/json"], "effects": ["read-workspace"]}},
]
PDF_QUERY = "Please summarise ~/docs/report.pdf as markdown"
GIT_QUERY = "Review https://github.com/acme/widget as markdown"


def build_library(root: Path) -> tuple[Path, Path]:
    """Fixture built-in skills dir and workspace under *root*: ``(builtin_dir, work_dir)``."""
    builtin = root / "bskills"
    work = root / "work"
    for spec in BUILTIN_LIBRARY:
        write_skill(builtin, spec["name"], spec["description"], io=spec.get("io"),
                    body=spec.get("body"), extra_front=spec.get("extra_front", ""))
    for spec in WORKSPACE_LIBRARY:
        write_skill(work / "skills", spec["name"], spec["description"], io=spec.get("io"))
    return builtin, work


# -- config / kernels / context builders -------------------------------------------------------


def config(caps: Mapping[str, Any] | None = None) -> Any:
    from nanobot.config.schema import Config

    return Config() if caps is None else Config(capabilities=dict(caps))


def make_kernel(state: Path, *, caps: Mapping[str, Any] | None, builtin: Path | None,
                work: Path, trace: Any) -> tuple[Any, Any]:
    from moeka import Environment, Kernel, ModelSpec
    from moeka.testing import FakeProvider
    from moeka.variants import Variant

    work.mkdir(parents=True, exist_ok=True)
    env = Environment.from_config(config(caps), state_dir=state, work_dir=work, trace=trace)
    kernel = Kernel(env, variant=Variant(builtin_skills_dir=builtin)) if builtin else Kernel(env)
    fake = FakeProvider(default="ok")
    kernel.llm.register_provider(
        "default", fake, ModelSpec(name="default", model="fake-main", provider="openai"),
    )
    return kernel, fake


def agent_of(kernel: Any, name: str = "h011") -> Any:
    from moeka.agents import AgentSpec

    return kernel.agent(AgentSpec(name=name))


def system_prompt_of(fake: Any, call: int = -1) -> str:
    msgs = fake.calls[call].messages
    assert msgs[0]["role"] == "system"
    return str(msgs[0]["content"])


def user_message_of(fake: Any, call: int = -1) -> str:
    return json.dumps(fake.calls[call].messages[-1].get("content"), ensure_ascii=False)


_CAPS_KWARGS = ("capabilities", "capabilities_config", "capability_config", "capabilities_cfg")


def make_ctx(work: Path, state: Path, *, caps: Mapping[str, Any] | None = None,
             builtin: Path | None = None, trace: Any = None) -> Any:
    """A ``ContextBuilder`` over *work* whose host env carries ``Config(capabilities=caps)``
    (``env.config.section("capabilities")``); when the constructor also takes a capabilities
    keyword (see ``_CAPS_KWARGS``) it is passed ``config.capabilities`` too."""
    from moeka import Environment
    from moeka.trace import MemoryTraceSink
    from moeka.variants import Variant
    from nanobot.agent.context import ContextBuilder

    work.mkdir(parents=True, exist_ok=True)
    cfg = config(caps)
    env = Environment.from_config(cfg, state_dir=state, work_dir=work,
                                  trace=trace if trace is not None else MemoryTraceSink())
    kwargs: dict[str, Any] = {"env": env.core}
    if builtin is not None:
        kwargs["variant"] = Variant(builtin_skills_dir=builtin)
    params = inspect.signature(ContextBuilder.__init__).parameters
    for name in _CAPS_KWARGS:
        if name in params and hasattr(cfg, "capabilities"):
            kwargs[name] = cfg.capabilities
    return ContextBuilder(work, **kwargs)


def build_prompt(ctx: Any, text: str | None, **kw: Any) -> str:
    return ctx.build_system_prompt(query=text, include_memory=False,
                                   include_memory_recent_history=False, **kw)


_SUMMARY_LINE = re.compile(r"^- \*\*(?P<name>[^*]+)\*\* — .*$", re.MULTILINE)


def summary_lines(prompt: str) -> list[str]:
    return [m.group(0) for m in _SUMMARY_LINE.finditer(prompt)]


def summary_names(prompt: str) -> list[str]:
    return [m.group("name") for m in _SUMMARY_LINE.finditer(prompt)]


def ev(sink: Any, name: str) -> list[dict[str, Any]]:
    return [e for e in sink.events if e.get("event") == name]


STAMP_KEYS = {"event", "trace_id", "span", "tags", "ts"}


def strip(e: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in e.items() if k not in STAMP_KEYS}


def att(mime: str | None = None, name: str | None = None) -> Any:
    """An attachment with exactly the given attributes (missing ones are absent)."""
    fields: dict[str, Any] = {}
    if mime is not None:
        fields["mime"] = mime
    if name is not None:
        fields["name"] = name
    return SimpleNamespace(**fields)


# -- SC-001 golden -----------------------------------------------------------------------------


def sc001_fixture(root: Path) -> tuple[Path, Path, Path]:
    """The SC-001 fixture workspace: ``(builtin_dir, work_dir, state_dir)``."""
    builtin, work = build_library(root)
    (work / "AGENTS.md").write_text("# Agents\n\nFixture agent instructions.\n", encoding="utf-8")
    write_skill(work / "skills", "ws-untyped", "An untyped workspace skill")
    return builtin, work, root / "state"


def normalise_prompt(prompt: str, root: Path) -> str:
    out = prompt
    for path, placeholder in sorted(
        ((str(root.resolve()), "<ROOT>"), (str(root), "<ROOT>")),
        key=lambda pair: len(pair[0]), reverse=True,
    ):
        out = out.replace(path, placeholder)
    out = re.sub(r"Python \d+\.\d+(\.\d+)?\S*", "Python <V>", out)
    return out


def canonical_golden(text: str) -> str:
    """*text* with each run of consecutive skills-summary lines sorted: the listing order inside
    a group is directory-iteration order, which is filesystem-dependent (the in-process SC-001
    comparisons check order exactly)."""
    out: list[str] = []
    run: list[str] = []
    for line in text.split("\n"):
        if _SUMMARY_LINE.fullmatch(line):
            run.append(line)
            continue
        out.extend(sorted(run))
        run = []
        out.append(line)
    out.extend(sorted(run))
    return "\n".join(out)


GOLDEN_SC001 = Path(__file__).resolve().parent / "golden_sc001_prompt.txt"
