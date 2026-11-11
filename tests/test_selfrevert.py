"""Stable marks, owner revert and the boot guard, against real throwaway git repos (spec 015)."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "selfrevert", Path(__file__).resolve().parents[1] / "scripts" / "selfrevert.py",
)
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)


def git(repo, *args):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(repo)}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def commit(repo, text):
    (repo / "code.py").write_text(text)
    git(repo, "add", "code.py")
    git(repo, "commit", "-q", "-m", text)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "v1")
    return r


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state"


def test_mark_and_list(repo):
    tag = sr.mark_stable(repo, "first good")
    assert tag.startswith(sr.STABLE_PREFIX)
    assert [t["tag"] for t in sr.stable_tags(repo)] == [tag]
    assert sr.mark_stable(repo) == tag  # same commit: no duplicate


def test_mark_refuses_a_dirty_tree(repo):
    (repo / "code.py").write_text("edited")
    with pytest.raises(sr.RevertError, match="dirty"):
        sr.mark_stable(repo)


def test_revert_checks_out_the_stable_commit_and_back_returns(repo, state):
    v1 = git(repo, "rev-parse", "HEAD")
    sr.mark_stable(repo)
    commit(repo, "v2-broken")
    result = sr.revert(repo, state, "latest")
    assert git(repo, "rev-parse", "HEAD") == v1 and (repo / "code.py").read_text() == "v1"
    assert result["stash"] == ""
    assert sr.back(repo, state)["ref"] == "main"
    assert (repo / "code.py").read_text() == "v2-broken"
    assert sr.read_state(state) is None


def test_revert_without_a_stable_mark_says_so(repo, state):
    with pytest.raises(sr.RevertError, match="No stable version"):
        sr.revert(repo, state)


def test_revert_refuses_dirty_unless_forced_and_then_stashes(repo, state):
    sr.mark_stable(repo)
    commit(repo, "v2")
    (repo / "code.py").write_text("uncommitted")
    with pytest.raises(sr.RevertError, match="uncommitted"):
        sr.revert(repo, state)
    result = sr.revert(repo, state, force=True)
    assert result["stash"].startswith("moeka-revert-")
    assert "moeka-revert-" in git(repo, "stash", "list")
    assert (repo / "code.py").read_text() == "v1"


def test_only_stable_marks_are_valid_targets(repo, state):
    sr.mark_stable(repo)
    other = commit(repo, "v2")
    with pytest.raises(sr.RevertError, match="not a stable mark"):
        sr.revert(repo, state, other)


def test_previous_skips_the_version_that_is_running(repo, state):
    sr.mark_stable(repo)
    commit(repo, "v2")
    second = sr.mark_stable(repo)
    assert sr.resolve_target(repo, "previous")["tag"] != second
    sr.revert(repo, state, "previous")
    assert (repo / "code.py").read_text() == "v1"


def test_chained_reverts_still_reach_the_original_branch(repo, state):
    sr.mark_stable(repo)
    commit(repo, "v2")
    sr.mark_stable(repo)
    commit(repo, "v3")
    sr.revert(repo, state, "latest")
    sr.revert(repo, state, "previous")
    assert sr.back(repo, state)["ref"] == "main"
    assert (repo / "code.py").read_text() == "v3"


# -- boot guard ---------------------------------------------------------------------------------


def test_guard_reverts_after_three_unhealthy_boots_on_one_commit(repo, state):
    sr.mark_stable(repo)
    commit(repo, "v2-crashes")
    for i in range(3):
        assert sr.guard(repo, state, now=1000 + i) == ""
    report = sr.guard(repo, state, now=1004)
    assert "reverted to" in report
    assert (repo / "code.py").read_text() == "v1"


def test_a_healthy_boot_resets_the_count(repo, state):
    sr.mark_stable(repo)
    commit(repo, "v2")
    for i in range(2):
        sr.guard(repo, state, now=1000 + i)
    sr.healthy(state)
    for i in range(2):
        assert sr.guard(repo, state, now=2000 + i) == ""
    assert (repo / "code.py").read_text() == "v2"


def test_slow_failures_outside_the_window_do_not_revert(repo, state):
    sr.mark_stable(repo)
    commit(repo, "v2")
    for i in range(6):
        assert sr.guard(repo, state, now=1000 + i * 3600) == ""
    assert (repo / "code.py").read_text() == "v2"


def test_guard_without_a_stable_mark_reports_and_stays_put(repo, state):
    for i in range(3):
        sr.guard(repo, state, now=1000 + i)
    assert "no earlier stable mark" in sr.guard(repo, state, now=1004)
    assert (repo / "code.py").read_text() == "v1"


def test_guard_is_a_noop_outside_a_git_checkout(tmp_path, state):
    plain = tmp_path / "pip-install"
    plain.mkdir()
    assert sr.guard(plain, state) == ""


def test_cli_guard_never_fails_the_start(repo, state):
    assert sr.main(["guard", "--repo", str(repo), "--state", str(state)]) == 0
    assert sr.main(["revert", "--repo", str(repo), "--state", str(state)]) == 1  # no stable mark
