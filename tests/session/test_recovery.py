"""Durable follow-up journal kept in session metadata across restarts."""

from __future__ import annotations

from pathlib import Path

from nanobot.bus.events import InboundMessage
from nanobot.session.manager import SessionManager
from nanobot.session.recovery import (
    PENDING_FOLLOWUPS_KEY,
    acknowledge_pending_followups,
    pending_followups,
    record_pending_followup,
)


def test_live_turn_followup_survives_restart_until_it_is_committed(tmp_path: Path) -> None:
    """A message injected mid-turn is not lost between checkpoints."""
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:chat")
    followup_id = record_pending_followup(
        session,
        InboundMessage(
            channel="websocket",
            sender_id="user",
            chat_id="chat",
            content="also check the logs",
            metadata={"webui": True},
        ),
    )
    assert followup_id is not None
    sessions.save(session)

    restored = SessionManager(tmp_path).get_or_create("websocket:chat")
    queued = pending_followups(restored)
    assert [message.content for message in queued] == ["also check the logs"]
    assert queued[0].metadata["_recovery_followup_id"] == followup_id

    acknowledge_pending_followups(restored, [followup_id])
    assert PENDING_FOLLOWUPS_KEY not in restored.metadata


def test_followup_journal_keeps_every_uncommitted_message(tmp_path: Path) -> None:
    """A live queue limit must never truncate durable WebUI follow-ups."""
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:chat")
    followup_ids = [
        record_pending_followup(
            session,
            InboundMessage(
                channel="websocket",
                sender_id="user",
                chat_id="chat",
                content=f"follow-up-{index}",
                metadata={"webui": True},
            ),
        )
        for index in range(21)
    ]

    assert all(followup_ids)
    sessions.save(session)
    restarted = SessionManager(tmp_path)
    restored = restarted.get_or_create("websocket:chat")
    assert [message.content for message in pending_followups(restored)] == [
        f"follow-up-{index}" for index in range(21)
    ]


def test_requeued_followup_preserves_its_journal_id(tmp_path: Path) -> None:
    """Routing a recovered follow-up into a live turn must remain idempotent."""
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:chat")
    original_id = record_pending_followup(
        session,
        InboundMessage(
            channel="websocket",
            sender_id="user",
            chat_id="chat",
            content="also check the logs",
            metadata={"webui": True},
        ),
    )
    assert original_id is not None

    recovered = pending_followups(session)[0]
    assert record_pending_followup(session, recovered) == original_id
    assert [record["id"] for record in session.metadata[PENDING_FOLLOWUPS_KEY]] == [
        original_id
    ]
