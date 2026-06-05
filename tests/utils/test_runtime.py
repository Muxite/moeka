"""Tests for the pure helpers in nanobot.utils.runtime.

The violation/exec-guard throttles are covered by
test_workspace_violation_throttle.py; this file pins the remaining surface:
blank-result repair, recovery messages, and external-lookup throttling.
"""

from __future__ import annotations

from nanobot.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_finalization_retry_message,
    build_length_recovery_message,
    empty_tool_result_message,
    ensure_nonempty_tool_result,
    external_lookup_signature,
    is_blank_text,
    repeated_external_lookup_error,
)

# ---------------------------------------------------------------------------
# Blank-text / empty-tool-result repair
# ---------------------------------------------------------------------------

def test_is_blank_text():
    assert is_blank_text(None)
    assert is_blank_text("")
    assert is_blank_text("   \n\t ")
    assert not is_blank_text("x")


def test_empty_tool_result_message_names_tool():
    assert "exec" in empty_tool_result_message("exec")


def test_ensure_nonempty_tool_result_replaces_blank():
    out = ensure_nonempty_tool_result("exec", "   ")
    assert isinstance(out, str) and out.strip()
    assert ensure_nonempty_tool_result("exec", None)


def test_ensure_nonempty_tool_result_passes_through_content():
    assert ensure_nonempty_tool_result("exec", "real output") == "real output"
    payload = [{"type": "text", "text": "hi"}]
    assert ensure_nonempty_tool_result("exec", payload) is payload


# ---------------------------------------------------------------------------
# Recovery messages
# ---------------------------------------------------------------------------

def test_finalization_retry_message_is_user_role():
    msg = build_finalization_retry_message()
    assert msg["role"] == "user"
    assert msg["content"].strip()


def test_length_recovery_message_is_user_role():
    msg = build_length_recovery_message()
    assert msg["role"] == "user"
    assert msg["content"].strip()


def test_empty_final_response_message_is_nonempty():
    assert EMPTY_FINAL_RESPONSE_MESSAGE.strip()


# ---------------------------------------------------------------------------
# External-lookup throttling
# ---------------------------------------------------------------------------

def test_lookup_signature_web_fetch_normalizes_case():
    sig = external_lookup_signature("web_fetch", {"url": "https://EXAMPLE.com/X"})
    assert sig == "web_fetch:https://example.com/x"


def test_lookup_signature_web_search_accepts_both_arg_names():
    a = external_lookup_signature("web_search", {"query": "Rust async"})
    b = external_lookup_signature("web_search", {"search_term": "Rust async"})
    assert a == b == "web_search:rust async"


def test_lookup_signature_none_for_other_tools_or_empty_args():
    assert external_lookup_signature("exec", {"command": "ls"}) is None
    assert external_lookup_signature("web_fetch", {"url": "  "}) is None
    assert external_lookup_signature("web_search", {}) is None


def test_repeated_lookup_blocks_after_budget():
    counts: dict[str, int] = {}
    args = {"url": "https://example.com"}
    assert repeated_external_lookup_error("web_fetch", args, counts) is None
    assert repeated_external_lookup_error("web_fetch", args, counts) is None
    third = repeated_external_lookup_error("web_fetch", args, counts)
    assert third is not None and "repeated external lookup" in third.lower()


def test_repeated_lookup_budgets_are_per_target():
    counts: dict[str, int] = {}
    for _ in range(2):
        repeated_external_lookup_error("web_fetch", {"url": "https://a.com"}, counts)
    # A different URL starts fresh.
    assert repeated_external_lookup_error("web_fetch", {"url": "https://b.com"}, counts) is None


def test_repeated_lookup_ignores_unthrottled_tools():
    counts: dict[str, int] = {}
    for _ in range(5):
        assert repeated_external_lookup_error("exec", {"command": "ls"}, counts) is None
    assert counts == {}
