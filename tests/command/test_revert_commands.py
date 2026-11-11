"""/stable and /revert: owner commands that move the checkout and schedule a restart (spec 015)."""

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.cli import selfrevert_link
from nanobot.command import builtin
from nanobot.command.router import CommandContext

_spec = importlib.util.spec_from_file_location(
    "sr_cmd", Path(__file__).resolve().parents[2] / "scripts" / "selfrevert.py",
)
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("NAME", "EMAIL"):
        monkeypatch.setenv(f"GIT_AUTHOR_{k}", "t")
        monkeypatch.setenv(f"GIT_COMMITTER_{k}", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("v1")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "v1")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(selfrevert_link, "module", lambda: sr)
    monkeypatch.setattr(selfrevert_link, "repo_root", lambda: repo)
    restarts = []
    monkeypatch.setattr(builtin, "_begin_restart", lambda ctx, note="": restarts.append(note))
    return SimpleNamespace(repo=repo, state=state, restarts=restarts)


def ctx_for(env, args=""):
    msg = InboundMessage(channel="telegram", sender_id="u", chat_id="1", content="/x")
    return CommandContext(msg=msg, session=None, key="telegram:1", raw="/x", args=args,
                          loop=SimpleNamespace(workspace=env.state))


@pytest.mark.asyncio
async def test_stable_then_revert_restores_code_and_schedules_restart(env):
    out = await builtin.cmd_stable(ctx_for(env, "good build"))
    assert "stable as" in out.content
    (env.repo / "f").write_text("v2")
    _git(env.repo, "commit", "-qam", "v2")
    out = await builtin.cmd_revert(ctx_for(env, "latest"))
    assert "Reverted to" in out.content and (env.repo / "f").read_text() == "v1"
    assert len(env.restarts) == 1 and "Reverted to" in env.restarts[0]


@pytest.mark.asyncio
async def test_revert_without_a_mark_explains_and_does_not_restart(env):
    out = await builtin.cmd_revert(ctx_for(env, "latest"))
    assert "No stable version" in out.content and env.restarts == []


@pytest.mark.asyncio
async def test_bare_revert_lists_marks(env):
    await builtin.cmd_stable(ctx_for(env))
    out = await builtin.cmd_revert(ctx_for(env, ""))
    assert "Running" in out.content and "/revert latest" in out.content


@pytest.mark.asyncio
async def test_dirty_tree_needs_force(env):
    await builtin.cmd_stable(ctx_for(env))
    (env.repo / "f").write_text("v2")
    _git(env.repo, "commit", "-qam", "v2")
    (env.repo / "f").write_text("wip")
    out = await builtin.cmd_revert(ctx_for(env, "latest"))
    assert "force" in out.content and env.restarts == []
    out = await builtin.cmd_revert(ctx_for(env, "latest force"))
    assert "stashed" in out.content and len(env.restarts) == 1


@pytest.mark.asyncio
async def test_back_returns_to_the_branch(env):
    await builtin.cmd_stable(ctx_for(env))
    (env.repo / "f").write_text("v2")
    _git(env.repo, "commit", "-qam", "v2")
    await builtin.cmd_revert(ctx_for(env, "latest"))
    out = await builtin.cmd_revert(ctx_for(env, "back"))
    assert "Back on main" in out.content and (env.repo / "f").read_text() == "v2"


@pytest.mark.asyncio
async def test_no_checkout_is_reported(env, monkeypatch):
    monkeypatch.setattr(selfrevert_link, "repo_root", lambda: None)
    out = await builtin.cmd_revert(ctx_for(env, "latest"))
    assert "git checkout" in out.content


def test_restart_note_reaches_the_completion_message(monkeypatch):
    from nanobot.utils.restart import (
        consume_restart_notice_from_env,
        format_restart_completed_message,
        set_restart_notice_to_env,
    )

    set_restart_notice_to_env(channel="telegram", chat_id="1", note="Reverted to x (abc).")
    notice = consume_restart_notice_from_env()
    assert notice.note == "Reverted to x (abc)."
    assert format_restart_completed_message("", notice.note).endswith("Reverted to x (abc).")
    assert format_restart_completed_message("") == "Restart completed."
