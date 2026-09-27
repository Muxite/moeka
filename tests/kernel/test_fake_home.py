"""Invariant I1/I2 proof: a strict kernel turn under a fake, poisoned HOME.

The process environment is scrubbed and every variable the pre-kernel code read
(``LEGACY_ENV_REFS``, ``LEGACY_ENV_SETTINGS``, the state-home chain) is set to a
poison sentinel. A strict ``CoreEnvironment`` with separated ``work_dir`` /
``state_dir`` and a ``StaticCredentialResolver`` then runs one ``MoekaKernel``
turn with a real tool call (``exec``) through a fake provider. The test asserts:

- nothing is created under ``$HOME`` or the current directory (a poisoned
  relative state-home path would land there), and the real home is untouched;
- files appear only under ``work_dir``, ``state_dir`` or the temp root;
- ``os.environ`` is not mutated and no poisoned variable is looked up;
- the sentinel appears nowhere: not in traces, logs, provider request payloads,
  tool output, ``sessions.db`` or any other file the turn wrote.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from loguru import logger

from nanobot.config.schema import Config
from nanobot.kernel import CoreEnvironment, MoekaKernel, Paths, StaticCredentialResolver
from nanobot.kernel.legacy import LEGACY_ENV_REFS, LEGACY_ENV_SETTINGS
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest

SENTINEL = "AMBIENT-MUST-NOT-BE-READ"

# The instance-dir override chain read by get_state_home() (nanobot/config/paths.py).
STATE_HOME_VARS = ("MOEKA_WORKSPACE", "MOEKA_STATE", "NANOBOT_HOME")
# Home-related variables removed outright (HOME itself is pointed at a temp dir).
SCRUBBED_HOME_VARS = (
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
)


def _poisoned_names() -> set[str]:
    names = {name for names in LEGACY_ENV_REFS.values() for name in names}
    names |= {name for names in LEGACY_ENV_SETTINGS.values() for name in names}
    names |= set(STATE_HOME_VARS)
    return names


class _RecordingEnviron(MutableMapping[str, str]):
    """Stands in for ``os.environ`` during the turn and records key lookups."""

    def __init__(self, inner: MutableMapping[str, str]) -> None:
        self._inner = inner
        self.looked_up: set[str] = set()
        self.writes: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.looked_up.add(key)
        return self._inner[key]

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            self.looked_up.add(key)
        return key in self._inner

    def __setitem__(self, key: str, value: str) -> None:
        self.writes.append(key)
        self._inner[key] = value

    def __delitem__(self, key: str) -> None:
        self.writes.append(key)
        del self._inner[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._inner)

    def __len__(self) -> int:
        return len(self._inner)

    def copy(self) -> dict[str, str]:
        # Whole-environment copies (subprocess helpers, proxy discovery) count as a
        # lookup of every poisoned name they would carry.
        self.looked_up.update(self._inner)
        return dict(self._inner)


class _DictSource:
    """A kernel-native ConfigSource: sections of an in-memory Config, nothing else."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def section(self, name: str) -> dict[str, Any]:
        if name not in Config.model_fields:
            return {}
        value = self._config.model_dump(by_alias=False, include={name}).get(name)
        return value if isinstance(value, dict) else {}


class _RecordingTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _tree(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def _fake_provider(requests: list[dict[str, Any]]) -> Any:
    provider = MagicMock(spec=LLMProvider)
    provider.generation = GenerationSettings()
    provider.supports_progress_deltas = False
    provider.get_default_model.return_value = "fake/model"
    step = {"n": 0}

    async def chat_stream_with_retry(**kwargs: Any) -> LLMResponse:
        requests.append(kwargs)
        step["n"] += 1
        if step["n"] == 1:
            command = "echo probe:$BRAVE_API_KEY:$NANOBOT_HOME:$HOME; env"
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="c1", name="exec", arguments={"command": command})
                ],
            )
        return LLMResponse(content="done", tool_calls=[], usage=None)

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        requests.append(kwargs)
        return LLMResponse(content="ok", tool_calls=[], usage=None)

    provider.chat_stream_with_retry = chat_stream_with_retry
    provider.chat_with_retry = chat_with_retry
    return provider


async def test_strict_kernel_turn_reads_nothing_ambient(tmp_path, monkeypatch) -> None:
    real_home = Path(os.environ.get("HOME", "/nonexistent"))
    real_state = real_home / ".nanobot"
    real_state_before = _tree(real_state)

    home = tmp_path / "home"
    work = tmp_path / "work"
    state = tmp_path / "state"
    cwd = tmp_path / "cwd"
    tmp_root = tmp_path / "tmp"
    for d in (home, work, state, cwd, tmp_root):
        d.mkdir()

    for name in SCRUBBED_HOME_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_root))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_root))
    poisoned = _poisoned_names()
    for name in poisoned:
        monkeypatch.setenv(name, SENTINEL)
    monkeypatch.chdir(cwd)

    config = Config.model_validate({
        "agents": {"defaults": {
            "workspace": str(work),
            "model": "fake/model",
            "vec": {"enable": False},
        }},
    })
    trace = _RecordingTrace()
    env = CoreEnvironment(
        config=_DictSource(config),
        credentials=StaticCredentialResolver({}),
        paths=Paths(work_dir=work, state_dir=state),
        trace=trace,
        exec_base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(work)},
        strict=True,
    )

    logs: list[str] = []
    sink_id = logger.add(lambda msg: logs.append(str(msg)), level="DEBUG")
    environ_before = dict(os.environ)
    recorder = _RecordingEnviron(os.environ)
    requests: list[dict[str, Any]] = []
    try:
        monkeypatch.setattr(os, "environ", recorder)
        kernel = MoekaKernel.create(config=config, env=env, provider=_fake_provider(requests))
        result = await kernel.run("run the probe", session_key="cli:fake-home")
        kernel.cleanup()
    finally:
        monkeypatch.setattr(os, "environ", recorder._inner)
        logger.remove(sink_id)

    # The turn ran end to end with one real tool call.
    assert result.content == "done"
    assert "exec" in result.tools_used
    tool_results = [
        m for m in result.messages if m.get("role") == "tool" and m.get("name") == "exec"
    ]
    assert tool_results, result.messages
    tool_output = json.dumps(tool_results, default=str)
    assert f"probe:::{work}" in tool_output  # child env = env.exec_base_env only

    # Nothing under $HOME or the cwd; real ~/.nanobot untouched.
    assert _tree(home) == set()
    assert _tree(cwd) == set()
    assert _tree(real_state) == real_state_before
    # Files only in work_dir / state_dir / the temp root.
    assert set(os.listdir(tmp_path)) == {"home", "work", "state", "cwd", "tmp"}
    assert any(state.rglob("sessions.db"))

    # os.environ not mutated, no poisoned variable looked up.
    assert dict(os.environ) == environ_before
    assert recorder.writes == []
    assert sorted(recorder.looked_up & poisoned) == []

    # The sentinel is nowhere: traces, logs, provider payloads, tool output, files.
    assert SENTINEL not in json.dumps(trace.events, default=str)
    assert not [line for line in logs if SENTINEL in line]
    assert requests
    assert SENTINEL not in json.dumps(requests, default=str)
    assert SENTINEL not in tool_output
    for root in (work, state, tmp_root):
        for path in root.rglob("*"):
            if path.is_file():
                assert SENTINEL.encode() not in path.read_bytes(), path
