"""Inbound video placeholders tell the model it cannot see the clip (no silent blind guessing)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("telegram")

from nanobot.bus.queue import MessageBus  # noqa: E402
from nanobot.channels.telegram import runtime  # noqa: E402
from nanobot.channels.telegram.runtime import TelegramChannel  # noqa: E402


class _File:
    async def download_to_drive(self, path: str) -> None:
        open(path, "wb").write(b"x")


class _Bot:
    async def get_file(self, file_id: str) -> _File:
        return _File()


def _channel(tmp_path, monkeypatch) -> TelegramChannel:
    monkeypatch.setattr(runtime, "get_media_dir", lambda name: tmp_path)
    ch = TelegramChannel({"enabled": True, "token": "1:t"}, MessageBus())
    ch._app = SimpleNamespace(bot=_Bot())  # type: ignore[assignment]
    return ch


def _msg(**attrs: Any) -> SimpleNamespace:
    base = dict(photo=None, voice=None, audio=None, document=None, video=None,
                video_note=None, animation=None)
    base.update(attrs)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_video_placeholder_says_it_cannot_be_seen(tmp_path, monkeypatch) -> None:
    ch = _channel(tmp_path, monkeypatch)
    video = SimpleNamespace(file_id="f", file_unique_id="u1", file_size=1000,
                            mime_type="video/mp4", file_name="clip.mp4")
    paths, parts = await ch._download_message_media(_msg(video=video))
    assert len(paths) == 1 and paths[0].endswith(".mp4")
    assert parts[0].startswith(f"[video: {paths[0]}]")
    assert "cannot see this video" in parts[0] and "view_media" in parts[0]


@pytest.mark.asyncio
async def test_document_placeholder_is_unchanged(tmp_path, monkeypatch) -> None:
    ch = _channel(tmp_path, monkeypatch)
    doc = SimpleNamespace(file_id="f", file_unique_id="u2", file_size=10,
                          mime_type="application/pdf", file_name="a.pdf")
    paths, parts = await ch._download_message_media(_msg(document=doc))
    assert parts == [f"[file: {paths[0]}]"]
