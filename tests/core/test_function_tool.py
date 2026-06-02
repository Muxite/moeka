"""Tests for the FunctionTool adapter and JSON-schema derivation."""

from __future__ import annotations

from typing import Optional

import pytest

from nanobot.core.function_tool import FunctionTool, _schema_from_signature


def test_schema_basic_types_and_required():
    def fn(city: str, days: int = 3, ratio: float = 1.0, verbose: bool = False) -> str:
        """Get a forecast."""
        return ""

    schema = _schema_from_signature(fn)
    assert schema["type"] == "object"
    assert schema["properties"] == {
        "city": {"type": "string"},
        "days": {"type": "integer"},
        "ratio": {"type": "number"},
        "verbose": {"type": "boolean"},
    }
    assert schema["required"] == ["city"]


def test_schema_containers_and_optionals():
    def fn(tags: list[str], note: str | None = None, alt: Optional[int] = None):
        return ""

    schema = _schema_from_signature(fn)
    assert schema["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert schema["properties"]["note"] == {"type": ["string", "null"]}
    assert schema["properties"]["alt"] == {"type": ["integer", "null"]}
    assert schema["required"] == ["tags"]


def test_schema_untyped_is_unconstrained():
    def fn(a, b=1):
        return a

    schema = _schema_from_signature(fn)
    assert schema["properties"] == {"a": {}, "b": {}}
    assert schema["required"] == ["a"]


def test_schema_skips_var_args():
    def fn(x: int, *args, **kwargs):
        return x

    schema = _schema_from_signature(fn)
    assert list(schema["properties"]) == ["x"]


def test_metadata_defaults_from_function():
    def lookup(q: str) -> str:
        """First line is the description.

        Trailing paragraphs are ignored.
        """
        return q

    tool = FunctionTool(lookup)
    assert tool.name == "lookup"
    assert tool.description == "First line is the description."
    assert tool.read_only is False
    assert tool.to_schema()["function"]["name"] == "lookup"


def test_metadata_overrides():
    tool = FunctionTool(
        lambda q: q,
        name="search",
        description="Custom desc.",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        read_only=True,
    )
    assert tool.name == "search"
    assert tool.description == "Custom desc."
    assert tool.read_only is True


def test_cast_and_validate_roundtrip():
    def fn(city: str, days: int) -> str:
        return ""

    tool = FunctionTool(fn)
    cast = tool.cast_params({"city": "Paris", "days": "5"})  # string -> int
    assert cast["days"] == 5
    assert tool.validate_params(cast) == []
    assert tool.validate_params(tool.cast_params({"days": 5})) == ["missing required city"]


@pytest.mark.asyncio
async def test_execute_sync_and_async():
    def add(a: int, b: int) -> str:
        return str(a + b)

    async def amul(a: int, b: int) -> str:
        return str(a * b)

    assert await FunctionTool(add).execute(a=2, b=3) == "5"
    assert await FunctionTool(amul).execute(a=2, b=3) == "6"


@pytest.mark.asyncio
async def test_execute_coerces_non_str_return():
    def count() -> int:
        return 42

    # Non-(str|list) returns are stringified so the loop can render them.
    assert await FunctionTool(count).execute() == "42"


def test_not_discoverable():
    # Dynamic instances must never be picked up by the plugin auto-loader.
    assert FunctionTool._plugin_discoverable is False


def test_rejects_non_callable():
    with pytest.raises(TypeError):
        FunctionTool(123)  # type: ignore[arg-type]
