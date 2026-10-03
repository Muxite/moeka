"""Part D: metered side calls (FR-003, SC-003): image generation, transcription and VecStore
embedding calls each add exactly one ``llm_usage`` row; a recording failure never fails the
call. Backends are fake: HTTP is answered in process (``httpx.AsyncClient.send`` patched) and
the embedder is an injected fake."""

from __future__ import annotations

import base64

import httpx
import pytest
from _h010 import FAKE_KEY_AUDIO, FAKE_KEY_IMAGE, fp, kind_of, usage_rows

PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00"
    b"\x00\x00\x00IEND\xaeB`\x82"
).decode()
IMAGE_MODEL = "openai/gpt-5.4-image-2"
AUDIO_MODEL = "whisper-1"
EMBED_MODEL = "all-MiniLM-L6-v2"


def _image_payload(usage: dict | None = None) -> dict:
    payload = {
        "id": "gen-h010",
        "model": IMAGE_MODEL,
        "choices": [{"message": {
            "role": "assistant", "content": "here you go",
            "images": [{"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{PNG}"}}],
        }, "finish_reason": "stop"}],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


@pytest.fixture
def fake_http(monkeypatch):
    """Answer every ``httpx.AsyncClient`` request in process: images for chat completions,
    ``{"text": ...}`` for audio transcriptions. ``seen`` collects the request URLs."""
    state = {"image_payload": _image_payload(), "seen": []}

    async def send(self, request, *args, **kwargs):
        url = str(request.url)
        state["seen"].append(url)
        if "audio/transcriptions" in url:
            return httpx.Response(200, json={"text": "hello from the fake backend"},
                                  request=request)
        if "chat/completions" in url:
            return httpx.Response(200, json=state["image_payload"], request=request)
        return httpx.Response(404, json={"error": "unexpected"}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return state


class FakeEmbedder:
    """A local embedding model stand-in: 384-dim unit vectors, no numpy needed."""

    model_name = EMBED_MODEL
    available = True

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[1.0] + [0.0] * 383 for _ in texts]


@pytest.fixture
def broken_store(monkeypatch):
    """Every way of writing to the usage store raises."""
    from nanobot.llm_usage import store as store_mod

    def boom(*_a, **_k):
        raise RuntimeError("h010: usage store is broken")

    for name in ("record", "record_event", "record_usage_event", "record_many", "_connect"):
        if hasattr(store_mod.LLMUsageStore, name):
            monkeypatch.setattr(store_mod.LLMUsageStore, name, boom)
    return boom


async def _generate_image():
    from nanobot.providers.image_generation import OpenRouterImageGenerationClient

    client = OpenRouterImageGenerationClient(api_key=FAKE_KEY_IMAGE)
    return await client.generate(prompt="a red square", model=IMAGE_MODEL)


async def _transcribe(tmp_path):
    from nanobot.providers.transcription import OpenAITranscriptionProvider

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32)
    provider = OpenAITranscriptionProvider(api_key=FAKE_KEY_AUDIO)
    return await provider.transcribe(audio)


def _embed(tmp_path, texts=("alpha", "beta", "gamma")):
    from nanobot.core.vec_store import VecStore

    embedder = FakeEmbedder()
    store = VecStore(tmp_path / "vec.db", embedder=embedder)
    try:
        public = getattr(store, "embed", None)
        vectors = public(list(texts)) if callable(public) else store._embed(list(texts))
    finally:
        store.close()
    return vectors, embedder


def _rows(tmp_path):
    import os
    from pathlib import Path

    return usage_rows(Path(os.environ["HOME"]), tmp_path)


# -- image ---------------------------------------------------------------------------------------


@pytest.mark.fr("FR-003", "SC-003")
async def test_image_generation_adds_exactly_one_row(tmp_path, fake_http):
    assert _rows(tmp_path) == []
    result = await _generate_image()
    assert result.images and result.images[0].startswith("data:image/png")
    rows = _rows(tmp_path)
    assert len(rows) == 1, rows


@pytest.mark.fr("FR-003")
async def test_image_row_fields(tmp_path, fake_http):
    await _generate_image()
    (row,) = _rows(tmp_path)
    assert "image" in kind_of(row), row
    assert row["provider"].lower() == "openrouter"
    assert IMAGE_MODEL in row["model"]
    assert row["key_fp"] == fp(FAKE_KEY_IMAGE)
    assert FAKE_KEY_IMAGE not in repr(row)


@pytest.mark.fr("FR-003")
async def test_image_row_records_tokens_when_reported(tmp_path, fake_http):
    fake_http["image_payload"] = _image_payload(
        {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
    await _generate_image()
    (row,) = _rows(tmp_path)
    assert row.get("total_tokens") == 18 or (
        row.get("input_tokens") == 11 and row.get("output_tokens") == 7), row


@pytest.mark.fr("FR-003")
async def test_image_recording_failure_does_not_fail_the_call(tmp_path, fake_http, broken_store):
    result = await _generate_image()
    assert result.images and result.images[0].startswith("data:image/png")


# -- transcription -------------------------------------------------------------------------------


@pytest.mark.fr("FR-003", "SC-003")
async def test_transcription_adds_exactly_one_row(tmp_path, fake_http):
    assert _rows(tmp_path) == []
    text = await _transcribe(tmp_path)
    assert text == "hello from the fake backend"
    rows = _rows(tmp_path)
    assert len(rows) == 1, rows


@pytest.mark.fr("FR-003")
async def test_transcription_row_fields(tmp_path, fake_http):
    await _transcribe(tmp_path)
    (row,) = _rows(tmp_path)
    assert "transcription" in kind_of(row), row
    assert row["provider"].lower() == "openai"
    assert AUDIO_MODEL in row["model"]
    assert row["key_fp"] == fp(FAKE_KEY_AUDIO)
    assert FAKE_KEY_AUDIO not in repr(row)


@pytest.mark.fr("FR-003")
async def test_transcription_recording_failure_does_not_fail_the_call(
        tmp_path, fake_http, broken_store):
    assert await _transcribe(tmp_path) == "hello from the fake backend"


# -- embedding -----------------------------------------------------------------------------------


@pytest.mark.fr("FR-003", "SC-003")
def test_embedding_call_adds_exactly_one_row(tmp_path):
    assert _rows(tmp_path) == []
    vectors, embedder = _embed(tmp_path)
    assert len(vectors) == 3 and embedder.calls == [["alpha", "beta", "gamma"]]
    rows = _rows(tmp_path)
    assert len(rows) == 1, rows


@pytest.mark.fr("FR-003")
def test_local_embedding_row_is_local_tier_cost_zero(tmp_path):
    _embed(tmp_path)
    (row,) = _rows(tmp_path)
    assert "embedding" in kind_of(row), row
    assert EMBED_MODEL in row["model"]
    assert row["provider"]
    assert row["tier"] == "local"
    assert row["cost_usd"] is not None and float(row["cost_usd"]) == 0.0
    assert row.get("key_fp") is None


@pytest.mark.fr("FR-003")
def test_embedding_recording_failure_does_not_fail_the_call(tmp_path, broken_store):
    vectors, embedder = _embed(tmp_path)
    assert len(vectors) == 3 and len(embedder.calls) == 1


# -- all three -------------------------------------------------------------------------------------


@pytest.mark.fr("SC-003", "FR-003")
async def test_three_side_calls_add_three_rows_one_per_kind(tmp_path, fake_http):
    await _generate_image()
    await _transcribe(tmp_path)
    _embed(tmp_path)
    rows = _rows(tmp_path)
    assert len(rows) == 3, rows
    labels = [kind_of(r) for r in rows]
    for kind in ("image", "transcription", "embedding"):
        assert sum(1 for lab in labels if kind in lab) == 1, (kind, labels)
