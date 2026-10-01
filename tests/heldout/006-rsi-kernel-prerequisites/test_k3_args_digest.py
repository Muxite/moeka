"""K3: argument digest on ``tool.call`` (FR-014 to FR-019, FR-040, SC-003, US2)."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest
from _h006 import TOOL_CALL_KEYS_BEFORE, calls, ev, ref_args_digest, strip, tc
from pydantic import BaseModel

from moeka.agents import AgentSpec
from moeka.testing import FakeProvider
from moeka.tools import FunctionTool

HEX64 = re.compile(r"[0-9a-f]{64}")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest():
    from moeka.trace import args_digest

    return args_digest


# -- FR-015 / FR-016: the function -----------------------------------------------------------


@pytest.mark.fr("FR-016", "FR-041")
def test_args_digest_is_exported():
    import moeka.trace

    assert callable(moeka.trace.args_digest)
    assert "args_digest" in getattr(moeka.trace, "__all__", ["args_digest"])


@pytest.mark.fr("FR-015", "FR-016")
def test_key_order_does_not_matter():
    d = _digest()
    assert d({"a": 1, "b": 2}) == d({"b": 2, "a": 1}) == _sha('{"a":1,"b":2}')
    nested1 = {"z": {"y": 1, "x": [3, {"q": 1, "p": 2}]}, "a": None}
    nested2 = {"a": None, "z": {"x": [3, {"p": 2, "q": 1}], "y": 1}}
    assert d(nested1) == d(nested2) == _sha('{"a":null,"z":{"x":[3,{"p":2,"q":1}],"y":1}}')


@pytest.mark.fr("FR-015", "FR-016")
def test_list_order_matters():
    d = _digest()
    assert d({"a": [1, 2]}) != d({"a": [2, 1]})


@pytest.mark.fr("FR-015", "FR-016")
def test_unicode_is_not_ascii_escaped():
    d = _digest()
    value = {"q": "café ✓ 日本 \U0001f600"}
    assert d(value) == _sha('{"q":"café ✓ 日本 \U0001f600"}')
    assert d(value) != _sha('{"q":"caf\\u00e9 \\u2713 \\u65e5\\u672c \\ud83d\\ude00"}')
    assert d(value) == ref_args_digest(value)


@pytest.mark.fr("FR-015", "FR-016")
def test_none_is_empty_object():
    d = _digest()
    assert d(None) == d({}) == _sha("{}")


@pytest.mark.fr("FR-015", "FR-016")
def test_json_string_is_parsed():
    d = _digest()
    assert d('{"b": 2, "a": 1}') == d({"a": 1, "b": 2})
    assert d('  {"q":"x"}  ') == d({"q": "x"})
    assert d('"x"') == _sha('"x"')
    assert d("[1, 2]") == _sha("[1,2]")
    assert d("null") == _sha("null")  # a parsed null is not the None -> {} rule
    assert d("null") != d(None)
    assert d("{}") == d(None)
    assert d("3") == _sha("3")


@pytest.mark.fr("FR-015", "FR-016")
@pytest.mark.parametrize("text", ["not json{", "", "{'a': 1}", "{\"a\": }", "ünï"])
def test_unparsable_string_is_a_json_string_value(text):
    d = _digest()
    import json

    assert d(text) == _sha(json.dumps(text, ensure_ascii=False))
    assert d(text) == ref_args_digest(text)


@pytest.mark.fr("FR-015", "FR-016")
@pytest.mark.parametrize("value", [
    {"x": float("nan")}, {"x": float("inf")}, {"x": float("-inf")}, [float("nan")],
    "NaN", '{"x": NaN}', "Infinity", {"s": {1, 2}}, {"b": b"bytes"}, {"o": object()},
])
def test_unserialisable_gives_none(value):
    assert _digest()(value) is None


@pytest.mark.fr("FR-015", "FR-016", "FR-019")
def test_circular_gives_none_and_does_not_raise():
    a: dict[str, Any] = {}
    a["self"] = a
    lst: list[Any] = []
    lst.append(lst)
    assert _digest()(a) is None
    assert _digest()({"l": lst}) is None


@pytest.mark.fr("FR-015", "FR-016")
def test_no_numeric_normalisation():
    d = _digest()
    assert d({"n": 1}) != d({"n": 1.0})
    assert d({"n": 1}) == _sha('{"n":1}') and d({"n": 1.0}) == _sha('{"n":1.0}')
    assert d({"n": True}) != d({"n": 1})


@pytest.mark.fr("FR-014", "FR-015", "FR-016")
@pytest.mark.parametrize("value", [
    {}, {"a": 1}, {"path": "x", "n": [1, 2.5, None, True]}, "plain", '{"k": "v"}', None, [1],
    {"deep": {"er": {"est": "ü"}}}, 0, "",
])
def test_matches_reference_and_is_lower_hex(value):
    out = _digest()(value)
    assert out == ref_args_digest(value)
    assert isinstance(out, str) and HEX64.fullmatch(out)


# -- FR-014 / FR-017 / FR-016: on every tool.call kind ----------------------------------------


class _Out(BaseModel):
    n: int


def _typed(q: str) -> dict:
    return {"n": "not-an-int"}


def boom_h006(q: str) -> str:
    raise RuntimeError("boom-h006")


def echo_h006(q: str) -> str:
    return f"echo {q}"


ACTIONS = (FunctionTool(_typed, name="typed_h006", output_model=_Out), boom_h006, echo_h006)


def _run(kmaker, script, **kw):
    fake = FakeProvider(script, default="fallback")
    kernel = kmaker(fake)
    agent = kernel.agent(AgentSpec(name=kw.pop("name", "k3"), actions=ACTIONS, **kw))
    return agent.run_sync("go"), fake


@pytest.mark.fr("FR-014", "FR-016", "FR-017")
def test_every_tool_call_kind_carries_the_digest(kmaker, sink, tmp_path):
    (tmp_path / "work").mkdir(exist_ok=True)
    args = {
        "ok1": ("list_dir", {"path": "."}),
        "err1": ("read_file", {"path": "missing-h006.txt"}),
        "inv1": ("typed_h006", {"q": "a"}),
        "exc1": ("boom_h006", {"q": "b"}),
        "unk1": ("no_such_tool_h006", {"z": 1, "a": 2}),
        "val1": ("read_file", {"path": "x.txt", "offset": 0}),
        "echo1": ("echo_h006", {"q": "é"}),
    }
    script = [calls(*[(name, a, cid) for cid, (name, a) in args.items()]), "done"]
    result, _ = _run(kmaker, script)
    assert result.stop_reason == "completed"
    events = {e["call_id"]: e for e in ev(sink, "tool.call")}
    assert set(events) == set(args)
    kinds = {cid: (e["ok"], e["error_kind"]) for cid, e in events.items()}
    assert kinds["ok1"] == (True, None) and kinds["echo1"] == (True, None)
    assert kinds["err1"] == (False, "tool_error")
    assert kinds["inv1"] == (False, "result_invalid")
    assert kinds["exc1"] == (False, "RuntimeError")
    assert kinds["unk1"] == (False, "invalid_args") and kinds["val1"] == (False, "invalid_args")
    d = _digest()
    for cid, (_name, a) in args.items():
        assert events[cid]["args_digest"] == d(a) == ref_args_digest(a), cid
        assert HEX64.fullmatch(events[cid]["args_digest"]), cid


@pytest.mark.fr("FR-015", "FR-016", "FR-019")
def test_edge_case_arguments_in_a_run(kmaker, sink):
    args = {
        "none1": None,
        "str1": '{"q": "x"}',
        "bad1": "not json{",
        "nan1": {"x": float("nan")},
        "inf1": {"x": float("inf")},
        "uni1": {"q": "日本"},
    }
    script = [calls(*[("no_such_tool_h006", a, cid) for cid, a in args.items()]), "done"]
    result, fake = _run(kmaker, script)
    assert result.stop_reason == "completed" and len(fake.calls) == 2
    events = {e["call_id"]: e for e in ev(sink, "tool.call")}
    assert set(events) == set(args)
    assert events["none1"]["args_digest"] == _sha("{}")
    assert events["str1"]["args_digest"] == _sha('{"q":"x"}')
    assert events["bad1"]["args_digest"] == _sha('"not json{"')
    assert events["nan1"]["args_digest"] is None
    assert events["inf1"]["args_digest"] is None
    assert events["uni1"]["args_digest"] == _sha('{"q":"日本"}')


@pytest.mark.fr("SC-003", "FR-016")
def test_sc003_two_distinct_digests(kmaker, sink):
    """US2 scenario 2 and SC-003."""
    script = [
        tc("echo_h006", {"q": "x"}, "s1"),
        tc("echo_h006", '{"q": "x"}', "s2"),
        calls(("echo_h006", {"q": "x"}, "s3"), ("echo_h006", {"q": "y"}, "s4")),
        tc("echo_h006", {"a": 1, "b": 2}, "s5"),
        tc("echo_h006", {"b": 2, "a": 1}, "s6"),
        "done",
    ]
    result, _ = _run(kmaker, script)
    events = {e["call_id"]: e["args_digest"] for e in ev(sink, "tool.call")}
    assert len({events[c] for c in ("s1", "s2", "s3", "s4")}) == 2
    assert events["s1"] == events["s2"] == events["s3"] != events["s4"]
    assert events["s5"] == events["s6"] == _digest()({"a": 1, "b": 2})


@pytest.mark.fr("FR-018")
def test_no_raw_arguments_and_only_one_new_key(kmaker, sink):
    marker = "RAW-ARG-MARKER-h006-7f3a"
    script = [calls(("echo_h006", {"q": marker}, "r1"), ("no_such_tool_h006", {"q": marker}, "r2"),
                    ("boom_h006", {"q": marker + "x"}, "r3")), "done"]
    _run(kmaker, script)
    tool_calls = ev(sink, "tool.call")
    assert len(tool_calls) == 3
    for e in tool_calls:
        assert set(strip(e)) == TOOL_CALL_KEYS_BEFORE | {"args_digest"}
        for key, value in strip(e).items():
            if key == "error":
                continue  # error text is the tool's own output, not the arguments field
            assert marker not in repr(value), key
        assert "arguments" not in e and "args" not in e


@pytest.mark.fr("FR-019", "FR-014")
def test_digest_failure_does_not_change_outcome(kmaker, sink):
    script = [calls(("echo_h006", {"q": float("nan")}, "n1"), ("echo_h006", {"q": "fine"}, "n2")), "done"]
    result, fake = _run(kmaker, script)
    assert result.stop_reason == "completed" and result.content == "done"
    events = {e["call_id"]: e for e in ev(sink, "tool.call")}
    assert events["n1"]["args_digest"] is None
    assert events["n2"]["args_digest"] == _sha('{"q":"fine"}')
    assert len(ev(sink, "run.completed")) == 1


@pytest.mark.fr("FR-040")
def test_events_catalogue_mentions_args_digest():
    from moeka.trace import EVENTS

    assert "args_digest" in EVENTS["tool.call"]
