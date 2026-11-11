"""Spec 014: the bridge against the REAL wonhee CLI (throwaway home), not mocks.

Skipped when no wonhee checkout with a venv is available. It is found as a sibling of this repo
(../wonhee), or set MOEKA_TEST_WONHEE to its path.
This is the test that fails when a wonhee change breaks what moeka reads, even if both
repositories' own suites are green.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.config.schema import CreativeBridgeConfig
from nanobot.creative_bridge import REQUIRED_SHAPES, CreativeBridge

WONHEE = Path(os.environ.get("MOEKA_TEST_WONHEE") or Path(__file__).resolve().parents[2] / "wonhee")
PYTHON = WONHEE / ".venv" / "bin" / "python"

pytestmark = pytest.mark.skipif(not PYTHON.exists(), reason="no wonhee checkout with a .venv")


@pytest.fixture
def live(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr("nanobot.creative_bridge.get_media_dir", lambda _: media)
    config = CreativeBridgeConfig(enabled=True, mode="direct", python=str(PYTHON), cwd=str(WONHEE),
                                  home=str(tmp_path / "wonhee-home"))
    monkeypatch.setattr("nanobot.creative_context.Path.home", lambda: tmp_path)  # signing key stays in tmp
    return CreativeBridge(config, workspace=tmp_path, bus=MessageBus(), peers=None, get_channel=lambda _: None)


@pytest.mark.asyncio
async def test_real_wonhee_satisfies_the_contract_moeka_checks_at_startup(live):
    assert await live.verify_contract() is True, live.contract_state["problems"]
    assert live.contract_state["version"] == 1


@pytest.mark.asyncio
async def test_every_response_key_moeka_reads_exists_in_the_real_responses(live):
    project = await live.call("project_create", name="contract-live")
    responses = {
        "project_create": project,
        "project_show": await live.call("project_show", project_id=project["id"]),
        "inbox_list": await live.call("inbox_list", project_id=project["id"], limit=5),
        "outbox_list": await live.call("outbox_list", project_id=project["id"]),
        "delivery_show": await live.call("delivery_show"),
        "health": await live.call("health"),
    }
    for action, response in responses.items():
        missing = set(REQUIRED_SHAPES[action]) - set(response)
        assert not missing, f"wonhee '{action}' no longer returns {sorted(missing)}"


@pytest.mark.asyncio
async def test_status_report_works_against_the_real_health_action(live):
    text = await live.status_report({"chat_id": "1"})
    assert "Wonhee link:" in text and "Could not read wonhee health" not in text


@pytest.mark.asyncio
async def test_real_wonhee_advertises_a_marker_and_moves_it_on_a_queued_delivery(live, tmp_path):
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg needed to make a real clip")
    assert await live.verify_contract() is True
    assert live.contract_state["signal"] == ".bridge-signal"
    assert live._wonhee_changed() is True      # first look
    assert live._wonhee_changed() is False     # idle: no spawn, no change
    video = tmp_path / "out.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=duration=1:size=160x120:rate=5", "-pix_fmt", "yuv420p", str(video)], check=True)
    project = await live.call("project_create", name="signal-live")
    stored = await live.call("inbox_add", source=str(video), project_id=project["id"], source_key="t:1")
    await live.call("outbox_add", ref=stored["ref"], filename="out.mp4", project_id=project["id"],
                    route={"channel": "telegram", "chat_id": "1"})
    assert live._wonhee_changed() is True      # the queued delivery signalled the bridge
    assert len((await live.call("delivery_show"))["deliveries"]) == 1


@pytest.mark.asyncio
async def test_real_wonhee_rerender_with_identical_bytes_is_flagged_by_the_gate(live, tmp_path):
    """The Oct 26 failure end to end: two deliveries of the same bytes, through wonhee's own CLI."""
    import shutil
    import subprocess

    from nanobot.creative_checks import compose_report

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg needed to make a real clip")
    video = tmp_path / "reel.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=duration=1:size=160x120:rate=5", "-pix_fmt", "yuv420p", str(video)], check=True)
    project = await live.call("project_create", name="gate-live")
    stored = await live.call("inbox_add", source=str(video), project_id=project["id"], source_key="g:1")
    route = {"channel": "telegram", "chat_id": "1"}
    first = await live.call("outbox_add", ref=stored["ref"], filename="reel-4.mp4",
                            project_id=project["id"], route=route, content="Reel ready.")
    second = await live.call("outbox_add", ref=stored["ref"], filename="reel-5.mp4",
                             project_id=project["id"], route=route,
                             content="Reel ready. Full file check: not yet run.")
    delivered = Path(second["path"])
    assert delivered.is_file()

    check = await live._verify_output({"id": second["id"], "project_id": project["id"]}, delivered)
    assert check["duplicate_of"]["id"] == first["id"]  # found through the real outbox_list
    report = compose_report(second["content"], check)
    assert f"WARNING: byte-identical to delivery {first['id']} (reel-4.mp4)" in report
    assert "160x120" in report  # real ffprobe facts, not wonhee's 'not yet run'

    other = await live._verify_output({"id": first["id"], "project_id": project["id"]}, Path(first["path"]))
    assert other["duplicate_of"] is None  # the first delivery is the original, never a duplicate
