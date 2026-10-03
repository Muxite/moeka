"""Part C: script tool execution (FR-010), the ``script_tool.call`` trace event (FR-011) and the
network sandbox (SC-003)."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest
from _h012 import (
    NET_IO,
    SH_OK,
    agent_of,
    bwrap_state,
    ev,
    fake_with,
    loader_registry,
    py_script,
    strip,
    tool_call,
    tool_messages,
    tool_names,
    write_script_tool,
)

# -- running a script tool --------------------------------------------------------------------------


@pytest.fixture
def run(tmp_path, kmaker):
    """``await run(name, args, caps=None)`` -> the registry's result for ``script_<name>``.

    The registry comes from ``ToolLoader().load`` (ToolContext with the host env); when the
    loader does not register script tools itself, from a kernel agent's own registry."""
    counter = [0]

    async def _run(name, args, *, caps=None, work=None):
        work = work or tmp_path / "work"
        counter[0] += 1
        reg = loader_registry(work, tmp_path / f"st{counter[0]}", caps=caps, script_enabled=True)
        tname = f"script_{name}"
        if not reg.has(tname):
            kernel, _ = kmaker(work=work, caps=caps, script_enabled=True)
            agent = agent_of(kernel)
            tool_names(agent)
            reg = agent._loop.tools
        assert reg.has(tname), f"{tname} is not registered"
        return await reg.execute(tname, args)

    return _run


def is_err(result) -> bool:
    return bool(getattr(result, "is_error", False)) or str(result).lstrip().startswith("Error")


def payload(result):
    if getattr(result, "has_structured", False):
        return result.structured
    return json.loads(str(result))


def tool(tmp_path, name, body, *, io=NET_IO, **over):
    return write_script_tool(tmp_path / "work", name, script=py_script(body), io=io, **over)


ECHO_STDIN = """import sys, json
raw = sys.stdin.buffer.read().decode("utf-8")
print(json.dumps({"stdin": raw}))
"""

ENV_DUMP = """import os, sys, json
sys.stdin.read()
h = os.environ.get("HOME")
listing = sorted(os.listdir(h)) if h and os.path.isdir(h) else None
if h and os.path.isdir(h):
    open(os.path.join(h, "touched"), "w").write("x")
print(json.dumps({"env": dict(os.environ), "home_listing": listing, "cwd": os.getcwd()}))
"""

SUM = """import sys, json
args = json.load(sys.stdin)
print(json.dumps({"sum": args["a"] + args["b"]}))
"""

SUM_PARAMS = {"type": "object", "properties": {"a": {"type": "integer"},
                                               "b": {"type": "integer"}},
              "required": ["a", "b"]}

SHELL_ARTIFACTS = {"PWD", "SHLVL", "_"}


# -- success path --------------------------------------------------------------------------------------


@pytest.mark.fr("FR-010")
async def test_success_returns_stdout_json(tmp_path, run):
    tool(tmp_path, "h012-sum", SUM, parameters=SUM_PARAMS)
    r = await run("h012-sum", {"a": 2, "b": 40})
    assert not is_err(r), str(r)
    assert payload(r) == {"sum": 42}


@pytest.mark.fr("FR-010")
@pytest.mark.parametrize("value", [42, "text", [1, 2], None, True])
async def test_any_single_json_value_is_accepted(tmp_path, run, value):
    tool(tmp_path, "h012-val", f"import sys\nsys.stdin.read()\nprint({json.dumps(json.dumps(value))})\n")
    r = await run("h012-val", {})
    assert not is_err(r), str(r)
    assert payload(r) == value


@pytest.mark.fr("FR-010")
async def test_stdin_is_canonical_json_of_arguments(tmp_path, run):
    params = {"type": "object", "properties": {
        "b": {"type": "integer"}, "a": {"type": "string"}, "c": {"type": "array"},
        "d": {"type": "object"}}}
    tool(tmp_path, "h012-stdin", ECHO_STDIN, parameters=params)
    args = {"b": 1, "a": "é ü ✓", "c": [3, 1], "d": {"z": None, "y": 1.5}}
    r = await run("h012-stdin", args)
    assert not is_err(r), str(r)
    expected = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert payload(r)["stdin"] == expected


@pytest.mark.fr("FR-010")
async def test_cwd_is_the_bundle_dir(tmp_path, run):
    b = tool(tmp_path, "h012-cwd", ENV_DUMP)
    r = await run("h012-cwd", {})
    assert Path(payload(r)["cwd"]).resolve() == b.resolve()


# -- environment ---------------------------------------------------------------------------------------


@pytest.mark.fr("FR-010")
async def test_environment_is_exactly_path_home_lang(tmp_path, run, monkeypatch, home):
    monkeypatch.setenv("H012_SECRET_TOKEN", "must-not-leak")
    tool(tmp_path, "h012-env", ENV_DUMP)
    r = await run("h012-env", {})
    assert not is_err(r), str(r)
    env = payload(r)["env"]
    assert set(env) - SHELL_ARTIFACTS == {"PATH", "HOME", "LANG"}, sorted(env)
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["LANG"] == "C.UTF-8"
    assert env["HOME"] not in (str(home), os.path.expanduser("~muk"), str(tmp_path / "work"))
    assert "must-not-leak" not in json.dumps(env)


@pytest.mark.fr("FR-010")
async def test_home_is_a_fresh_empty_dir_per_call(tmp_path, run):
    tool(tmp_path, "h012-home", ENV_DUMP)
    first = payload(await run("h012-home", {}))
    second = payload(await run("h012-home", {}))
    assert first["home_listing"] == [] and second["home_listing"] == []


@pytest.mark.fr("FR-010")
@pytest.mark.parametrize("effects,present", [
    (["network"], False),
    (["network", "exec"], False),
    (["network", "read-workspace"], True),
    (["network", "write-workspace"], True),
    (["network", "read-workspace", "write-workspace"], True),
], ids=["network", "exec", "read", "write", "read-write"])
async def test_moeka_workspace_only_with_workspace_effects(tmp_path, run, effects, present):
    io = {"inputs": ["text/query"], "outputs": ["data/json"], "effects": effects}
    tool(tmp_path, "h012-ws", ENV_DUMP, io=io)
    env = payload(await run("h012-ws", {}))["env"]
    assert ("MOEKA_WORKSPACE" in env) is present, sorted(env)
    if present:
        assert Path(env["MOEKA_WORKSPACE"]).resolve() == (tmp_path / "work").resolve()
        assert set(env) - SHELL_ARTIFACTS == {"PATH", "HOME", "LANG", "MOEKA_WORKSPACE"}


# -- argument validation ------------------------------------------------------------------------------

MARKER = """import os, sys, json
sys.stdin.read()
open(os.path.join(os.environ["MOEKA_WORKSPACE"], "ran.marker"), "w").write("x")
print(json.dumps({"ran": True}))
"""

WS_NET = {"inputs": ["text/query"], "outputs": ["data/json"],
          "effects": ["network", "write-workspace"]}


@pytest.mark.fr("FR-010")
@pytest.mark.parametrize("args", [{}, {"a": "abc", "b": 1}, {"a": 1}],
                         ids=["missing", "wrong-type", "partial"])
async def test_invalid_arguments_rejected_before_running(tmp_path, run, args):
    tool(tmp_path, "h012-args", MARKER, io=WS_NET, parameters=SUM_PARAMS)
    r = await run("h012-args", args)
    assert is_err(r), str(r)
    assert not (tmp_path / "work" / "ran.marker").exists()


@pytest.mark.fr("FR-010")
async def test_valid_arguments_run(tmp_path, run):
    tool(tmp_path, "h012-args-ok", MARKER, io=WS_NET, parameters=SUM_PARAMS)
    r = await run("h012-args-ok", {"a": 1, "b": 2})
    assert not is_err(r), str(r)
    assert (tmp_path / "work" / "ran.marker").exists()


# -- timeout --------------------------------------------------------------------------------------------

SLEEPER = """import os, subprocess, sys, time
sys.stdin.read()
p = subprocess.Popen(["/bin/sleep", "300"])
with open(os.path.join(os.environ["MOEKA_WORKSPACE"], "gc.pid"), "w") as fh:
    fh.write(str(p.pid))
with open(os.path.join(os.environ["MOEKA_WORKSPACE"], "child.pid"), "w") as fh:
    fh.write(str(os.getpid()))
time.sleep(300)
"""


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return False
    return fields[0] not in ("Z", "X")


@pytest.mark.fr("FR-010")
async def test_timeout_kills_the_whole_process_group(tmp_path, run):
    tool(tmp_path, "h012-sleep", SLEEPER, io=WS_NET, timeout_s=1)
    start = time.monotonic()
    r = await run("h012-sleep", {})
    elapsed = time.monotonic() - start
    assert is_err(r), str(r)
    assert "timeout" in str(r).lower()
    assert elapsed < 20
    gc_file = tmp_path / "work" / "gc.pid"
    assert gc_file.exists(), "fixture: the script did not start its grandchild"
    pids = [int(gc_file.read_text()), int((tmp_path / "work" / "child.pid").read_text())]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.1)
    try:
        assert not any(_alive(p) for p in pids), "grandchild sleeper survived the timeout"
    finally:
        for p in pids:
            try:
                os.kill(p, 9)
            except OSError:
                pass


# -- failures -------------------------------------------------------------------------------------------

FAIL = """import sys
sys.stdin.read()
sys.stdout.write("{}")
sys.stderr.write("HEAD-START" + "y" * 2990 + "TAIL-END-MARK")
sys.exit(37)
"""


@pytest.mark.fr("FR-010")
async def test_nonzero_exit_reports_code_and_stderr_tail(tmp_path, run):
    tool(tmp_path, "h012-fail", FAIL)
    r = await run("h012-fail", {})
    text = str(r)
    assert is_err(r), text
    assert "37" in text
    stderr = "HEAD-START" + "y" * 2990 + "TAIL-END-MARK"
    assert stderr[-2000:] in text
    assert "HEAD-START" not in text


@pytest.mark.fr("FR-010")
@pytest.mark.parametrize("stdout", ["hello not json", "1 2", "", "{\"a\": 1} trailing"],
                         ids=["text", "two-values", "empty", "trailing"])
async def test_non_json_stdout_is_an_error_with_stderr(tmp_path, run, stdout):
    body = ("import sys\nsys.stdin.read()\nsys.stderr.write('NJ-STDERR-MARK')\n"
            f"sys.stdout.write({stdout!r})\n")
    tool(tmp_path, "h012-nj", body)
    r = await run("h012-nj", {})
    assert is_err(r), str(r)
    assert "NJ-STDERR-MARK" in str(r)


@pytest.mark.fr("FR-010")
async def test_stdout_over_one_mib_is_an_error(tmp_path, run):
    tool(tmp_path, "h012-big", "import sys, json\nsys.stdin.read()\n"
                               "sys.stdout.write(json.dumps('x' * (1024 * 1024 + 64)))\n")
    r = await run("h012-big", {})
    assert is_err(r)


@pytest.mark.fr("FR-010")
async def test_stdout_under_limit_is_fine(tmp_path, run):
    tool(tmp_path, "h012-mid", "import sys, json\nsys.stdin.read()\n"
                               "sys.stdout.write(json.dumps('x' * (512 * 1024)))\n")
    r = await run("h012-mid", {})
    assert not is_err(r)
    assert payload(r) == "x" * (512 * 1024)


# -- output schema --------------------------------------------------------------------------------------

OUT_SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


@pytest.mark.fr("FR-010")
async def test_output_schema_violation_is_an_error(tmp_path, run):
    tool(tmp_path, "h012-osbad", "import sys\nsys.stdin.read()\nprint('{\"ok\": \"yes\"}')\n",
         output_schema=OUT_SCHEMA)
    r = await run("h012-osbad", {})
    assert is_err(r), str(r)
    assert "result failed schema" in str(r)


@pytest.mark.fr("FR-010")
async def test_output_schema_match_passes(tmp_path, run):
    tool(tmp_path, "h012-osok", "import sys\nsys.stdin.read()\nprint('{\"ok\": true}')\n",
         output_schema=OUT_SCHEMA)
    r = await run("h012-osok", {})
    assert not is_err(r), str(r)
    assert payload(r) == {"ok": True}


# -- network-less tools run on every host -------------------------------------------------------------


@pytest.mark.fr("FR-010")
async def test_networkless_tool_runs_whatever_the_sandbox(tmp_path, run):
    """Without ``network`` the tool runs under bwrap when bwrap is usable, else unsandboxed;
    either way the call succeeds (interpretation: a bwrap binary that cannot create
    namespaces is "unavailable")."""
    write_script_tool(tmp_path / "work", "h012-nonet", script=SH_OK,
                      io={"inputs": ["text/query"], "outputs": ["data/json"], "effects": ["none"]})
    r = await run("h012-nonet", {})
    assert not is_err(r), f"{bwrap_state()}: {r}"
    assert payload(r) == {"ok": True}


# -- FR-011 trace event ---------------------------------------------------------------------------------

TRACE_BODY = """import sys, json
raw = sys.stdin.read()
print(json.dumps({"out": "OUT-SECRET-456", "len": len(raw)}))
"""

EVENT_KEYS = {"tool", "effects_declared", "sandboxed", "exit_code", "duration_ms"}


def _kernel_call(kmaker, work, name, args, sink):
    fake = fake_with(tool_call(f"script_{name}", args), "done")
    kernel, fake = kmaker(work=work, script_enabled=True, fake=fake)
    agent_of(kernel).run_sync("go")
    return ev(sink, "script_tool.call"), tool_messages(fake)


@pytest.mark.fr("FR-011")
def test_trace_event_fields(kmaker, tmp_path, sink):
    io = {"inputs": ["text/query"], "outputs": ["data/json"],
          "effects": ["read-workspace", "network"]}
    tool(tmp_path, "h012-tr", TRACE_BODY, io=io)
    events, msgs = _kernel_call(kmaker, tmp_path / "work", "h012-tr", {"q": "ARG-SECRET-123"},
                                sink)
    assert any("OUT-SECRET-456" in m for m in msgs), "fixture: the tool did not run"
    assert len(events) == 1, events
    e = strip(events[0])
    assert set(e) == EVENT_KEYS, sorted(e)
    assert e["tool"] in ("script_h012-tr", "h012-tr")
    assert sorted(e["effects_declared"]) == ["network", "read-workspace"]
    assert e["sandboxed"] is False
    assert e["exit_code"] == 0
    assert isinstance(e["duration_ms"], (int, float)) and e["duration_ms"] >= 0


@pytest.mark.fr("FR-011")
def test_trace_event_has_no_arguments_or_output(kmaker, tmp_path, sink):
    tool(tmp_path, "h012-tr2", TRACE_BODY)
    events, _ = _kernel_call(kmaker, tmp_path / "work", "h012-tr2", {"q": "ARG-SECRET-123"}, sink)
    assert events
    dumped = json.dumps(events, default=str)
    assert "ARG-SECRET-123" not in dumped
    assert "OUT-SECRET-456" not in dumped


@pytest.mark.fr("FR-011")
def test_trace_event_on_failure_records_exit_code(kmaker, tmp_path, sink):
    tool(tmp_path, "h012-tr3", FAIL)
    events, _ = _kernel_call(kmaker, tmp_path / "work", "h012-tr3", {}, sink)
    assert len(events) == 1
    assert strip(events[0])["exit_code"] == 37


@pytest.mark.fr("FR-011", "FR-010")
def test_trace_sandboxed_flag_follows_bwrap(kmaker, tmp_path, sink):
    write_script_tool(tmp_path / "work", "h012-tr4", script=SH_OK,
                      io={"inputs": ["text/query"], "outputs": ["data/json"], "effects": ["none"]})
    events, _ = _kernel_call(kmaker, tmp_path / "work", "h012-tr4", {}, sink)
    assert len(events) == 1
    state, detail = bwrap_state()
    assert strip(events[0])["sandboxed"] is (state == "usable"), detail


@pytest.mark.fr("FR-011")
def test_one_event_per_call(kmaker, tmp_path, sink):
    tool(tmp_path, "h012-tr5", TRACE_BODY)
    fake = fake_with(tool_call("script_h012-tr5", {}, "c1"), tool_call("script_h012-tr5", {}, "c2"),
                     "done")
    kernel, fake = kmaker(work=tmp_path / "work", script_enabled=True, fake=fake)
    agent_of(kernel).run_sync("go")
    assert len(ev(sink, "script_tool.call")) == 2


# -- SC-003 network denial under bwrap ------------------------------------------------------------------

CONNECT = """#!/usr/bin/python3
import json, socket, sys
port = json.load(sys.stdin)["port"]
s = socket.socket()
s.settimeout(3)
try:
    s.connect(("127.0.0.1", port))
    ok = True
except OSError:
    ok = False
print(json.dumps({"connected": ok}))
"""


def _require_bwrap():
    state, detail = bwrap_state()
    if state == "absent":
        pytest.skip("SC-003 skipped: bwrap is absent on this host (`which bwrap` -> none)")
    if state == "broken":
        pytest.skip(f"SC-003 skipped: {detail}")
    if not os.path.exists("/usr/bin/python3"):
        pytest.skip("SC-003 skipped: /usr/bin/python3 (the probe interpreter) is absent")


@pytest.fixture
def listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    yield srv.getsockname()[1]
    srv.close()


PORT_PARAMS = {"type": "object", "properties": {"port": {"type": "integer"}},
               "required": ["port"]}


@pytest.mark.fr("SC-003", "FR-010")
async def test_networkless_tool_cannot_connect_under_bwrap(tmp_path, run, listener):
    _require_bwrap()
    write_script_tool(tmp_path / "work", "h012-nonet-tcp", script=CONNECT, parameters=PORT_PARAMS,
                      io={"inputs": ["text/query"], "outputs": ["data/json"], "effects": ["none"]})
    r = await run("h012-nonet-tcp", {"port": listener})
    assert not is_err(r), str(r)
    assert payload(r) == {"connected": False}


@pytest.mark.fr("SC-003", "FR-010")
async def test_network_tool_can_connect(tmp_path, run, listener):
    """Control for SC-003 (and the probe itself): declaring ``network`` skips the sandbox."""
    if not os.path.exists("/usr/bin/python3"):
        pytest.skip("/usr/bin/python3 (the probe interpreter) is absent")
    write_script_tool(tmp_path / "work", "h012-net-tcp", script=CONNECT, parameters=PORT_PARAMS,
                      io=NET_IO)
    r = await run("h012-net-tcp", {"port": listener})
    assert not is_err(r), str(r)
    assert payload(r) == {"connected": True}
