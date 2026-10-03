"""Spec 012 Part C: script-tool manifests, execution and registration."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import yaml
from _certkit import Key, certify, make_script, python_script, write_trust

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.capabilities import script_tool
from nanobot.capabilities.manifest import ManifestError
from nanobot.capabilities.script_tool import ScriptManifest, ScriptTool, load_manifest
from nanobot.config.schema import Config

ECHO_ARGS = python_script(
    "import json, sys\nprint(json.dumps({'got': json.load(sys.stdin)}))\n"
)


@pytest.fixture
def no_bwrap(monkeypatch):
    """Run unsandboxed (python entries live outside the sandbox's bind mounts)."""
    monkeypatch.setattr(script_tool, "bwrap_available", lambda: False)


# -- FR-009 manifest ------------------------------------------------------------------


def test_load_manifest_valid(tmp_path):
    bundle = make_script(tmp_path, "echo", ECHO_ARGS, timeout_s=5,
                         output_schema={"type": "object"})
    manifest = load_manifest(bundle)
    assert isinstance(manifest, ScriptManifest)
    assert manifest.name == "echo" and manifest.tool_name == "script_echo"
    assert manifest.timeout_s == 5 and manifest.entry == "run"
    assert manifest.entry_path == bundle / "run"
    assert manifest.io.effects == ("none",) and manifest.io.outputs == ("data/json",)
    assert manifest.output_schema == {"type": "object"}


def test_timeout_defaults_to_30(tmp_path):
    assert load_manifest(make_script(tmp_path, "echo", ECHO_ARGS)).timeout_s == 30


def _broken(tmp_path: Path, field: str) -> Path:
    bundle = make_script(tmp_path, "tool", ECHO_ARGS)
    data = yaml.safe_load((bundle / "tool.yaml").read_text())
    if field == "name":
        data["name"] = "other"
    elif field == "name-rule":
        bundle = bundle.rename(bundle.with_name("Bad_Name"))
        data["name"] = "Bad_Name"
    elif field == "description":
        data["description"] = ""
    elif field == "parameters":
        data["parameters"] = {"type": "array"}
    elif field == "output_schema":
        data["output_schema"] = "object"
    elif field == "io":
        data.pop("io")
    elif field == "io.effects":
        data["io"] = {"effects": ["teleport"]}
    elif field == "timeout_s":
        data["timeout_s"] = 601
    elif field == "timeout_s-bool":
        data["timeout_s"] = True
    elif field == "entry-missing":
        data.pop("entry")
    elif field == "entry-escape":
        (tmp_path / "outside").write_text("#!/bin/sh\n")
        (tmp_path / "outside").chmod(0o755)
        data["entry"] = "../../outside"
    elif field == "entry-absolute":
        data["entry"] = "/bin/true"
    elif field == "entry-symlink":
        (bundle / "link").symlink_to("run")
        data["entry"] = "link"
    elif field == "entry-dir-symlink":
        (bundle / "real").mkdir()
        (bundle / "real" / "run").write_text("#!/bin/sh\n")
        (bundle / "real" / "run").chmod(0o755)
        (bundle / "linkdir").symlink_to("real")
        data["entry"] = "linkdir/run"
    elif field == "entry-not-exec":
        (bundle / "run").chmod(0o644)
    elif field == "entry-nonexistent":
        data["entry"] = "nope"
    elif field == "yaml":
        (bundle / "tool.yaml").write_text("- a list\n")
        return bundle
    (bundle / "tool.yaml").write_text(yaml.safe_dump(data))
    return bundle


@pytest.mark.parametrize("case,field", [
    ("name", "name"), ("name-rule", "name"), ("description", "description"),
    ("parameters", "parameters"), ("output_schema", "output_schema"), ("io", "io"),
    ("io.effects", "io.effects"), ("timeout_s", "timeout_s"), ("timeout_s-bool", "timeout_s"),
    ("entry-missing", "entry"), ("entry-escape", "entry"), ("entry-absolute", "entry"),
    ("entry-symlink", "entry"), ("entry-dir-symlink", "entry"), ("entry-not-exec", "entry"),
    ("entry-nonexistent", "entry"), ("yaml", "tool.yaml"),
])
def test_manifest_errors_name_the_field(tmp_path, case, field):
    with pytest.raises(ManifestError) as info:
        load_manifest(_broken(tmp_path, case))
    assert info.value.field == field
    assert str(info.value).startswith(field)


def test_missing_manifest(tmp_path):
    (tmp_path / "x").mkdir()
    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "x")


# -- FR-010 execution -----------------------------------------------------------------


async def test_arguments_on_stdin_and_json_result(tmp_path, no_bwrap):
    params = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    tool = ScriptTool(make_script(tmp_path, "echo", ECHO_ARGS, parameters=params))
    assert tool.name == "script_echo"
    assert tool.to_schema()["function"]["parameters"]["required"] == ["n"]
    result = await tool.execute(n=3)
    assert not result.is_error
    assert json.loads(result) == {"got": {"n": 3}}
    assert result.structured == {"got": {"n": 3}}
    assert result.metadata["sandboxed"] is False and result.metadata["exit_code"] == 0


async def test_invalid_arguments_are_rejected(tmp_path, no_bwrap):
    params = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    tool = ScriptTool(make_script(tmp_path, "echo", ECHO_ARGS, parameters=params))
    result = await tool.execute(n="three")
    assert result.is_error and "Invalid parameters" in result
    registry = ToolRegistry()
    registry.register(tool)
    assert (await registry.execute("script_echo", {})).is_error


async def test_cwd_is_the_bundle(tmp_path, no_bwrap):
    script = python_script("import json, os\nprint(json.dumps(os.getcwd()))\n")
    bundle = make_script(tmp_path, "cwd", script)
    assert json.loads(await ScriptTool(bundle).execute()) == str(bundle)


ENV_DUMP = python_script("import json, os\nprint(json.dumps(dict(os.environ)))\n")


async def test_environment_is_scrubbed(tmp_path, monkeypatch, no_bwrap):
    monkeypatch.setenv("SECRET_TOKEN", "leak")
    tool = ScriptTool(make_script(tmp_path, "env", ENV_DUMP), workspace=tmp_path)
    env = json.loads(await tool.execute())
    env.pop("LC_CTYPE", None)  # the interpreter may set it for C.UTF-8
    assert set(env) == {"PATH", "HOME", "LANG"}
    assert env["PATH"] == "/usr/bin:/bin" and env["LANG"] == "C.UTF-8"
    home = Path(env["HOME"])
    assert home.is_absolute() and home != Path.home() and not home.exists()  # cleaned up


@pytest.mark.parametrize("effect", ["read-workspace", "write-workspace"])
async def test_workspace_exported_only_with_workspace_effects(tmp_path, no_bwrap, effect):
    io = {"outputs": ["data/json"], "effects": [effect]}
    tool = ScriptTool(make_script(tmp_path, "env", ENV_DUMP, io=io), workspace=tmp_path)
    assert json.loads(await tool.execute())["MOEKA_WORKSPACE"] == str(tmp_path)
    plain = ScriptTool(make_script(tmp_path, "env2", ENV_DUMP), workspace=tmp_path)
    assert "MOEKA_WORKSPACE" not in json.loads(await plain.execute())


async def test_nonzero_exit_reports_code_and_stderr_tail(tmp_path, no_bwrap):
    script = "#!/bin/sh\nhead -c 5000 /dev/zero | tr '\\0' 'a' >&2\necho END >&2\nexit 3\n"
    result = await ScriptTool(make_script(tmp_path, "fail", script)).execute()
    assert result.is_error and "code 3" in result
    tail = result.split("chars):\n", 1)[1]
    assert tail.endswith("END\n") and len(tail) == 2000


async def test_non_json_stdout_is_an_error(tmp_path, no_bwrap):
    script = "#!/bin/sh\necho not json\necho oops >&2\n"
    result = await ScriptTool(make_script(tmp_path, "prose", script)).execute()
    assert result.is_error and "not one JSON value" in result and "oops" in result
    assert "exit code 0" in result


async def test_two_json_values_are_not_one(tmp_path, no_bwrap):
    script = "#!/bin/sh\necho 1\necho 2\n"
    assert (await ScriptTool(make_script(tmp_path, "two", script)).execute()).is_error


async def test_output_schema_is_checked(tmp_path, no_bwrap):
    script = "#!/bin/sh\necho '{\"count\": \"x\"}'\n"
    schema = {"type": "object", "properties": {"count": {"type": "integer"}},
              "required": ["count"]}
    tool = ScriptTool(make_script(tmp_path, "typed", script, output_schema=schema))
    from nanobot.kernel.typed import RESULT_SCHEMA_MARKER

    result = await tool.execute()
    assert result.is_error and RESULT_SCHEMA_MARKER in result and "count" in result
    assert result.metadata["exit_code"] == 0
    registry = ToolRegistry()
    registry.register(tool)
    via_registry = await registry.execute("script_typed", {})
    assert via_registry.is_error and RESULT_SCHEMA_MARKER in via_registry
    good = ScriptTool(make_script(tmp_path, "typed2", "#!/bin/sh\necho '{\"count\": 2}'\n",
                                  output_schema=schema))
    assert json.loads(await good.execute()) == {"count": 2}


async def test_stdout_over_one_mib_is_an_error(tmp_path, no_bwrap):
    script = "#!/bin/sh\nhead -c 1100000 /dev/zero | tr '\\0' '1'\n"
    result = await ScriptTool(make_script(tmp_path, "big", script)).execute()
    assert result.is_error and "1048576" in result


def _alive(pid: int) -> bool:
    """Running (a killed orphan may linger as a zombie until its new parent reaps it)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    return state not in ("Z", "X")


async def test_timeout_kills_the_whole_process_group(tmp_path, no_bwrap):
    marker = tmp_path / "child.pid"
    script = f"#!/bin/sh\nsleep 30 &\necho $! > {marker}\nsleep 30\n"
    tool = ScriptTool(make_script(tmp_path, "slow", script, timeout_s=1))
    started = time.monotonic()
    result = await tool.execute()
    assert time.monotonic() - started < 10
    assert result.is_error and "timeout" in result
    child = int(marker.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _alive(child):
            break
        time.sleep(0.05)
    else:
        pytest.fail("the backgrounded child survived the timeout")


async def test_trace_event_has_no_arguments_or_output(tmp_path, no_bwrap):
    from moeka.trace import MemoryTraceSink

    sink = MemoryTraceSink()
    tool = ScriptTool(make_script(tmp_path, "echo", ECHO_ARGS), trace=sink)
    await tool.execute(secret="hunter2")
    events = [e for e in sink.events if e.get("event") == "script_tool.call"]
    assert len(events) == 1
    event = events[0]
    assert event["tool"] == "script_echo" and event["effects_declared"] == ["none"]
    assert event["sandboxed"] is False and event["exit_code"] == 0
    assert isinstance(event["duration_ms"], int)
    assert "hunter2" not in json.dumps(event) and "got" not in json.dumps(event)


async def test_network_effect_never_sandboxes(tmp_path, monkeypatch):
    monkeypatch.setattr(script_tool, "bwrap_available", lambda: True)
    io = {"outputs": ["data/json"], "effects": ["network"]}
    tool = ScriptTool(make_script(tmp_path, "net", "#!/bin/sh\necho 1\n", io=io))
    assert tool.sandboxed is False
    assert ScriptTool(make_script(tmp_path, "off", "#!/bin/sh\necho 1\n")).sandboxed is True


def test_bwrap_argv_unshares_the_network(tmp_path):
    manifest = load_manifest(make_script(tmp_path, "x", "#!/bin/sh\necho 1\n"))
    argv = script_tool._bwrap_argv(manifest, tmp_path / "home", None)
    assert "--unshare-net" in argv and "--unshare-pid" in argv
    assert argv[-1] == str(manifest.entry_path)


def _listener() -> tuple[socket.socket, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server, server.getsockname()[1]


async def test_sc003_no_network_under_bwrap(tmp_path):
    """SC-003: without ``network``, a script cannot open a TCP connection (bwrap present)."""
    if not script_tool.bwrap_available():
        pytest.skip("bwrap cannot unshare the network on this host (absent or userns blocked)")
    if not Path("/usr/bin/python3").exists():
        pytest.skip("no /usr/bin/python3 inside the sandbox's /usr bind")
    server, port = _listener()
    threading.Thread(target=lambda: server.accept(), daemon=True).start()
    script = (
        "#!/usr/bin/python3\nimport json, socket\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {port}), timeout=2)\n"
        "    print(json.dumps('connected'))\n"
        "except OSError as exc:\n"
        "    print(json.dumps('blocked'))\n"
    )
    tool = ScriptTool(make_script(tmp_path, "net", script))
    result = await tool.execute()
    server.close()
    assert result.metadata["sandboxed"] is True
    assert json.loads(result) == "blocked"


# -- registration ---------------------------------------------------------------------


def _load(workspace: Path, *, enabled: bool, caps: dict | None = None,
          allow: list[str] | None = None) -> list[str]:
    config = Config(tools={"script": {"enabled": enabled}}, capabilities=caps or {})
    registry = ToolRegistry()
    loader = ToolLoader(test_classes=[])
    ctx = ToolContext(config=config.tools, workspace=str(workspace),
                      capabilities=config.capabilities)
    return loader.load(ctx, registry, allow=allow)


def test_registration_needs_the_flag(tmp_path):
    make_script(tmp_path, "echo", ECHO_ARGS)
    assert _load(tmp_path, enabled=False) == []
    assert _load(tmp_path, enabled=True) == ["script_echo"]
    assert _load(tmp_path, enabled=True, allow=["read_file"]) == []


def test_invalid_bundles_are_skipped(tmp_path, caplog_warnings):
    make_script(tmp_path, "good", ECHO_ARGS)
    bad = make_script(tmp_path, "bad", ECHO_ARGS)
    (bad / "run").chmod(0o644)
    assert _load(tmp_path, enabled=True) == ["script_good"]
    assert any("'bad'" in m and "entry" in m for m in caplog_warnings)


def test_enforcement_registers_only_certified_tools(tmp_path):
    key = Key()
    trust = write_trust(tmp_path / "trust", [key])
    work = tmp_path / "work"
    certify(make_script(work, "signed", ECHO_ARGS), key)
    make_script(work, "unsigned", ECHO_ARGS)
    caps = {"enforce_certs": True, "trust_dirs": [str(trust)]}
    assert _load(work, enabled=True, caps=caps) == ["script_signed"]
    assert sorted(_load(work, enabled=True, caps={**caps, "enforce_certs": False})) == [
        "script_signed", "script_unsigned",
    ]
    (work / "tools-script" / "signed" / "run").write_text(ECHO_ARGS + "# tampered\n")
    assert _load(work, enabled=True, caps=caps) == []


def test_script_tools_are_indexed_with_provenance(tmp_path):
    from nanobot.capabilities.library import tool_entries

    key = Key()
    trust = write_trust(tmp_path / "trust", [key])
    certify(make_script(tmp_path, "signed", ECHO_ARGS), key)
    config = Config(tools={"script": {"enabled": True}},
                    capabilities={"trust_dirs": [str(trust)]})
    registry = ToolRegistry()
    ToolLoader(test_classes=[]).load(
        ToolContext(config=config.tools, workspace=str(tmp_path),
                    capabilities=config.capabilities), registry,
    )
    entry = tool_entries([registry.get("script_signed")])[0]
    assert entry.cert_status == "certified" and len(entry.cert_id) == 16
    assert entry.io is not None and entry.io.effects == ("none",)


def test_kernel_agent_registers_and_traces_script_tools(tmp_path, monkeypatch):
    """Through ``moeka.Kernel``: ``tools.script.enabled`` registers the tool, and a call
    emits ``script_tool.call`` to the kernel trace sink."""
    import asyncio

    from moeka import Environment, Kernel, ModelSpec
    from moeka.agents import AgentSpec
    from moeka.testing import FakeProvider
    from moeka.trace import MemoryTraceSink

    monkeypatch.setattr(script_tool, "bwrap_available", lambda: False)
    work = tmp_path / "work"
    make_script(work, "echo", ECHO_ARGS)
    sink = MemoryTraceSink()
    env = Environment.from_config(Config(tools={"script": {"enabled": True}}),
                                  state_dir=tmp_path / "state", work_dir=work, trace=sink)
    kernel = Kernel(env)
    try:
        kernel.llm.register_provider(
            "default", FakeProvider(default="ok"),
            ModelSpec(name="default", model="fake", provider="openai"),
        )
        agent = kernel.agent(AgentSpec(name="t012"))
        loop = agent._ensure_loop()
        assert loop.tools.has("script_echo")
        result = asyncio.run(loop.tools.execute("script_echo", {"a": 1}))
        assert json.loads(result) == {"got": {"a": 1}}
        assert [e["tool"] for e in sink.events if e.get("event") == "script_tool.call"] == [
            "script_echo"
        ]
    finally:
        kernel.close()
