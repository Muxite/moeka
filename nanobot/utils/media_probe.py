"""ffprobe/ffmpeg helpers for looking at video: facts, timed frames, loudness.

Pure subprocess work with argv lists (never a shell). Everything degrades to
``None``/empty when ffmpeg is missing or a file is unreadable, so callers can
report "not checked" instead of failing.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".gif", ".mpg", ".mpeg"})
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})

_SCENE_THRESHOLD = 0.3
_MIN_FRAME_GAP_S = 0.4


@dataclass(frozen=True)
class MediaFacts:
    duration_s: float | None
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool
    size_bytes: int
    video_codec: str | None = None


@dataclass(frozen=True)
class Frame:
    t: float
    path: Path


def tools_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def media_kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return None


async def _run(*argv: str, timeout: float = 120.0) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, b"", b"timeout"
    return proc.returncode if proc.returncode is not None else 1, out, err


def _fps(raw: str | None) -> float | None:
    if not raw or "/" not in raw:
        return None
    num, _, den = raw.partition("/")
    try:
        n, d = float(num), float(den)
    except ValueError:
        return None
    return round(n / d, 2) if d else None


async def probe(path: Path) -> MediaFacts | None:
    """Facts about a media file, or ``None`` when ffprobe cannot read it."""
    if shutil.which("ffprobe") is None:
        return None
    rc, out, _ = await _run(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
        str(path), timeout=30.0,
    )
    if rc != 0:
        return None
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        return None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration")) if fmt.get("duration") else None
    except (TypeError, ValueError):
        duration = None
    return MediaFacts(
        duration_s=duration,
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
        fps=_fps(video.get("avg_frame_rate")) if video else None,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        size_bytes=path.stat().st_size,
        video_codec=video.get("codec_name") if video else None,
    )


async def scene_times(path: Path, *, limit: int = 16) -> list[float]:
    """Timestamps where the picture changes a lot (caption/scene changes)."""
    rc, _, err = await _run(
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-vf", f"select=gt(scene\\,{_SCENE_THRESHOLD}),showinfo", "-an", "-f", "null", "-",
        timeout=180.0,
    )
    if rc != 0:
        return []
    times = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", err.decode("utf-8", "replace"))]
    return times[:limit]


def pick_times(duration: float, scene: list[float], n: int) -> list[float]:
    """Choose up to *n* distinct timestamps: start, end, scene changes, then even fill."""
    if duration <= 0 or n <= 1:
        return [0.0]
    end = max(duration - 0.1, 0.0)
    chosen: list[float] = [0.0]
    if end - 0.0 >= _MIN_FRAME_GAP_S:
        chosen.append(end)

    def fits(t: float) -> bool:
        return all(abs(t - c) >= _MIN_FRAME_GAP_S for c in chosen)

    for t in scene:
        if len(chosen) >= n:
            break
        if 0.0 < t < end and fits(t):
            chosen.append(t)
    step = max(end / n, _MIN_FRAME_GAP_S)
    t = step
    while len(chosen) < n and t < end:
        if fits(t):
            chosen.append(t)
        t += step
    return sorted(round(t, 2) for t in chosen)


async def extract_frames(
    path: Path, times: list[float], out_dir: Path, *, long_edge: int
) -> list[Frame]:
    """One downscaled JPEG per timestamp; timestamps that fail are skipped."""
    frames: list[Frame] = []
    scale = f"scale=w={long_edge}:h={long_edge}:force_original_aspect_ratio=decrease"
    for i, t in enumerate(times):
        target = out_dir / f"frame_{i:02d}.jpg"
        rc, _, _ = await _run(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1", "-vf", scale,
            "-q:v", "4", str(target),
            timeout=60.0,
        )
        if rc == 0 and target.exists() and target.stat().st_size > 0:
            frames.append(Frame(t=t, path=target))
    return frames


async def loudness(path: Path) -> dict[str, float] | None:
    """Integrated loudness (LUFS) and true peak (dBTP) via ebur128, or ``None`` without audio."""
    rc, _, err = await _run(
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
        "-af", "ebur128=peak=true", "-f", "null", "-",
        timeout=120.0,
    )
    if rc != 0:
        return None
    text = err.decode("utf-8", "replace")
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else ""
    integrated = re.search(r"I:\s+(-?[0-9.]+)\s+LUFS", summary)
    peak = re.search(r"Peak:\s+(-?[0-9.]+)\s+dBFS", summary)
    if not integrated:
        return None
    result = {"integrated_lufs": float(integrated.group(1))}
    if peak:
        result["true_peak_db"] = float(peak.group(1))
    return result


def new_scratch_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="moeka-view-"))
