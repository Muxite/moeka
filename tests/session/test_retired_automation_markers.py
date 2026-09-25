"""History written by the removed cron scheduler stays hidden after its removal."""

from nanobot.session.automation_turns import (
    AUTOMATION_HISTORY_META,
    automation_history_overrides,
    is_automation_history_message,
)
from nanobot.session.history_visibility import is_hidden_history_message


def test_legacy_cron_marker_still_hides_history_record():
    message = {"role": "user", "content": "Cron job: daily check", "_cron_turn": True}

    assert is_automation_history_message(message)
    assert is_hidden_history_message(message)


def test_current_cron_automation_marker_still_hides_history_record():
    message = {
        "role": "user",
        "content": "Scheduled cron job triggered",
        AUTOMATION_HISTORY_META: {"kind": "cron", "cron_job_id": "job-1"},
    }

    assert is_automation_history_message(message)


def test_cron_trigger_metadata_is_no_longer_an_automation_turn():
    text, extra = automation_history_overrides({"_cron_trigger": {"job_id": "job-1"}})

    assert text is None
    assert extra == {}


def test_retired_cron_kind_is_still_an_automation_kind_for_old_history():
    from nanobot.session.automation_turns import is_automation_kind

    assert is_automation_kind("cron")
    assert is_automation_kind("trigger")
    assert not is_automation_kind("user")
