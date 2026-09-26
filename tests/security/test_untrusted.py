from __future__ import annotations

import copy

from nanobot.security.untrusted import (
    UNTRUSTED_BANNER,
    mark_untrusted,
    sanitize_description,
    sanitize_schema_descriptions,
)


def test_banner_is_shared_with_web_module():
    from nanobot.agent.tools import web

    assert web._UNTRUSTED_BANNER is UNTRUSTED_BANNER


def test_mark_untrusted_prefixes_banner():
    assert mark_untrusted("hi") == f"{UNTRUSTED_BANNER}\n\nhi"


def test_mark_untrusted_is_idempotent():
    once = mark_untrusted("hi")
    assert mark_untrusted(once) == once


def test_sanitize_description_caps_with_ellipsis():
    out = sanitize_description("a" * 2500)
    assert out == "a" * 2000 + "…"
    assert sanitize_description("a" * 2000) == "a" * 2000
    assert sanitize_description("abcdef", limit=3) == "abc…"


def test_sanitize_description_strips_control_chars_keeps_newline_tab():
    assert sanitize_description("a\x00b\x07c\x1b[31md\x7fe\x85f\n\tg") == "abc[31mdef\n\tg"


def test_sanitize_schema_descriptions_copies_and_only_touches_descriptions():
    schema = {
        "type": "object",
        "properties": {
            "x": {
                "type": "string",
                "description": "bad\x00" + "z" * 3000,
                "enum": ["a\x00b"],
                "default": "d\x00",
            },
            "description": {"type": "string"},
        },
        "anyOf": [{"description": "q\x01"}],
    }
    original = copy.deepcopy(schema)
    out = sanitize_schema_descriptions(schema)
    assert schema == original
    x = out["properties"]["x"]
    assert x["description"] == "bad" + "z" * 1997 + "…"
    assert x["enum"] == ["a\x00b"] and x["default"] == "d\x00"
    assert out["properties"]["description"] == {"type": "string"}
    assert out["anyOf"][0]["description"] == "q"
    assert out is not schema and out["properties"] is not schema["properties"]
