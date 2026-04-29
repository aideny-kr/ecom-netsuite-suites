"""Tests for ChatDisclosureEvent model — HITL telemetry events."""

import uuid

from app.models.chat_disclosure_event import ChatDisclosureEvent


def test_chat_disclosure_event_construction():
    event = ChatDisclosureEvent(
        tenant_id=uuid.uuid4(),
        chat_session_id=uuid.uuid4(),
        chat_message_id=uuid.uuid4(),
        event_type="clarification_pending",
        payload={"options": [], "default_id": "A"},
    )
    assert event.event_type == "clarification_pending"
    assert event.payload == {"options": [], "default_id": "A"}


def test_chat_disclosure_event_optional_message_id():
    """chat_message_id is nullable for session-scoped events."""
    event = ChatDisclosureEvent(
        tenant_id=uuid.uuid4(),
        chat_session_id=uuid.uuid4(),
        event_type="session_telemetry",
        payload={},
    )
    assert event.chat_message_id is None
