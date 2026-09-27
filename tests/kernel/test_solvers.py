"""Deterministic-solver registry (Task 15, invariant I6).

A task a registered solver can answer must never reach the provider: the
end-to-end tests use a provider whose ``chat``/``chat_with_retry`` raise, so a
passing test proves zero LLM calls rather than only checking a return value.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from nanobot.core import MoekaCore
from nanobot.kernel import solvers as solvers_mod
from nanobot.kernel.solvers import Solved, SolverRegistry

complete_mod = importlib.import_module("nanobot.api.complete")

_CONFIG = {"providers": {"openrouter": {"apiKey": "sk-test"}}}


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    """Isolate the process-wide default registry per test."""
    registry = SolverRegistry()
    monkeypatch.setattr(solvers_mod, "_DEFAULT_REGISTRY", registry)
    return registry


@pytest.fixture
def caplog_loguru():
    from loguru import logger

    msgs: list[str] = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="DEBUG")
    yield msgs
    logger.remove(hid)


class _ExplodingProvider:
    """Any call proves the solver fast path leaked to the LLM."""

    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        raise AssertionError("provider.chat must not be called")

    async def chat_with_retry(self, **kwargs):
        self.calls += 1
        raise AssertionError("provider.chat_with_retry must not be called")


class _StubProvider:
    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat_with_retry(self, *, messages, max_tokens=None, temperature=None):
        self.calls.append(messages)
        return SimpleNamespace(
            content=self._replies.pop(0), finish_reason="stop", error_type=None
        )


def _install(monkeypatch, provider):
    import nanobot.providers.factory as factory

    made: list[object] = []

    def _make(config, *, preset_name=None, preset=None, model=None):
        made.append(provider)
        return provider

    monkeypatch.setattr(factory, "make_provider", _make)
    return made


class _Date(BaseModel):
    year: int
    month: int


def _iso_month(payload: dict) -> Solved | None:
    import re

    m = re.fullmatch(r"(\d{4})-(\d{2})", payload.get("prompt", ""))
    if m is None:
        return None
    return Solved({"year": int(m.group(1)), "month": int(m.group(2))}, "iso_month")


# ---------------------------------------------------------------------------
# register_solver / try_solve
# ---------------------------------------------------------------------------


def test_matching_solver_returns_solved():
    solvers_mod.register_solver("date", _iso_month)
    out = solvers_mod.try_solve("date", {"prompt": "2026-09"})
    assert out == Solved({"year": 2026, "month": 9}, "iso_month")
    assert out.solver_name == "iso_month"


def test_non_matching_payload_falls_through():
    solvers_mod.register_solver("date", _iso_month)
    assert solvers_mod.try_solve("date", {"prompt": "next tuesday"}) is None


def test_unregistered_task_type_returns_none():
    assert solvers_mod.try_solve("nothing", {"prompt": "x"}) is None


def test_multiple_solvers_try_in_registration_order():
    order: list[str] = []

    def first(payload):
        order.append("first")
        return None

    def second(payload):
        order.append("second")
        return Solved("two", "second")

    def third(payload):
        order.append("third")
        return Solved("three", "third")

    for fn in (first, second, third):
        solvers_mod.register_solver("t", fn)
    out = solvers_mod.try_solve("t", {})
    assert out is not None and out.value == "two"
    assert order == ["first", "second"]


def test_raising_solver_is_swallowed_and_logged(caplog_loguru):
    def broken(payload):
        raise RuntimeError("kaboom")

    def good(payload):
        return Solved(1, "good")

    solvers_mod.register_solver("t", broken)
    assert solvers_mod.try_solve("t", {}) is None
    assert any("kaboom" in m for m in caplog_loguru)

    solvers_mod.register_solver("t", good)
    assert solvers_mod.try_solve("t", {}) == Solved(1, "good")


def test_non_solved_return_is_treated_as_no_solution():
    solvers_mod.register_solver("t", lambda payload: {"not": "Solved"})
    assert solvers_mod.try_solve("t", {}) is None


def test_input_schema_rejects_payload_before_solver_runs():
    ran: list[dict] = []

    def solver(payload):
        ran.append(payload)
        return Solved("ok", "s")

    schema = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
    }
    solvers_mod.register_solver("t", solver, input_schema=schema)
    assert solvers_mod.try_solve("t", {"prompt": 7}) is None
    assert solvers_mod.try_solve("t", {}) is None
    assert ran == []  # distinct from "solver returned None": it never ran
    assert solvers_mod.try_solve("t", {"prompt": "fine"}) == Solved("ok", "s")
    assert ran == [{"prompt": "fine"}]


def test_schema_rejection_is_per_solver():
    """A payload one solver's schema rejects still reaches the next solver."""
    solvers_mod.register_solver(
        "t", lambda p: Solved("strict", "a"), input_schema={"type": "object", "required": ["x"]}
    )
    solvers_mod.register_solver("t", lambda p: Solved("loose", "b"))
    assert solvers_mod.try_solve("t", {}) == Solved("loose", "b")


def test_explicit_registry_is_independent_of_default():
    reg = SolverRegistry()
    reg.register("t", lambda p: Solved(1, "local"))
    assert reg.try_solve("t", {}) == Solved(1, "local")
    assert solvers_mod.try_solve("t", {}) is None
    assert reg.has("t") and not solvers_mod.default_registry().has("t")


def test_register_rejects_bad_arguments():
    with pytest.raises(ValueError):
        solvers_mod.register_solver("", _iso_month)
    with pytest.raises(TypeError):
        solvers_mod.register_solver("t", "not callable")  # type: ignore[arg-type]


def test_solved_is_frozen():
    s = Solved(1, "x")
    with pytest.raises(Exception):
        s.value = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# acomplete_json fast path
# ---------------------------------------------------------------------------


async def test_acomplete_json_solved_makes_zero_provider_calls(monkeypatch):
    provider = _ExplodingProvider()
    made = _install(monkeypatch, provider)
    solvers_mod.register_solver("date", _iso_month)

    out = await complete_mod.acomplete_json(
        "2026-09", task_type="date", config_dict=dict(_CONFIG)
    )
    assert out == {"year": 2026, "month": 9}
    assert provider.calls == 0
    assert made == []  # the provider is never even built


async def test_acomplete_json_solved_validates_through_model_cls(monkeypatch):
    provider = _ExplodingProvider()
    _install(monkeypatch, provider)
    solvers_mod.register_solver("date", _iso_month)

    out = await complete_mod.acomplete_json(
        "2026-09", model_cls=_Date, task_type="date", config_dict=dict(_CONFIG)
    )
    assert isinstance(out, _Date)
    assert (out.year, out.month) == (2026, 9)
    assert provider.calls == 0


async def test_acomplete_json_explicit_task_payload(monkeypatch):
    provider = _ExplodingProvider()
    _install(monkeypatch, provider)
    seen: list[dict] = []

    def solver(payload):
        seen.append(payload)
        return Solved({"k": payload["k"]}, "echo")

    solvers_mod.register_solver("echo", solver)
    out = await complete_mod.acomplete_json(
        "ignored prompt", task_type="echo", task_payload={"k": 3}, config_dict=dict(_CONFIG)
    )
    assert out == {"k": 3}
    assert seen == [{"k": 3}]


async def test_acomplete_json_invalid_solver_value_falls_through_to_llm(monkeypatch):
    """A solver value model_cls rejects is treated like an invalid LLM reply: not returned.

    The LLM path retries on a validation failure; the solver analogue is falling
    through to the LLM, which then answers normally.
    """
    stub = _StubProvider(['{"year": 2000, "month": 1}'])
    _install(monkeypatch, stub)
    solvers_mod.register_solver("date", lambda p: Solved({"year": "not-int"}, "bad"))

    out = await complete_mod.acomplete_json(
        "p", model_cls=_Date, task_type="date", config_dict=dict(_CONFIG)
    )
    assert isinstance(out, _Date) and out.year == 2000
    assert len(stub.calls) == 1


async def test_acomplete_json_non_matching_solver_uses_llm(monkeypatch):
    stub = _StubProvider(['{"year": 1999, "month": 12}'])
    _install(monkeypatch, stub)
    solvers_mod.register_solver("date", _iso_month)

    out = await complete_mod.acomplete_json(
        "next tuesday", task_type="date", config_dict=dict(_CONFIG)
    )
    assert out == {"year": 1999, "month": 12}
    assert len(stub.calls) == 1


# ---------------------------------------------------------------------------
# Regression: no task_type / unregistered task_type is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task_type", [None, "unregistered"])
async def test_acomplete_json_no_solver_unchanged_plain(monkeypatch, task_type):
    stub = _StubProvider(["not json", '{"ok": true}'])
    _install(monkeypatch, stub)
    solvers_mod.register_solver("other", lambda p: Solved({"no": 1}, "other"))

    kwargs = {} if task_type is None else {"task_type": task_type}
    out = await complete_mod.acomplete_json("p", config_dict=dict(_CONFIG), **kwargs)
    assert out == {"ok": True}
    assert len(stub.calls) == 2


@pytest.mark.parametrize("task_type", [None, "unregistered"])
async def test_acomplete_json_no_solver_unchanged_model_cls(monkeypatch, task_type):
    stub = _StubProvider(['{"year": 2020, "month": 2}'])
    _install(monkeypatch, stub)
    kwargs = {} if task_type is None else {"task_type": task_type}
    out = await complete_mod.acomplete_json(
        "p", model_cls=_Date, config_dict=dict(_CONFIG), **kwargs
    )
    assert isinstance(out, _Date) and out.month == 2
    assert len(stub.calls) == 1


@pytest.mark.parametrize("task_type", [None, "unregistered"])
async def test_acomplete_json_no_solver_unchanged_error(monkeypatch, task_type):
    stub = _StubProvider(["nope", "nope"])
    _install(monkeypatch, stub)
    kwargs = {} if task_type is None else {"task_type": task_type}
    with pytest.raises(ValueError, match="after 2 attempt"):
        await complete_mod.acomplete_json(
            "p", retries=1, config_dict=dict(_CONFIG), **kwargs
        )
    assert len(stub.calls) == 2


async def test_acomplete_json_without_task_type_never_consults_registry(monkeypatch):
    stub = _StubProvider(['{"ok": 1}'])
    _install(monkeypatch, stub)

    def _boom(*a, **k):
        raise AssertionError("registry consulted without task_type")

    monkeypatch.setattr(solvers_mod, "try_solve", _boom)
    assert await complete_mod.acomplete_json("p", config_dict=dict(_CONFIG)) == {"ok": 1}


# ---------------------------------------------------------------------------
# MoekaCore.think_structured
# ---------------------------------------------------------------------------


async def test_think_structured_solved_makes_zero_provider_calls(monkeypatch):
    provider = _ExplodingProvider()
    _install(monkeypatch, provider)
    solvers_mod.register_solver("date", _iso_month)

    out = await MoekaCore.think_structured(
        "2026-09", model_cls=_Date, task_type="date", config_dict=dict(_CONFIG)
    )
    assert isinstance(out, _Date) and out.year == 2026
    assert provider.calls == 0

    plain = await MoekaCore.think_structured(
        "2026-09", task_type="date", config_dict=dict(_CONFIG)
    )
    assert plain == {"year": 2026, "month": 9}
    assert provider.calls == 0


@pytest.mark.parametrize("task_type", [None, "unregistered"])
async def test_think_structured_no_solver_unchanged(monkeypatch, task_type):
    stub = _StubProvider(['{"year": 2021, "month": 3}', '{"a": 1}'])
    _install(monkeypatch, stub)
    kwargs = {} if task_type is None else {"task_type": task_type}
    out = await MoekaCore.think_structured(
        "p", model_cls=_Date, config_dict=dict(_CONFIG), **kwargs
    )
    assert isinstance(out, _Date) and out.year == 2021
    plain = await MoekaCore.think_structured("p", config_dict=dict(_CONFIG), **kwargs)
    assert plain == {"a": 1}
    assert len(stub.calls) == 2


async def test_think_structured_omits_task_type_when_not_given(monkeypatch):
    """Existing delegation contract: no task_type keyword leaks into the forward."""
    captured: dict = {}

    async def fake(prompt, *, schema=None, model_cls=None, retries=2, **kw):
        captured.update(kw)
        return {}

    monkeypatch.setattr(complete_mod, "acomplete_json", fake)
    await MoekaCore.think_structured("q", model="m")
    assert captured == {"model": "m"}
    await MoekaCore.think_structured("q", task_type="x")
    assert captured["task_type"] == "x"


# ---------------------------------------------------------------------------
# Import discipline
# ---------------------------------------------------------------------------


def test_solvers_module_is_import_cheap():
    """Beyond the ``nanobot.kernel`` package itself, solvers.py imports nothing new."""
    import subprocess
    import sys

    probe = (
        "import sys; import nanobot.kernel; before = set(sys.modules); "
        "import nanobot.kernel.solvers; "
        "print(','.join(sorted(set(sys.modules) - before)))"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    added = [m for m in proc.stdout.strip().split(",") if m]
    assert added == ["nanobot.kernel.solvers"]
