"""Tests for secret redaction helpers and the two log sites that use them."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger as loguru_logger

from nanobot.security.redact import redact_text, redact_value

FAKE_KEY = "sk-abcdefghijklmnopqrstuvwx"


class TestRedactText:
    def test_sk_token_masked_benign_words_kept(self):
        out = redact_text(f"key {FAKE_KEY} for the build")
        assert FAKE_KEY not in out
        assert "<redacted>" in out
        assert "for the build" in out

    def test_authorization_header_masked(self):
        out = redact_text("Authorization: Bearer abc.def.ghi")
        assert "abc.def.ghi" not in out

    def test_name_value_forms_masked(self):
        assert "hunter2secret" not in redact_text("api_key=hunter2secret")
        assert "hunter2secret" not in redact_text("password: hunter2secret")

    def test_bare_bearer_masked(self):
        assert "xyz123abc456" not in redact_text("got Bearer xyz123abc456 here")

    def test_benign_unchanged(self):
        s = "a cat on the moon, 16:9"
        assert redact_text(s) == s

    def test_none_stays_none(self):
        assert redact_text(None) is None


class TestRedactValue:
    def test_recurses(self):
        value = {
            "a": [f"x {FAKE_KEY}", ("password=hunter2secret", 3)],
            "b": {"c": "Bearer xyz123abc456"},
            "n": 7,
            "z": None,
        }
        out = redact_value(value)
        flat = repr(out)
        assert FAKE_KEY not in flat
        assert "hunter2secret" not in flat
        assert "xyz123abc456" not in flat
        assert out["n"] == 7
        assert out["z"] is None
        assert isinstance(out["a"][1], tuple)
        assert out["a"][1][1] == 3

    def test_does_not_mutate_input(self):
        value = {"k": FAKE_KEY}
        redact_value(value)
        assert value == {"k": FAKE_KEY}


_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 16).decode()


class _Resp:
    status_code = 200
    text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"data": [{"b64_json": _PNG}]}


class _Client:
    async def post(self, url: str, **kwargs: Any) -> _Resp:
        return _Resp()


@pytest.mark.asyncio
async def test_custom_image_request_log_redacted_and_debug_only():
    from nanobot.providers.image_generation import CustomImageGenerationClient

    client = CustomImageGenerationClient(
        api_key="unused",
        api_base="https://custom.example/v1?api_key=sk-basesecretabcdefghij",
        extra_body={"api_key": FAKE_KEY, "note": f"tok {FAKE_KEY}"},
        client=_Client(),  # type: ignore[arg-type]
    )
    records, hid = _capture_levels()
    try:
        await client.generate(prompt="draw", model="m")
    finally:
        loguru_logger.remove(hid)
    joined = "\n".join(text for _, text in records)
    assert "sk-" not in joined
    assert "Custom Images API request" in joined
    assert all(
        level == "DEBUG" for level, text in records if "Custom Images API request" in text
    )


def _capture_levels():
    records: list[tuple[str, str]] = []
    hid = loguru_logger.add(
        lambda m: records.append((m.record["level"].name, str(m))), level="DEBUG"
    )
    return records, hid


@pytest.mark.asyncio
async def test_subagent_hook_arguments_redacted():
    from nanobot.agent.hook import AgentHookContext
    from nanobot.agent.subagent import _SubagentHook

    hook = _SubagentHook("t1")
    call = SimpleNamespace(
        name="exec",
        arguments={"command": f"curl -H 'Authorization: Bearer {FAKE_KEY}' x", "api_key": FAKE_KEY},
    )
    ctx = AgentHookContext(iteration=0, messages=[], tool_calls=[call])  # type: ignore[list-item]
    records, hid = _capture_levels()
    try:
        await hook.before_execute_tools(ctx)
    finally:
        loguru_logger.remove(hid)
    joined = "\n".join(text for _, text in records)
    assert "sk-" not in joined
    assert "Subagent [t1] executing" in joined


class TestRedactBounded:
    @pytest.mark.parametrize(
        "text",
        [
            "key" * 30000,
            "a" + "key" * 30000,
            "token: " + " " * 100000,
            "Bearer " + "x" * 100000,
            "key=" + "a" * 100000,
        ],
        ids=["keys", "a-keys", "token-spaces", "bearer-x", "key-eq-a"],
    )
    def test_pathological_input_is_fast(self, text):
        import time

        start = time.perf_counter()
        redact_text(text)
        assert time.perf_counter() - start < 1.0

    def test_secret_at_start_of_long_string_still_masked(self):
        out = redact_text("api_key=sk-abcdefghijklmnop " + "a" * 100000)
        assert "sk-abcdefghijklmnop" not in out
        assert len(out) < 30000


@pytest.mark.asyncio
async def test_subagent_hook_redaction_lazy_when_debug_disabled(monkeypatch):
    from nanobot.agent import subagent as subagent_mod
    from nanobot.agent.hook import AgentHookContext

    calls = {"n": 0}
    real = subagent_mod.redact_value

    def counting(value):
        calls["n"] += 1
        return real(value)

    monkeypatch.setattr(subagent_mod, "redact_value", counting)
    hook = subagent_mod._SubagentHook("t2")
    call = SimpleNamespace(name="exec", arguments={"api_key": FAKE_KEY})
    ctx = AgentHookContext(iteration=0, messages=[], tool_calls=[call])  # type: ignore[list-item]

    loguru_logger.remove()  # drop all sinks; no handler accepts DEBUG
    try:
        await hook.before_execute_tools(ctx)
        assert calls["n"] == 0
        records: list[str] = []
        hid = loguru_logger.add(lambda m: records.append(str(m)), level="DEBUG")
        await hook.before_execute_tools(ctx)
        loguru_logger.remove(hid)
        assert calls["n"] == 1
        assert FAKE_KEY not in "".join(records)
    finally:
        loguru_logger.add(lambda m: None, level="DEBUG")
