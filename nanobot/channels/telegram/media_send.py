"""Prepare a local file for a Telegram bot upload: size limits, fit-to-limit, video metadata.

Telegram's hosted Bot API accepts uploads up to 50 MB (photos 10 MB as photos). Instead of
failing with a bare "[Failed to send]", this module:

- sends oversized photos as documents,
- re-encodes oversized videos with a bounded bitrate ladder (3 tries) to fit under the limit,
- raises ``MediaTooLargeError`` with a human-readable reason when a file cannot be made to fit,
- reports width/height/duration so vertical videos render at the right aspect ratio.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from nanobot.utils import media_probe

UPLOAD_LIMIT = 50_000_000
PHOTO_LIMIT = 10_000_000
CAPTION_MAX = 1024
_AUDIO_BPS = 128_000
_MIN_VIDEO_BPS = 150_000
_SAFETY = 0.9
_LADDER = (1.0, 0.75, 0.55)
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv"})
_PHOTO_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


class MediaTooLargeError(ValueError):
    """The file cannot be made to fit Telegram's upload limit; ``str(exc)`` says why."""


@dataclass
class PreparedMedia:
    path: Path
    as_document: bool = False
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    note: str | None = None  # e.g. "re-encoded from 83.4 MB to 47.1 MB"
    _scratch: Path | None = field(default=None, repr=False)

    def cleanup(self) -> None:
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None


def _mb(n: int | float) -> str:
    return f"{n / 1_000_000:.1f} MB"


async def fit_video(path: Path, limit: int = UPLOAD_LIMIT) -> tuple[Path, Path] | None:
    """Re-encode *path* to at most *limit* bytes. Returns (output, scratch_dir) or ``None``."""
    if not media_probe.tools_available():
        return None
    facts = await media_probe.probe(path)
    if facts is None or not facts.duration_s or facts.duration_s <= 0:
        return None
    scratch = Path(tempfile.mkdtemp(prefix="moeka-fit-"))
    base_total_bps = limit * 8 * _SAFETY / facts.duration_s
    for factor in _LADDER:
        video_bps = int(base_total_bps * factor - _AUDIO_BPS)
        if video_bps < _MIN_VIDEO_BPS:
            break
        out = scratch / "fit.mp4"
        argv = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
            "-vf", "scale=w=1280:h=1280:force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(video_bps),
            "-maxrate", str(int(video_bps * 1.2)), "-bufsize", str(video_bps * 2),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", str(_AUDIO_BPS),
            "-movflags", "+faststart", str(out),
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=900)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            break
        if proc.returncode == 0 and out.exists() and out.stat().st_size <= limit:
            return out, scratch
    shutil.rmtree(scratch, ignore_errors=True)
    return None


async def prepare_local_media(
    path: Path, *, as_document: bool = False, limit: int = UPLOAD_LIMIT,
) -> PreparedMedia:
    """Decide how to upload *path*; may re-encode a too-large video (caller must ``cleanup``)."""
    size = path.stat().st_size
    ext = path.suffix.lower()
    is_video = ext in _VIDEO_EXTS and not as_document
    if ext in _PHOTO_EXTS and size > PHOTO_LIMIT and size <= limit:
        return PreparedMedia(path, as_document=True, note="sent as a document (over 10 MB)")
    prepared = PreparedMedia(path, as_document=as_document)
    scratch: Path | None = None
    if size > limit:
        if not is_video:
            raise MediaTooLargeError(
                f"{path.name} is {_mb(size)}, over Telegram's {_mb(limit)} bot upload limit"
                + (" (re-encoding only applies to video)" if ext not in _VIDEO_EXTS else "")
                + ". Share it as a Drive link instead."
            )
        fitted = await fit_video(path, limit)
        if fitted is None:
            raise MediaTooLargeError(
                f"{path.name} is {_mb(size)}, over Telegram's {_mb(limit)} bot upload limit, "
                "and could not be re-encoded under it (too long, or ffmpeg unavailable). "
                "Share it as a Drive link instead."
            )
        out, scratch = fitted
        prepared = PreparedMedia(
            out, as_document=False,
            note=f"re-encoded from {_mb(size)} to {_mb(out.stat().st_size)} to fit Telegram's limit",
        )
        prepared._scratch = scratch
    if is_video:
        facts = await media_probe.probe(prepared.path)
        if facts is not None:
            prepared.width, prepared.height = facts.width, facts.height
            prepared.duration = round(facts.duration_s) if facts.duration_s else None
    return prepared


def clip_caption(text: str | None) -> str | None:
    if not text:
        return None
    text = text.strip()
    return text[: CAPTION_MAX - 1] + "…" if len(text) > CAPTION_MAX else text or None
