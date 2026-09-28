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
        "PathsOverlapError",
        "ProviderSpec",
        "Sampling",
        "StaticCredentialResolver",
    ],
    "moeka.agents": [
        "Agent", "AgentSpec", "AgentStream", "AskUser", "RunLimits", "RunResult",
        "StopReason", "StreamEvent", "StreamEventType", "SyncAgentStream", "ToolInfo",
    ],
    "moeka.budget": ["Budget", "CallEstimate", "CapBudget", "ModelCallEvent", "ResponseCache"],
    "moeka.epistemics": [
        "ArtifactError",
        "ArtifactIncompleteError",
        "ArtifactKindMismatchError",
        "ArtifactNotFoundError",
        "ArtifactResult",
        "ArtifactValidationError",
        "CitationError",
        "CommitReady",
        "Divergence",
        "Epistemics",
        "FactRecord",
        "Question",
        "UnknownKindError",
    ],
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
        "Solved",
        "SolverRegistry",
        "SyncTextStream",
        "TextStream",
        "Usage",
        "assistant",
        "image_part",
        "system",
        "user",
    ],
    "moeka.memory": ["DocStore", "Hit"],
    "moeka.sessions": [
        "Checkpoint", "CheckpointMismatch", "Session", "SessionBusyError", "SessionInfo",
        "SessionSnapshot", "Sessions",
    ],
    "moeka.testing": ["FakeCall", "FakeProvider", "error", "reply"],
    "moeka.tools": [
        "CapabilityRequest",
        "DefaultPolicy",
        "FunctionTool",
        "IntersectionPolicy",
        "MCPServer",
        "OfflinePolicy",
        "PermissionPolicy",
        "PluginRegistry",
        "Tool",
    ],
    "moeka.trace": [
        "EVENTS", "FanoutSink", "JsonlTraceSink", "LoguruTraceSink", "MemoryTraceSink",
        "NullTraceSink", "TraceSink", "Tracer",
    ],
    "moeka.variants": ["Fingerprint", "Variant"],
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

    import moeka.agents
    import moeka.budget
    import moeka.llm
    from nanobot.kernel.agent import SyncAgentStream
    from nanobot.kernel.env import PathsOverlapError
    from nanobot.kernel.ledger import LedgerEvent
    from nanobot.kernel.llm import SyncTextStream

    assert moeka.budget.ModelCallEvent is LedgerEvent
    assert moeka.PathsOverlapError is PathsOverlapError
    assert issubclass(moeka.PathsOverlapError, ValueError)
    assert moeka.llm.SyncTextStream is SyncTextStream
    assert moeka.agents.SyncAgentStream is SyncAgentStream


def test_errors_are_the_implementation_classes() -> None:
    import moeka.errors
    from nanobot.kernel import llm_errors

    for name in moeka.errors.__all__:
        assert getattr(moeka.errors, name) is getattr(llm_errors, name)
    assert not hasattr(moeka.errors, "classify")
    assert issubclass(moeka.errors.LLMTimeoutError, TimeoutError)
