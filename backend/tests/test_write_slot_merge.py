"""Tests for Task 7 — server-declared editable slots + server-side merge on approve.

Security contract under test: the client may submit values only for field
names the SERVER declared editable (``editable_slots``), and only values
inside a slot's ``allowed`` set when one exists. Everything else is
rejected. ``merge_slot_values`` is the single choke point that enforces
this before a merged payload is ever handed to ``execute_tool_call`` — see
``backend/app/services/chat/write_confirmation_service.py``.

The first block below (``merge_slot_values`` unit tests) is the failing
test set specified in the Task 7 brief, verbatim. The second block
(``TestOrchestratorApproveWithSlotValues``) exercises the *actual*
orchestrator approve branch — not just the helper in isolation — because
unit suites in this area have previously passed against broken
integration behaviour (see task-7-brief.md care points).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.chat import ChatMessage, ChatSession
from app.services.chat.mutation_guard import generate_confirmation_token
from app.services.chat.write_confirmation_service import (
    _build_payload_json,
    merge_slot_values,
    validate_and_extract_confirmation,
)

SESSION = "11111111-1111-1111-1111-111111111111"
TOOL = "ext__" + "a" * 32 + "__ns_createRecord"


def _pending(slots):
    tool_input = {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
    return {
        "type": "write_confirmation",
        "mutation_type": "create",
        "record_type": "customer",
        "tool_name": TOOL,
        "tool_input": tool_input,
        "editable_slots": slots,
        "confirmation_token": generate_confirmation_token(SESSION, _build_payload_json(TOOL, tool_input)),
        "status": "pending",
    }


SLOTS = [
    {
        "name": "subsidiary",
        "label": "Primary Subsidiary",
        "type": "select",
        "allowed": [{"value": "1", "label": "Framework Inc"}, {"value": "2", "label": "Framework EU"}],
    }
]


def test_declared_slot_value_is_merged():
    ok, tool_name, merged, err = merge_slot_values(_pending(SLOTS), {"subsidiary": "2"}, SESSION)
    assert ok is True
    assert err == ""
    import json

    assert json.loads(merged["data"])["subsidiary"] == "2"
    assert json.loads(merged["data"])["companyname"] == "test ai customer"


def test_undeclared_field_is_rejected():
    ok, _, _, err = merge_slot_values(_pending(SLOTS), {"companyname": "evil"}, SESSION)
    assert ok is False
    assert "not editable" in err


def test_value_outside_allowlist_is_rejected():
    ok, _, _, err = merge_slot_values(_pending(SLOTS), {"subsidiary": "99"}, SESSION)
    assert ok is False
    assert "not an allowed value" in err


def test_merged_payload_gets_a_fresh_token_and_old_one_dies():
    pending = _pending(SLOTS)
    old_token = pending["confirmation_token"]
    ok, tool_name, merged, _ = merge_slot_values(pending, {"subsidiary": "2"}, SESSION)
    assert ok is True
    stale = {"confirmation_token": old_token, "tool_name": tool_name, "tool_input": merged}
    assert validate_and_extract_confirmation(stale, SESSION)[0] is False


def test_no_slots_and_no_values_is_a_passthrough():
    ok, _, merged, err = merge_slot_values(_pending([]), {}, SESSION)
    assert ok is True and err == ""


# ---------------------------------------------------------------------------
# Orchestrator integration — the approve branch, not the helper in isolation.
#
# `_confirm_msg` must be a REAL (transient, never-flushed) ChatMessage
# instance rather than a MagicMock(spec=...): the approve branch calls
# `sqlalchemy.orm.attributes.flag_modified(_confirm_msg, "structured_output")`,
# which reads `_sa_instance_state` — an attribute SQLAlchemy's instrumentation
# adds at object construction, not one `spec=ChatMessage` (built from `dir()`)
# knows about. A MagicMock there raises AttributeError, so any test that
# mocked ChatMessage here would never actually reach the merge logic.
# ---------------------------------------------------------------------------


_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()


class _FakeResult:
    def __init__(self, msg):
        self._msg = msg

    def scalar_one_or_none(self):
        return self._msg


def _make_confirm_message(structured_output: dict) -> ChatMessage:
    msg = ChatMessage(
        tenant_id=_TENANT_ID,
        session_id=uuid.UUID(SESSION),
        role="assistant",
        content="",
        structured_output=structured_output,
        created_at=datetime.now(timezone.utc),
    )
    msg.id = uuid.uuid4()
    return msg


def _make_db(confirm_msg: ChatMessage) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(return_value=_FakeResult(confirm_msg))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


def _make_session() -> MagicMock:
    session = MagicMock(spec=ChatSession)
    session.id = uuid.UUID(SESSION)
    return session


class TestOrchestratorApproveWithSlotValues:
    @pytest.mark.asyncio
    async def test_approve_merges_declared_slot_before_executing(self):
        from app.services.chat.orchestrator import run_chat_turn

        confirm_msg = _make_confirm_message(_pending(SLOTS))
        db = _make_db(confirm_msg)
        session = _make_session()

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))
        mock_log_event = AsyncMock(return_value=None)

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={
                        "action": "approve",
                        "confirmation_id": str(confirm_msg.id),
                        "slot_values": {"subsidiary": "2"},
                    },
                )
            ]

        assert not [e for e in events if e.get("type") == "error"]
        mock_execute_tool_call.assert_awaited_once()
        sent_tool_input = mock_execute_tool_call.await_args.kwargs["tool_input"]
        merged_fields = json.loads(sent_tool_input["data"])
        assert merged_fields["subsidiary"] == "2"
        assert merged_fields["companyname"] == "test ai customer"

        message_events = [e for e in events if e.get("type") == "message"]
        assert message_events
        assert "Done" in message_events[-1]["message"]["content"]

    @pytest.mark.asyncio
    async def test_approve_rejects_undeclared_slot_before_executing(self):
        """A field the server never declared editable must never reach NetSuite.

        This is the case that matters most: a manipulated browser submitting
        a value for a field it was never offered a slot for.
        """
        from app.services.chat.orchestrator import run_chat_turn

        confirm_msg = _make_confirm_message(_pending(SLOTS))
        db = _make_db(confirm_msg)
        session = _make_session()

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))
        mock_log_event = AsyncMock(return_value=None)

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={
                        "action": "approve",
                        "confirmation_id": str(confirm_msg.id),
                        "slot_values": {"companyname": "evil"},
                    },
                )
            ]

        mock_execute_tool_call.assert_not_awaited()
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "not editable" in error_events[0]["error"]

    @pytest.mark.asyncio
    async def test_approve_rejects_value_outside_allowlist_before_executing(self):
        from app.services.chat.orchestrator import run_chat_turn

        confirm_msg = _make_confirm_message(_pending(SLOTS))
        db = _make_db(confirm_msg)
        session = _make_session()

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))
        mock_log_event = AsyncMock(return_value=None)

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={
                        "action": "approve",
                        "confirmation_id": str(confirm_msg.id),
                        "slot_values": {"subsidiary": "99"},
                    },
                )
            ]

        mock_execute_tool_call.assert_not_awaited()
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "not an allowed value" in error_events[0]["error"]

    @pytest.mark.asyncio
    async def test_approve_with_no_slot_values_still_validates_and_executes(self):
        """Existing (pre-Task-7) approve flow with no slot editing must keep working."""
        from app.services.chat.orchestrator import run_chat_turn

        confirm_msg = _make_confirm_message(_pending([]))
        db = _make_db(confirm_msg)
        session = _make_session()

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))
        mock_log_event = AsyncMock(return_value=None)

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
        ):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={
                        "action": "approve",
                        "confirmation_id": str(confirm_msg.id),
                    },
                )
            ]

        assert not [e for e in events if e.get("type") == "error"]
        mock_execute_tool_call.assert_awaited_once()
        sent_tool_input = mock_execute_tool_call.await_args.kwargs["tool_input"]
        assert json.loads(sent_tool_input["data"])["companyname"] == "test ai customer"
