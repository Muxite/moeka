"""Telegram uploads: fit-to-limit, documents, captions, and clear failure reasons."""

from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("telegram")

from nanobot.bus.events import OutboundMessage  # noqa: E402
from nanobot.bus.queue import MessageBus  # noqa: E402
from nanobot.channels.telegram import media_send  # noqa: E402
from nanobot.channels.telegram.media_send import (  # noqa: E402
    MediaTooLargeError,
    clip_caption,
    prepare_local_media,
)
from nanobot.channels.telegram.runtime import TelegramChannel  # noqa: E402
from nanobot.utils import media_probe  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not media_probe.tools_available(), reason="ffmpeg not installed")


def _big_clip(path, seconds=3):
    subprocess.run(
        [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=640x480:rate=15",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-b:v", "3M", "-pix_fmt", "yuv420p", "-shortest", str(path)],
        check=True,
    )


@pytest.mark.asyncio
async def test_small_file_is_unchanged(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x" * 1000)
    p = await prepare_local_media(f)
    assert p.path == f and not p.as_document and p.note is None
    p.cleanup()


@pytest.mark.asyncio
async def test_oversize_photo_goes_as_document(tmp_path):
    f = tmp_path / "big.png"
    f.write_bytes(b"0" * 10_500_000)
    p = await prepare_local_media(f)
    assert p.as_document and "document" in (p.note or "")


@pytest.mark.asyncio
async def test_oversize_non_video_raises_with_reason(tmp_path):
    f = tmp_path / "data.zip"
    f.write_bytes(b"0" * 2_000)
    with pytest.raises(MediaTooLargeError) as err:
        await prepare_local_media(f, limit=1_000)
    assert "data.zip" in str(err.value) and "Drive link" in str(err.value)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_oversize_video_is_reencoded_under_limit(tmp_path):
    clip = tmp_path / "reel.mp4"
    _big_clip(clip)
    limit = 600_000
    assert clip.stat().st_size > limit
    p = await prepare_local_media(clip, limit=limit)
    try:
        assert p.path != clip and p.path.stat().st_size <= limit
        assert "re-encoded" in (p.note or "")
        assert p.width and p.height and p.duration
    finally:
        scratch = p.path.parent
        p.cleanup()
    assert not scratch.exists()


@needs_ffmpeg
@pytest.mark.asyncio
async def test_video_too_long_for_limit_raises(tmp_path):
    clip = tmp_path / "reel.mp4"
    _big_clip(clip)
    with pytest.raises(MediaTooLargeError) as err:
        await prepare_local_media(clip, limit=100_000)
    assert "could not be re-encoded" in str(err.value)


def test_clip_caption():
    assert clip_caption(None) is None and clip_caption("  ") is None
    assert clip_caption("hi ") == "hi"
    assert len(clip_caption("x" * 2000)) == media_send.CAPTION_MAX


class _Bot:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name):
        if not name.startswith("send_"):
            raise AttributeError(name)

        async def call(**kw):
            self.calls.append((name, kw))
            return SimpleNamespace(message_id=len(self.calls))

        return call


def _channel() -> tuple[TelegramChannel, _Bot]:
    ch = TelegramChannel({"enabled": True, "token": "1:t"}, MessageBus())
    bot = _Bot()
    ch._app = SimpleNamespace(bot=bot)  # type: ignore[assignment]
    ch._app_ready.set()
    return ch, bot


@needs_ffmpeg
@pytest.mark.asyncio
async def test_send_video_has_caption_once_and_dimensions(tmp_path):
    clip = tmp_path / "a.mp4"
    _big_clip(clip, seconds=1)
    ch, bot = _channel()
    msg = OutboundMessage(channel="telegram", chat_id="42", content="", media=[str(clip), str(clip)],
                          metadata={"media_caption": "the reel"})
    await ch.send(msg)
    sends = [c for c in bot.calls if c[0] == "send_video"]
    assert len(sends) == 2
    assert sends[0][1]["caption"] == "the reel" and "caption" not in sends[1][1]
    assert sends[0][1]["width"] == 640 and sends[0][1]["height"] == 480


@pytest.mark.asyncio
async def test_send_as_document_forces_document(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"not really a video")
    ch, bot = _channel()
    msg = OutboundMessage(channel="telegram", chat_id="42", content="", media=[str(f)],
                          metadata={"as_document": True})
    await ch.send(msg)
    assert [c[0] for c in bot.calls] == ["send_document"]


@pytest.mark.asyncio
async def test_send_oversize_reports_the_reason_not_a_bare_failure(tmp_path, monkeypatch):
    f = tmp_path / "huge.zip"
    f.write_bytes(b"0" * 100)
    monkeypatch.setattr(media_send, "UPLOAD_LIMIT", 10)

    async def tiny_limit(path, *, as_document=False, limit=10):
        return await real(path, as_document=as_document, limit=10)

    real = media_send.prepare_local_media
    from nanobot.channels.telegram import runtime
    monkeypatch.setattr(runtime, "prepare_local_media", tiny_limit)
    ch, bot = _channel()
    await ch.send(OutboundMessage(channel="telegram", chat_id="42", content="", media=[str(f)]))
    texts = [kw["text"] for name, kw in bot.calls if name == "send_message"]
    assert len(texts) == 1 and "huge.zip" in texts[0] and "Drive link" in texts[0]
    assert "Failed to send" not in texts[0]


@needs_ffmpeg
@pytest.mark.asyncio
async def test_send_registered_adds_caption_and_video_dimensions(tmp_path):
    clip = tmp_path / "a.mp4"
    _big_clip(clip, seconds=1)
    ch, bot = _channel()
    msg = OutboundMessage(channel="telegram", chat_id="42", content="done", media=[str(clip)],
                          metadata={"media_caption": "final cut"})
    ids = await ch.send_registered(msg)
    assert len(ids) == 2  # video + text
    name, kw = bot.calls[0]
    assert name == "send_video" and kw["caption"] == "final cut" and kw["width"] == 640


@pytest.mark.asyncio
async def test_send_registered_oversize_raises_clear_error(tmp_path, monkeypatch):
    f = tmp_path / "huge.zip"
    f.write_bytes(b"0" * 100)
    from nanobot.channels.telegram import runtime
    real = media_send.prepare_local_media

    async def tiny(path, *, as_document=False, limit=10):
        return await real(path, as_document=as_document, limit=10)

    monkeypatch.setattr(runtime, "prepare_local_media", tiny)
    ch, bot = _channel()
    with pytest.raises(MediaTooLargeError):
        await ch.send_registered(
            OutboundMessage(channel="telegram", chat_id="42", content="", media=[str(f)])
        )
    assert bot.calls == []


@pytest.mark.asyncio
async def test_send_registered_caption_replaces_the_second_text_message(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"not really a video")
    ch, bot = _channel()
    msg = OutboundMessage(channel="telegram", chat_id="42", content="Reel ready", media=[str(f)],
                          metadata={"media_caption": "Reel ready", "content_as_caption": True})
    ids = await ch.send_registered(msg)
    assert len(ids) == 1 and [c[0] for c in bot.calls] == ["send_video"]
    assert bot.calls[0][1]["caption"] == "Reel ready"
