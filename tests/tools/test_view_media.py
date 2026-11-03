"""view_media: ffmpeg frame sampling + a separate vision preset, with cited answers."""

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.view_media import ViewMediaTool
from nanobot.config.schema import ToolsConfig, ViewMediaToolConfig
from nanobot.security.untrusted import UNTRUSTED_BANNER
from nanobot.utils import media_probe

needs_ffmpeg = pytest.mark.skipif(not media_probe.tools_available(), reason="ffmpeg not installed")


def _make_clip(path, seconds=3):
    subprocess.run(
        [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=10",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-shortest", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


def _make_png(path):
    subprocess.run(
        [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=1",
         "-frames:v", "1", str(path)],
        check=True,
    )


class FakeProvider:
    def __init__(self, content="Caption visible at 0.00s. Not verifiable from these frames: motion.",
                 finish="stop"):
        self.calls = []
        self._content = content
        self._finish = finish

    async def chat_with_retry(self, messages, model=None, max_tokens=None, temperature=None, **kw):
        self.calls.append({"messages": messages, "model": model})
        return SimpleNamespace(content=self._content, finish_reason=self._finish)


def _tool(tmp_path, provider=None, loader=None, **cfg):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    provider = provider or FakeProvider()
    loader = loader or (lambda preset_name=None: SimpleNamespace(provider=provider, model="flash-lite"))
    ctx = ToolContext(
        config=ToolsConfig(restrict_to_workspace=True, view_media=ViewMediaToolConfig(**cfg)),
        workspace=str(ws), host_tools=True, provider_snapshot_loader=loader,
    )
    return ViewMediaTool.create(ctx), provider, ws


def test_pick_times_includes_start_end_and_respects_cap():
    times = media_probe.pick_times(10.0, [2.0, 2.1, 5.0, 7.5], 5)
    assert times[0] == 0.0 and times[-1] == pytest.approx(9.9)
    assert len(times) <= 5 and times == sorted(times)
    assert all(b - a >= 0.4 for a, b in zip(times, times[1:]))


def test_pick_times_zero_duration_is_single_frame():
    assert media_probe.pick_times(0.0, [], 8) == [0.0]


def test_enabled_requires_host_tools_and_snapshot_loader(tmp_path):
    loader = lambda preset_name=None: None  # noqa: E731
    base = dict(config=ToolsConfig(), workspace=str(tmp_path))
    assert ViewMediaTool.enabled(ToolContext(**base, host_tools=True, provider_snapshot_loader=loader))
    assert not ViewMediaTool.enabled(ToolContext(**base, host_tools=False, provider_snapshot_loader=loader))
    assert not ViewMediaTool.enabled(ToolContext(**base, host_tools=True))


@needs_ffmpeg
@pytest.mark.asyncio
async def test_probe_and_loudness_on_real_clip(tmp_path):
    clip = tmp_path / "c.mp4"
    _make_clip(clip)
    facts = await media_probe.probe(clip)
    assert facts and facts.width == 320 and facts.height == 240 and facts.has_audio
    assert facts.duration_s == pytest.approx(3.0, abs=0.3)
    loud = await media_probe.loudness(clip)
    assert loud and "integrated_lufs" in loud


@needs_ffmpeg
@pytest.mark.asyncio
async def test_video_result_has_facts_timestamps_and_untrusted_banner(tmp_path):
    tool, provider, ws = _tool(tmp_path, max_frames=4)
    _make_clip(ws / "clip.mp4")
    result = await tool.execute(path=str(ws / "clip.mp4"), question="Is the caption visible?")
    assert not result.is_error, result
    assert result.startswith(UNTRUSTED_BANNER)
    assert "resolution: 320x240" in result and "frames at 0.00s" in result
    assert "Answer: Caption visible at 0.00s" in result
    sent = provider.calls[0]["messages"][1]["content"]
    images = [b for b in sent if b.get("type") == "image_url"]
    labels = [b["text"] for b in sent if b.get("type") == "text" and b["text"].startswith("frame at")]
    assert 2 <= len(images) <= 4 and len(labels) == len(images)
    assert provider.calls[0]["model"] == "flash-lite"


@needs_ffmpeg
@pytest.mark.asyncio
async def test_image_goes_straight_to_viewer(tmp_path):
    tool, provider, ws = _tool(tmp_path)
    _make_png(ws / "a.png")
    result = await tool.execute(path=str(ws / "a.png"), question="What is shown?")
    assert not result.is_error, result
    assert "viewed: image (single frame)" in result
    sent = provider.calls[0]["messages"][1]["content"]
    assert sum(1 for b in sent if b.get("type") == "image_url") == 1


@pytest.mark.asyncio
async def test_unusable_viewer_preset_is_a_clear_error(tmp_path):
    def loader(preset_name=None):
        raise KeyError(preset_name)

    tool, _, ws = _tool(tmp_path, loader=loader)
    (ws / "a.png").write_bytes(b"x")
    result = await tool.execute(path=str(ws / "a.png"), question="?")
    assert result.is_error and "viewer preset 'viewer'" in result and "modelPresets" in result


@pytest.mark.asyncio
async def test_path_outside_workspace_is_denied(tmp_path):
    tool, provider, _ = _tool(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    result = await tool.execute(path=str(outside), question="?")
    assert result.is_error and "outside" in result.lower()
    assert provider.calls == []


@pytest.mark.asyncio
async def test_unsupported_type_and_oversize_rejected(tmp_path):
    tool, provider, ws = _tool(tmp_path, max_file_mb=1)
    (ws / "n.txt").write_text("hi")
    assert (await tool.execute(path=str(ws / "n.txt"), question="?")).is_error
    (ws / "big.mp4").write_bytes(b"0" * 1_100_000)
    result = await tool.execute(path=str(ws / "big.mp4"), question="?")
    assert result.is_error and "limit" in result
    assert provider.calls == []


@needs_ffmpeg
@pytest.mark.asyncio
async def test_empty_viewer_answer_is_an_error_not_a_pass(tmp_path):
    tool, _, ws = _tool(tmp_path, provider=FakeProvider(content="  "))
    _make_clip(ws / "clip.mp4", seconds=1)
    result = await tool.execute(path=str(ws / "clip.mp4"), question="?")
    assert result.is_error and "no answer" in result
