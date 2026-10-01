"""Helpers for the 006 held-out tests: environments, scripted tool calls, and reference
implementations of the spec's formulas (FR-003/FR-004 skills records, FR-015 digest)."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from moeka import Environment, ModelSpec, ProviderSpec
from nanobot.providers.base import LLMResponse, LLMUsage, ToolCallRequest

MAIN = ModelSpec(name="main", model="fake-main", provider="openai")

# tool.call keys before this spec (contract list minus args_digest)
TOOL_CALL_KEYS_BEFORE = {
    "session_key", "iteration", "tool", "call_id", "ok", "args_valid", "error_kind", "error",
    "duration_ms",
}
STAMP_KEYS = {"event", "trace_id", "span", "tags", "ts"}


def repo_root() -> Path:
    import importlib.util

    spec = importlib.util.find_spec("nanobot")
    assert spec is not None and spec.origin is not None
    return Path(os.environ.get("MOEKA_HELDOUT_REPO") or Path(spec.origin).resolve().parents[1])


def make_env(root: Path, state: str, work: Path, trace: Any) -> Environment:
    return Environment.for_host(
        state_dir=root / state,
        work_dir=work,
        credentials={"oa": "sk-test"},
        providers=[ProviderSpec(name="openai", credential="oa")],
        models=[ModelSpec(name="main", model="gpt-4.1", provider="openai")],
        default_model="main",
        trace=trace,
    )


def tc(name: str, args: Any, call_id: str = "c1") -> LLMResponse:
    return calls((name, args, call_id))


def calls(*items: tuple[str, Any, str]) -> LLMResponse:
    """One model response carrying several tool calls ``(name, arguments, id)``."""
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=cid, name=name, arguments=args) for name, args, cid in items],
        finish_reason="tool_calls",
        usage=LLMUsage.reported(input_tokens=10, output_tokens=5),
    )


def tools_of(agent: Any) -> list[Any]:
    """``agent.tools()`` per the contract (tolerates the pre-spec property form)."""
    t = agent.tools
    return t() if callable(t) else t


def tool_info(agent: Any, name: str) -> Any:
    return next(t for t in tools_of(agent) if t.name == name)


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(v) for v in value]
    return value


def ev(sink: Any, name: str) -> list[dict[str, Any]]:
    return [e for e in sink.events if e.get("event") == name]


def strip(e: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in e.items() if k not in STAMP_KEYS}


# -- FR-015 reference -------------------------------------------------------------------------


def ref_args_digest(arguments: Any) -> str | None:
    value = arguments
    if value is None:
        value = {}
    elif isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = arguments
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- FR-003 / FR-004 reference ----------------------------------------------------------------


def _skipped(rel: Path) -> bool:
    return any(p == "__pycache__" or p.startswith(".") for p in rel.parts) or rel.name.endswith(".pyc")


def file_records(name: str, skill_dir: Path) -> list[tuple[str, str]]:
    """Records of a file-based skill. Test trees use file symlinks only (no dir symlinks)."""
    out: list[tuple[str, str]] = []
    for root, _dirs, files in os.walk(skill_dir):
        for fname in files:
            p = Path(root) / fname
            rel = p.relative_to(skill_dir)
            if _skipped(rel):
                continue
            if not p.is_file():  # dangling symlink or special file
                continue
            out.append((f"file:{name}/{rel.as_posix()}", hashlib.sha256(p.read_bytes()).hexdigest()))
    return out


def inline_record(name: str, description: Any, content: Any, metadata: Any) -> tuple[str, str]:
    obj = {"name": name, "description": description, "content": content, "metadata": metadata}
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (f"inline:{name}", sha_text(text))


def skills_component(records: Iterable[tuple[str, str]]) -> str:
    body = "".join(f"{k}\n{v}\n" for k, v in sorted(records, key=lambda r: r[0]))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def fingerprint_digest(components: Mapping[str, str]) -> str:
    return sha_text("\n".join(f"{n}={h}" for n, h in sorted(components.items())))


def write_skill(root: Path, name: str, body: str = "", *, description: str | None = None,
                extra_front: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    desc = description or f"the {name} skill"
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n{extra_front}---\n\n{body or f'Do {name}.'}\n",
        encoding="utf-8",
    )
    return d
