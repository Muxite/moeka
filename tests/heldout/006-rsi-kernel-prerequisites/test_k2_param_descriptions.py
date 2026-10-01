"""K2: tool parameter-description overrides (FR-007 to FR-013, US4)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from _h006 import calls, ev, tc, thaw, tool_info, tools_of

from moeka.agents import AgentSpec
from moeka.testing import FakeProvider
from moeka.variants import Variant


def VariantError():  # noqa: N802 - imported lazily so a missing name fails per test
    from moeka.errors import VariantError as cls

    return cls


VALID_PATHS = ["path", "edits[].old_text", "grid[][]", "a.b.c", "x[].y[][].z", "ünï", "a-b_c d"]
INVALID_PATHS = ["", ".a", "a.", "a..b", "[]", "a[", "a[0]", "a]", "a[]b", "a.[]", "[]a",
                 "a[][", "a.b.", "a[ ]"]


def _strip(schema: Any, paths: list[list[str]]) -> Any:
    """Copy of *schema* with the description key removed at each resolved node."""
    out = copy.deepcopy(thaw(schema))
    for segs in paths:
        node = out
        for seg in segs:
            node = node["items"] if seg == "[]" else node["properties"][seg]
        node.pop("description", None)
    return out


def _segs(path: str) -> list[str]:
    out: list[str] = []
    for seg in path.split("."):
        name = seg.split("[]", 1)[0]
        out.append(name)
        out.extend(["[]"] * seg.count("[]"))
    return out


def _node(schema: Any, path: str) -> Any:
    node = thaw(schema)
    for seg in _segs(path):
        node = node["items"] if seg == "[]" else node["properties"][seg]
    return node


def _provider_tool(fake: FakeProvider, name: str) -> dict[str, Any]:
    tools = fake.calls[0].kwargs["tools"]
    for t in tools:
        fn = t.get("function", t)
        if fn.get("name") == name:
            return thaw(fn)
    raise AssertionError(f"{name} not sent")


def _agent(kmaker, variant=None, fake=None, **kw):
    kernel = kmaker(fake or FakeProvider(default="ok"), variant=variant)
    return kernel.agent(AgentSpec(name=kw.pop("name", "k2"), **kw))


# -- FR-007 / FR-008: the field --------------------------------------------------------


@pytest.mark.fr("FR-007", "FR-041")
def test_default_empty_equal_and_hashable():
    assert Variant() == Variant(tool_param_descriptions={})
    assert hash(Variant()) == hash(Variant(tool_param_descriptions={}))
    a = Variant(tool_param_descriptions={"read_file": {"path": "X"}})
    b = Variant(tool_param_descriptions={"read_file": {"path": "X"}})
    c = Variant(tool_param_descriptions={"read_file": {"path": "Y"}})
    assert a == b and hash(a) == hash(b)
    assert a != c and a != Variant()
    assert len({a, b, c}) == 2
    assert dict(Variant().tool_param_descriptions) == {}


@pytest.mark.fr("FR-007")
def test_stored_frozen():
    src = {"read_file": {"path": "X"}}
    v = Variant(tool_param_descriptions=src)
    h = hash(v)
    src["read_file"]["path"] = "MUTATED"
    src["write_file"] = {"path": "Z"}
    assert thaw(v.tool_param_descriptions) == {"read_file": {"path": "X"}}
    assert hash(v) == h
    with pytest.raises(TypeError):
        v.tool_param_descriptions["x"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        v.tool_param_descriptions["read_file"]["path"] = "Q"  # type: ignore[index]


@pytest.mark.fr("FR-008")
@pytest.mark.parametrize("value", [
    ["read_file"], "read_file", 3, {1: {"path": "X"}}, {"": {"path": "X"}},
    {"read_file": "path"}, {"read_file": ["path"]}, {"read_file": {1: "X"}},
    {"read_file": {"path": 3}}, {"read_file": {"path": None}},
])
def test_type_errors(value):
    Variant(tool_param_descriptions={"read_file": {"path": "ok"}})  # the field exists
    with pytest.raises(TypeError):
        Variant(tool_param_descriptions=value)


@pytest.mark.fr("FR-008")
@pytest.mark.parametrize("path", INVALID_PATHS)
def test_invalid_path_grammar_is_value_error(path):
    with pytest.raises(ValueError):
        Variant(tool_param_descriptions={"read_file": {path: "X"}})


@pytest.mark.fr("FR-008")
@pytest.mark.parametrize("path", VALID_PATHS)
def test_valid_path_grammar(path):
    v = Variant(tool_param_descriptions={"some_tool": {path: "X"}})
    assert thaw(v.tool_param_descriptions) == {"some_tool": {path: "X"}}


# -- FR-009 / FR-010: resolution and visibility -------------------------------------------


@pytest.mark.fr("FR-009", "FR-010")
def test_us4_read_file_path_description(kmaker):
    """US4 scenario 1."""
    base = thaw(tool_info(_agent(kmaker, name="base"), "read_file").parameters)
    agent = _agent(kmaker, Variant(tool_param_descriptions={"read_file": {"path": "X"}}))
    params = thaw(tool_info(agent, "read_file").parameters)
    assert params["properties"]["path"]["description"] == "X"
    assert _strip(params, [["path"]]) == _strip(base, [["path"]])
    # nothing else changed either (only one description differs)
    base["properties"]["path"]["description"] = "X"
    assert params == base


@pytest.mark.fr("FR-009")
def test_nested_array_item_paths(kmaker):
    paths = {"edits[].old_text": "OLD", "edits[]": "EACH", "edits": "LIST"}
    base_agent = _agent(kmaker, name="base")
    base = thaw(tool_info(base_agent, "apply_patch").parameters)
    agent = _agent(kmaker, Variant(tool_param_descriptions={"apply_patch": paths}))
    params = thaw(tool_info(agent, "apply_patch").parameters)
    for path, text in paths.items():
        assert _node(params, path)["description"] == text, path
    assert _strip(params, [_segs(p) for p in paths]) == _strip(base, [_segs(p) for p in paths])


@pytest.mark.fr("FR-009")
def test_description_added_when_absent(kmaker):
    """Find a schema node without a description and override it: the key is added."""
    base_agent = _agent(kmaker, name="base")
    target = None
    for info in tools_of(base_agent):
        params = thaw(info.parameters)

        def walk(node: Any, path: str) -> str | None:
            if not isinstance(node, dict):
                return None
            for name, child in (node.get("properties") or {}).items():
                if any(c in name for c in ".[]"):
                    continue
                p = f"{path}.{name}" if path else name
                if isinstance(child, dict) and "description" not in child:
                    return p
                hit = walk(child, p)
                if hit:
                    return hit
            items = node.get("items")
            if path and isinstance(items, dict):
                if "description" not in items:
                    return path + "[]"
                return walk(items, path + "[]")
            return None

        found = walk(params, "")
        if found:
            target = (info.name, found, params)
            break
    if target is None:
        pytest.skip("every built-in schema node already has a description")
    name, path, base = target
    agent = _agent(kmaker, Variant(tool_param_descriptions={name: {path: "ADDED"}}))
    params = thaw(tool_info(agent, name).parameters)
    assert _node(params, path)["description"] == "ADDED"
    assert _strip(params, [_segs(path)]) == _strip(base, [_segs(path)])


@pytest.mark.fr("FR-010")
def test_override_reaches_provider_request_and_fingerprint(kmaker):
    fake = FakeProvider(["done"])
    base = _agent(kmaker, name="base").fingerprint()
    agent = _agent(kmaker, Variant(tool_param_descriptions={"read_file": {"path": "X"}}),
                   fake=fake)
    assert agent.run_sync("hi").stop_reason == "completed"
    sent = _provider_tool(fake, "read_file")
    assert sent["parameters"]["properties"]["path"]["description"] == "X"
    fp = agent.fingerprint()
    assert fp.components["tools"] != base.components["tools"]


@pytest.mark.fr("FR-010")
def test_per_kernel_isolation_and_base_class_untouched(kmaker):
    a = _agent(kmaker, Variant(name="va", tool_param_descriptions={"read_file": {"path": "A"}}))
    b = _agent(kmaker, Variant(name="vb", tool_param_descriptions={"read_file": {"path": "B"}}))
    assert thaw(tool_info(a, "read_file").parameters)["properties"]["path"]["description"] == "A"
    assert thaw(tool_info(b, "read_file").parameters)["properties"]["path"]["description"] == "B"
    plain = _agent(kmaker)
    desc = thaw(tool_info(plain, "read_file").parameters)["properties"]["path"]["description"]
    assert desc not in ("A", "B")
    # re-reading the first two after the plain build still gives their own text
    assert thaw(tool_info(a, "read_file").parameters)["properties"]["path"]["description"] == "A"
    later = _agent(kmaker, name="later")
    assert thaw(tool_info(later, "read_file").parameters)["properties"]["path"][
        "description"] == desc


@pytest.mark.fr("FR-010")
def test_two_agents_same_kernel_share_variant(kmaker):
    kernel = kmaker(FakeProvider(default="ok"),
                    variant=Variant(tool_param_descriptions={"read_file": {"path": "S"}}))
    a1 = kernel.agent(AgentSpec(name="one"))
    a2 = kernel.agent(AgentSpec(name="two", tools_allow=("read_file",)))
    for agent in (a1, a2):
        assert thaw(tool_info(agent, "read_file").parameters)["properties"]["path"][
            "description"] == "S"


# -- FR-011: unknown tool ignored, bad path raises -----------------------------------------


@pytest.mark.fr("FR-011")
def test_unknown_tool_entry_ignored(kmaker):
    agent = _agent(kmaker, Variant(tool_param_descriptions={
        "no_such_tool_h006": {"whatever.deep[]": "X"}, "read_file": {"path": "X"}}))
    assert thaw(tool_info(agent, "read_file").parameters)["properties"]["path"][
        "description"] == "X"
    agent.fingerprint()


@pytest.mark.fr("FR-011")
def test_tool_not_loaded_by_scope_is_ignored(kmaker):
    agent = _agent(kmaker, Variant(tool_param_descriptions={"write_file": {"nonexistent": "X"}}),
                   tools_allow=("read_file",))
    assert [t.name for t in tools_of(agent)] == ["read_file"]


def _check_variant_error(exc, variant: Variant, tool: str, path: str) -> None:
    assert isinstance(exc, ValueError)
    assert exc.tool == tool
    assert exc.path == path
    assert exc.variant in (variant, variant.name)


@pytest.mark.fr("FR-011")
@pytest.mark.parametrize("path", ["nonexistent", "path.inner", "path[]", "offset[].x",
                                  "edits", "properties", "path.description"])
def test_unresolvable_path_raises_variant_error_on_tools(kmaker, path):
    """US4 scenario 2 (and other non-resolving shapes)."""
    variant = Variant(name="bad-v", tool_param_descriptions={"read_file": {path: "X"}})
    agent = _agent(kmaker, variant)
    with pytest.raises(VariantError()) as info:
        tools_of(agent)
    _check_variant_error(info.value, variant, "read_file", path)


@pytest.mark.fr("FR-011")
def test_variant_error_on_first_fingerprint(kmaker):
    variant = Variant(name="bad-fp", tool_param_descriptions={"read_file": {"nonexistent": "X"}})
    agent = _agent(kmaker, variant)
    with pytest.raises(VariantError()) as info:
        agent.fingerprint()
    _check_variant_error(info.value, variant, "read_file", "nonexistent")


@pytest.mark.fr("FR-011")
def test_variant_error_on_first_run(kmaker):
    fake = FakeProvider(default="ok")
    variant = Variant(name="bad-run", tool_param_descriptions={"read_file": {"nonexistent": "X"}})
    agent = _agent(kmaker, variant, fake=fake)
    with pytest.raises(VariantError()) as info:
        agent.run_sync("hi")
    _check_variant_error(info.value, variant, "read_file", "nonexistent")
    assert fake.calls == []


@pytest.mark.fr("FR-011", "FR-041")
def test_variant_error_is_public_value_error():
    import moeka.errors

    assert issubclass(VariantError(), ValueError)
    assert "VariantError" in getattr(moeka.errors, "__all__", ["VariantError"])


# -- FR-012: coverage ---------------------------------------------------------------------


@pytest.mark.fr("FR-012")
def test_actions_are_not_overridden(kmaker):
    def lookup_h006(query: str) -> str:
        """Look something up."""
        return "found"

    base = _agent(kmaker, name="base", actions=(lookup_h006,))
    base_params = thaw(tool_info(base, "lookup_h006").parameters)
    agent = _agent(kmaker, Variant(tool_param_descriptions={"lookup_h006": {"query": "NEW"}}),
                   actions=(lookup_h006,))
    params = thaw(tool_info(agent, "lookup_h006").parameters)
    assert params == base_params
    assert params.get("properties", {}).get("query", {}).get("description") != "NEW"


@pytest.mark.fr("FR-012")
def test_builtins_beyond_filesystem_are_covered(kmaker):
    agent = _agent(kmaker, Variant(tool_param_descriptions={
        "grep": {"pattern": "G"}, "list_dir": {"path": "L"}, "exec": {"command": "E"}}))
    infos = {t.name: thaw(t.parameters) for t in tools_of(agent)}
    assert infos["grep"]["properties"]["pattern"]["description"] == "G"
    assert infos["list_dir"]["properties"]["path"]["description"] == "L"
    assert infos["exec"]["properties"]["command"]["description"] == "E"


# -- FR-013: validation unchanged ---------------------------------------------------------


@pytest.mark.fr("FR-013")
def test_validation_is_unchanged(kmaker, sink, tmp_path):
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "work" / "f.txt").write_text("hello")
    script = [
        calls(("read_file", {"path": None}, "bad1"), ("read_file", {"path": "f.txt"}, "good1"),
              ("read_file", {"path": "f.txt", "offset": 0}, "bad2"),
              ("read_file", {}, "bad3")),
        "done",
    ]
    results = {}
    for label, variant in (("base", None), ("over", Variant(tool_param_descriptions={
            "read_file": {"path": "X", "offset": "Y"}}))):
        start = len(sink.events)
        agent = _agent(kmaker, variant, fake=FakeProvider(list(script)), name=f"v-{label}")
        assert agent.run_sync("go").stop_reason == "completed"
        results[label] = {e["call_id"]: e["args_valid"]
                          for e in sink.events[start:] if e.get("event") == "tool.call"}
    assert results["base"] == results["over"]
    assert results["base"]["good1"] is True and results["base"]["bad1"] is False


@pytest.mark.fr("FR-013")
def test_validation_unchanged_without_trace_dependency(kmaker, sink):
    fake = FakeProvider([tc("read_file", {"path": "f.txt", "limit": "abc"}), "done"])
    agent = _agent(kmaker, Variant(tool_param_descriptions={"read_file": {"path": "X"}}),
                   fake=fake)
    agent.run_sync("go")
    [call] = ev(sink, "tool.call")
    assert call["args_valid"] is False and call["error_kind"] == "invalid_args"
