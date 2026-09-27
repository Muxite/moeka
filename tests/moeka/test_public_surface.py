"""Snapshot of the stable ``moeka`` surface: additions and removals are deliberate."""

from __future__ import annotations

import importlib

import pytest

SURFACE: dict[str, list[str]] = {
    "moeka": [
        "CredentialResolver",
        "Environment",
        "Kernel",
        "ModelSpec",
        "Paths",
        "ProviderSpec",
        "Sampling",
        "StaticCredentialResolver",
    ],
    "moeka.budget": ["Budget", "CallEstimate", "CapBudget", "ResponseCache"],
    "moeka.errors": [
        "AuthError",
        "BudgetExceeded",
        "ContentFilterError",
        "LLMError",
        "LLMTimeoutError",
        "ModelNotFound",
        "ParseError",
        "QuotaError",
        "RateLimitError",
        "TransientError",
        "TruncatedError",
        "UnsupportedRequestError",
    ],
    "moeka.llm": [
        "BatchResult",
        "Completion",
        "GenerateOptions",
        "LLM",
        "Request",
        "Sampling",
        "TextStream",
        "Usage",
        "assistant",
        "image_part",
        "system",
        "user",
    ],
    "moeka.testing": ["FakeCall", "FakeProvider", "error", "reply"],
    "moeka.tools": [
        "CapabilityRequest",
        "DefaultPolicy",
        "FunctionTool",
        "IntersectionPolicy",
        "PermissionPolicy",
        "PluginRegistry",
        "Tool",
    ],
    "moeka.trace": [
        "EVENTS", "FanoutSink", "JsonlTraceSink", "LoguruTraceSink", "MemoryTraceSink",
        "NullTraceSink", "TraceSink", "Tracer",
    ],
}


@pytest.mark.parametrize("module", sorted(SURFACE))
def test_all_snapshot(module: str) -> None:
    mod = importlib.import_module(module)
    assert sorted(mod.__all__) == SURFACE[module]
    for name in mod.__all__:
        assert getattr(mod, name) is not None, f"{module}.{name}"


def test_submodules_are_exactly_the_snapshot() -> None:
    import pkgutil

    import moeka

    found = {f"moeka.{m.name}" for m in pkgutil.iter_modules(moeka.__path__)}
    assert found | {"moeka"} == set(SURFACE)


def test_reexports_are_the_implementation_objects() -> None:
    import moeka
    from nanobot.kernel.hostenv import Environment
    from nanobot.kernel.kernel import Kernel

    assert moeka.Environment is Environment
    assert moeka.Kernel is Kernel


def test_errors_are_the_implementation_classes() -> None:
    import moeka.errors
    from nanobot.kernel import llm_errors

    for name in moeka.errors.__all__:
        assert getattr(moeka.errors, name) is getattr(llm_errors, name)
    assert not hasattr(moeka.errors, "classify")
    assert issubclass(moeka.errors.LLMTimeoutError, TimeoutError)
