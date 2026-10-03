"""Spec 010 FR-003 / SC-003: image, transcription and embedding calls are metered."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nanobot.llm_usage import USAGE_DB_FILENAME, get_llm_usage_store
from nanobot.llm_usage.context import llm_usage_source
from nanobot.llm_usage.query import UsageFilter, records
from nanobot.llm_usage.side_calls import record_side_call, set_side_call_recorder
from nanobot.providers.image_generation import (
    GeneratedImageResponse,
    ImageGenerationError,
    ImageGenerationProvider,
    OpenRouterImageGenerationClient,
)
from nanobot.providers.transcription import GroqTranscriptionProvider

KEY = "sk-test-FAKE-0123456789abcdef"
PNG_DATA_URL = "data:image/png;base64,iVBORw0KGgo="


def _fp(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _rows(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / USAGE_DB_FILENAME
    if not path.exists():
        return []
    get_llm_usage_store(data_dir=data_dir).flush()
    with closing(sqlite3.connect(path)) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM llm_calls ORDER BY id")]


class _FakeImages(ImageGenerationProvider):
    provider_name = "fakeimg"
    missing_key_message = "no key"

    def __init__(self, *, fail: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail = fail

    async def generate(self, *, prompt: str, model: str, reference_images=None,
                       aspect_ratio=None, image_size=None) -> GeneratedImageResponse:
        if not self.api_key:
            raise ImageGenerationError(self.missing_key_message)
        if self.fail:
            raise ImageGenerationError("upstream 500")
        return GeneratedImageResponse(
            images=[PNG_DATA_URL, PNG_DATA_URL], content="",
            raw={"usage": {"prompt_tokens": 12, "completion_tokens": 3, "cost": 0.04}},
        )


class _DelegatingImages(_FakeImages):
    provider_name = "delegating"

    async def generate(self, **kwargs: Any) -> GeneratedImageResponse:
        return await super().generate(**kwargs)


class _FakeEmbedder:
    model_name = "fake-mini"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0 / 384**0.5] * 384 for _ in texts]  # numpy-free stand-in vectors


async def test_sc003_image_call_adds_one_row(tmp_path: Path) -> None:
    client = _FakeImages(api_key=KEY)
    client.usage_data_dir = tmp_path
    with llm_usage_source("user"):
        response = await client.generate(prompt="a cat", model="img-1")
    assert len(response.images) == 2
    [row] = _rows(tmp_path)
    assert (row["purpose"], row["provider"], row["model"]) == ("image", "fakeimg", "img-1")
    assert row["key_fp"] == _fp(KEY) and row["source"] == "user"
    assert (row["input_tokens"], row["output_tokens"]) == (12, 3)
    assert row["cost_usd"] == pytest.approx(0.04) and row["cost_billed"] == 1
    assert '"units": 2' in row["tags"] and '"kind": "image"' in row["tags"]
    [record] = records(get_llm_usage_store(data_dir=tmp_path), UsageFilter())
    assert record["purpose"] == "image" and record["key_fp"] == _fp(KEY)


async def test_image_failure_is_one_error_row_and_missing_key_none(tmp_path: Path) -> None:
    failing = _FakeImages(api_key=KEY, fail=True)
    failing.usage_data_dir = tmp_path
    with pytest.raises(ImageGenerationError, match="upstream"):
        await failing.generate(prompt="x", model="img-1")
    keyless = _FakeImages(api_key=None)
    keyless.usage_data_dir = tmp_path
    with pytest.raises(ImageGenerationError, match="no key"):
        await keyless.generate(prompt="x", model="img-1")
    [row] = _rows(tmp_path)
    assert row["finish_reason"] == "error" and row["purpose"] == "image"


async def test_nested_super_generate_records_once(tmp_path: Path) -> None:
    client = _DelegatingImages(api_key=KEY)
    client.usage_data_dir = tmp_path
    await client.generate(prompt="x", model="img-2")
    assert [r["provider"] for r in _rows(tmp_path)] == ["delegating"]


async def test_real_openrouter_client_is_metered(tmp_path: Path) -> None:
    class _Resp:
        status_code = 200
        text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "ok", "images": [
                {"image_url": {"url": PNG_DATA_URL}}]}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1}}

    class _Client:
        async def post(self, url: str, **_kw: Any) -> _Resp:
            return _Resp()

    client = OpenRouterImageGenerationClient(api_key=KEY, client=_Client())  # type: ignore[arg-type]
    client.usage_data_dir = tmp_path
    await client.generate(prompt="draw", model="openai/gpt-5.4-image-2")
    [row] = _rows(tmp_path)
    assert (row["provider"], row["key_fp"], row["input_tokens"]) == ("openrouter", _fp(KEY), 5)
    assert row["cost_usd"] is None


async def test_sc003_transcription_call_adds_one_row(tmp_path: Path) -> None:
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 96)
    provider = GroqTranscriptionProvider(api_key=KEY)
    provider.usage_data_dir = tmp_path
    response = httpx.Response(
        200, json={"text": "hello"},
        request=httpx.Request("POST", "https://example.test/audio/transcriptions"),
    )
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=response)):
        assert await provider.transcribe(audio) == "hello"
    [row] = _rows(tmp_path)
    assert (row["purpose"], row["provider"], row["model"]) == (
        "transcription", "groq", "whisper-large-v3",
    )
    assert row["key_fp"] == _fp(KEY) and row["finish_reason"] == "stop"
    assert '"units": 100' in row["tags"] and '"unit": "audio_bytes"' in row["tags"]


async def test_transcription_without_a_request_records_nothing(tmp_path: Path) -> None:
    keyless = GroqTranscriptionProvider(api_key=None)
    keyless.api_key = None  # no ambient credential either
    keyless.usage_data_dir = tmp_path
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"x")
    assert await keyless.transcribe(audio) == ""
    keyed = GroqTranscriptionProvider(api_key=KEY)
    keyed.usage_data_dir = tmp_path
    assert await keyed.transcribe(tmp_path / "missing.ogg") == ""
    assert _rows(tmp_path) == []


def test_sc003_embedding_call_adds_one_row(tmp_path: Path) -> None:
    from nanobot.core.vec_store import VecStore

    store = VecStore(tmp_path / "vec.db", embedder=_FakeEmbedder(), usage_data_dir=tmp_path)
    try:
        vectors = store._embed(["one", "two", "three"])  # noqa: SLF001 - the metered seam
    finally:
        store.close()
    assert len(vectors) == 3
    [row] = _rows(tmp_path)
    assert (row["purpose"], row["provider"], row["model"]) == ("embedding", "local", "fake-mini")
    assert row["tier"] == "local" and row["cost_usd"] == 0.0
    assert row["key_fp"] is None and '"units": 3' in row["tags"]
    [record] = records(get_llm_usage_store(data_dir=tmp_path), UsageFilter())
    assert record["price_source"] == "local_zero"


def test_recording_failure_never_fails_the_call(tmp_path: Path) -> None:
    from nanobot.core.vec_store import VecStore

    def broken(_call: Any, _event: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    set_side_call_recorder(broken)
    try:
        store = VecStore(tmp_path / "vec.db", embedder=_FakeEmbedder(), usage_data_dir=tmp_path)
        try:
            assert len(store._embed(["x"])) == 1  # noqa: SLF001
        finally:
            store.close()
        assert record_side_call(
            "image", provider="p", model="m", started_at_ms=0, duration_ms=0,
        ) is False
    finally:
        set_side_call_recorder(None)
    assert _rows(tmp_path) == []


async def test_image_recording_failure_returns_the_images(tmp_path: Path) -> None:
    set_side_call_recorder(lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        response = await _FakeImages(api_key=KEY).generate(prompt="x", model="m")
    finally:
        set_side_call_recorder(None)
    assert response.images


def test_unknown_kind_is_refused_fail_open(tmp_path: Path) -> None:
    assert record_side_call(
        "video", provider="p", model="m", started_at_ms=0, duration_ms=0,  # type: ignore[arg-type]
        data_dir=tmp_path,
    ) is False
