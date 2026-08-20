"""Tests for write_confirm flow through chat.py and orchestrator.py.

Task 4: Handle confirmation_required events in the orchestrator's streaming loop,
add write_confirm parameter to SendMessageRequest, and handle write_confirm at
the start of run_chat_turn.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.chat import ChatMessage, ChatSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEX_32 = "a1b2c3d4e5f67890a1b2c3d4e5f67890"
_SESSION_ID = str(uuid.uuid4())
_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()


def _make_session(session_id=None, messages=None, session_type="chat"):
    session = MagicMock(spec=ChatSession)
    session.id = uuid.UUID(session_id) if session_id else uuid.uuid4()
    session.tenant_id = _TENANT_ID
    session.session_type = session_type
    session.source_pin = None
    session.workspace_id = None
    session.agent_id = None
    session.messages = messages or []
    session.title = "Test Session"
    return session


def _make_confirmation_msg(session_id, tool_name, tool_input, status="pending"):
    """Build a mock assistant ChatMessage with write_confirmation structured_output."""
    from app.services.chat.write_confirmation_service import build_confirmation_payload

    payload = build_confirmation_payload(
        mutation_type="create",
        record_type="salesOrder",
        tool_name=tool_name,
        tool_input=tool_input,
        session_id=str(session_id),
    )
    assert payload is not None

    msg = MagicMock(spec=ChatMessage)
    msg.id = uuid.uuid4()
    msg.role = "assistant"
    msg.content = ""
    msg.structured_output = {**payload.model_dump(), "status": status}
    msg.tenant_id = _TENANT_ID
    msg.session_id = session_id
    msg.created_at = datetime.now(timezone.utc)
    return msg


def _ext(tool_name: str) -> str:
    return f"ext__{_HEX_32}__{tool_name}"


def _make_real_confirmation_msg(session_id, tool_name, tool_input, status="pending"):
    """Like ``_make_confirmation_msg`` but a REAL ``ChatMessage`` instance.

    Tests that actually drive ``run_chat_turn``'s approve branch need this —
    it calls ``sqlalchemy.orm.attributes.flag_modified(_confirm_msg,
    "structured_output")``, which reads ``_sa_instance_state``, an attribute
    only SQLAlchemy-instrumented instances have. ``MagicMock(spec=ChatMessage)``
    (what ``_make_confirmation_msg`` above returns) raises ``AttributeError``
    there — see the same note in ``test_write_slot_merge.py``.
    """
    from app.services.chat.write_confirmation_service import build_confirmation_payload

    payload = build_confirmation_payload(
        mutation_type="create",
        record_type="salesOrder",
        tool_name=tool_name,
        tool_input=tool_input,
        session_id=str(session_id),
    )
    assert payload is not None

    msg = ChatMessage(
        tenant_id=_TENANT_ID,
        session_id=session_id,
        role="assistant",
        content="",
        structured_output={**payload.model_dump(), "status": status},
        created_at=datetime.now(timezone.utc),
    )
    msg.id = uuid.uuid4()
    return msg


class _FakeScalarResult:
    def __init__(self, msg):
        self._msg = msg

    def scalar_one_or_none(self):
        return self._msg


def _make_db(confirm_msg):
    db = MagicMock()
    db.execute = AsyncMock(return_value=_FakeScalarResult(confirm_msg))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


# ---------------------------------------------------------------------------
# A) SendMessageRequest schema tests
# ---------------------------------------------------------------------------


class TestSendMessageRequestWriteConfirm:
    """write_confirm field on SendMessageRequest."""

    def test_write_confirm_defaults_to_none(self):
        from app.api.v1.chat import SendMessageRequest

        req = SendMessageRequest(content="hello")
        assert req.write_confirm is None

    def test_write_confirm_accepts_approve(self):
        from app.api.v1.chat import SendMessageRequest

        req = SendMessageRequest(
            content="approve",
            write_confirm={"action": "approve", "confirmation_id": str(uuid.uuid4())},
        )
        assert req.write_confirm is not None
        assert req.write_confirm["action"] == "approve"

    def test_write_confirm_accepts_reject(self):
        from app.api.v1.chat import SendMessageRequest

        req = SendMessageRequest(
            content="reject",
            write_confirm={"action": "reject", "confirmation_id": str(uuid.uuid4())},
        )
        assert req.write_confirm is not None
        assert req.write_confirm["action"] == "reject"

    def test_write_confirm_accepts_dict(self):
        from app.api.v1.chat import SendMessageRequest

        confirm_data = {
            "action": "approve",
            "confirmation_id": "msg-123",
        }
        req = SendMessageRequest(content="go ahead", write_confirm=confirm_data)
        assert req.write_confirm == confirm_data


# ---------------------------------------------------------------------------
# B) chat.py: write_confirm reuses last user message (like source_pick)
# ---------------------------------------------------------------------------


class TestChatWriteConfirmReusesMessage:
    """When write_confirm is set, send_message() should reuse the last user
    message instead of creating a duplicate — exactly like source_pick."""

    def test_write_confirm_field_exists_on_schema(self):
        from app.api.v1.chat import SendMessageRequest

        schema = SendMessageRequest.model_json_schema()
        assert "write_confirm" in schema["properties"]


# ---------------------------------------------------------------------------
# C) run_chat_turn signature accepts write_confirm
# ---------------------------------------------------------------------------


class TestRunChatTurnSignature:
    """run_chat_turn() must accept a write_confirm kwarg."""

    def test_run_chat_turn_has_write_confirm_param(self):
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        sig = inspect.signature(run_chat_turn)
        assert "write_confirm" in sig.parameters
        # Default should be None
        param = sig.parameters["write_confirm"]
        assert param.default is None


# ---------------------------------------------------------------------------
# D) Orchestrator: confirmation_required event → last_structured_output
# ---------------------------------------------------------------------------


class TestConfirmationRequiredEvent:
    """When the unified agent yields a confirmation_required event, the
    orchestrator should set last_structured_output with the payload."""

    def test_orchestrator_source_has_confirmation_required_handler(self):
        """The orchestrator streaming loop handles 'confirmation_required' events."""
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)
        assert 'event_type == "confirmation_required"' in source

    def test_confirmation_required_sets_structured_output(self):
        """The handler should set last_structured_output with type=write_confirmation."""
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)
        # Check the handler sets structured_output correctly
        assert '"type": "write_confirmation"' in source


# ---------------------------------------------------------------------------
# E) Orchestrator: write_confirm approve flow
# ---------------------------------------------------------------------------


class TestWriteConfirmApproveFlow:
    """When write_confirm with action='approve' is passed, the orchestrator
    should validate the HMAC, execute the tool, update status, and return."""

    def test_orchestrator_has_write_confirm_handler(self):
        """The orchestrator handles write_confirm at the top of run_chat_turn."""
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)
        assert "write_confirm" in source
        assert '"approve"' in source

    def test_orchestrator_validates_hmac(self):
        """The approve flow calls validate_and_extract_confirmation."""
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)
        assert "validate_and_extract_confirmation" in source

    def test_orchestrator_calls_execute_tool_call(self):
        """The approve flow calls execute_tool_call with the original tool params."""
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)
        # Must call execute_tool_call somewhere in the write_confirm block
        assert "execute_tool_call" in source

    def test_orchestrator_updates_status_to_approved(self):
        """The approve flow updates structured_output.status to 'approved'."""
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)
        assert '"approved"' in source

    def test_orchestrator_audits_approved_write(self):
        """The approve flow logs an audit event."""
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)
        # Check for audit log in the write_confirm block
        assert "log_event" in source


# ---------------------------------------------------------------------------
# F) Orchestrator: write_confirm reject flow
# ---------------------------------------------------------------------------


class TestWriteConfirmRejectFlow:
    """When write_confirm with action='reject' is passed, the orchestrator
    should update status to 'rejected' and return a message."""

    def test_orchestrator_handles_reject(self):
        """The orchestrator has a reject branch."""
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)
        assert '"reject"' in source
        assert '"rejected"' in source

    def test_reject_creates_no_changes_message(self):
        """The reject flow sends a 'no changes were made' type message."""
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)
        # The reject path should mention no changes
        assert "No changes were made" in source or "no changes" in source.lower()


# ---------------------------------------------------------------------------
# G) _run_chat_pipeline and _run_chat_background pass write_confirm
# ---------------------------------------------------------------------------


class TestChatPipelinePassthrough:
    """write_confirm must be threaded through the call chain."""

    def test_run_chat_pipeline_has_write_confirm_param(self):
        import inspect

        from app.api.v1.chat import _run_chat_pipeline

        sig = inspect.signature(_run_chat_pipeline)
        assert "write_confirm" in sig.parameters

    def test_run_chat_background_has_write_confirm_param(self):
        import inspect

        from app.api.v1.chat import _run_chat_background

        sig = inspect.signature(_run_chat_background)
        assert "write_confirm" in sig.parameters


# ---------------------------------------------------------------------------
# H) Orchestrator: a failed write becomes TERMINAL, never reverts to pending
#
# Task 8 (fixes 86bbgnw9g): before this, a NetSuite write that failed after
# approval flipped structured_output.status back to "pending" — forever.
# There was no terminal state and no error surfaced, so the card looked like
# it was still hanging rather than telling the human it failed. These tests
# drive the REAL run_chat_turn approve branch (not source inspection like
# the classes above) so they'd actually catch a regression to "pending".
# ---------------------------------------------------------------------------


class TestWriteConfirmFailedFlow:
    @pytest.mark.asyncio
    async def test_failed_write_gets_terminal_status_and_error(self):
        """Regression for 86bbgnw9g: a failed write must never revert to
        'pending' — it must become 'failed' and carry the NetSuite error."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
        confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input)
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        mock_execute_tool_call = AsyncMock(
            return_value=json.dumps({"error": "HTTP 400: Please enter value(s) for: Primary Subsidiary."})
        )
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
                    write_confirm={"action": "approve", "confirmation_id": str(confirm_msg.id)},
                )
            ]

        so = confirm_msg.structured_output
        assert so["status"] == "failed"
        assert "Primary Subsidiary" in so["error"]

        message_events = [e for e in events if e.get("type") == "message"]
        assert message_events
        assert "failed" in message_events[-1]["message"]["content"].lower()

    @pytest.mark.asyncio
    async def test_failed_card_cannot_be_re_approved(self):
        """Terminal means terminal — the existing pending-only gate must
        refuse a second approve against an already-'failed' card. (Not a new
        behavior — the gate at the top of the write_confirm block already
        checks status == "pending" unconditionally; this proves the new
        'failed' status is covered by it, same as 'approved'/'rejected'.)"""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
        confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input, status="failed")
        confirm_msg.structured_output = {**confirm_msg.structured_output, "error": "boom"}
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        mock_execute_tool_call = AsyncMock()

        with patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call):
            events = [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message="approve",
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={"action": "approve", "confirmation_id": str(confirm_msg.id)},
                )
            ]

        mock_execute_tool_call.assert_not_awaited()
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "not in a pending state" in error_events[0]["error"].lower()

    @pytest.mark.asyncio
    async def test_failed_write_after_slot_merge_still_persists_merged_payload(self):
        """Task 7's merge-then-persist block must keep running on the failure
        path too: the card should show what was actually attempted (the
        merged payload + re-minted token), not the agent's pre-merge guess,
        even when NetSuite rejects the write. This is the block Task 8 was
        told NOT to touch — this test guards against accidentally breaking it
        while wiring up the terminal status right above it."""
        from app.services.chat.mutation_guard import generate_confirmation_token
        from app.services.chat.orchestrator import run_chat_turn
        from app.services.chat.write_confirmation_service import _build_payload_json

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
        slots = [
            {
                "name": "subsidiary",
                "label": "Primary Subsidiary",
                "type": "select",
                "allowed": [{"value": "1", "label": "Framework Inc"}, {"value": "2", "label": "Framework EU"}],
            }
        ]
        structured_output = {
            "type": "write_confirmation",
            "mutation_type": "create",
            "record_type": "customer",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "editable_slots": slots,
            "proposed_fields": {"companyname": "test ai customer"},
            "proposed_lines": [],
            "confirmation_token": generate_confirmation_token(
                str(session_id), _build_payload_json(tool_name, tool_input, slots)
            ),
            "status": "pending",
        }
        confirm_msg = ChatMessage(
            tenant_id=_TENANT_ID,
            session_id=session_id,
            role="assistant",
            content="",
            structured_output=structured_output,
            created_at=datetime.now(timezone.utc),
        )
        confirm_msg.id = uuid.uuid4()
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"error": "Invalid field value."}))
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
        so = confirm_msg.structured_output
        assert so["status"] == "failed"
        assert so["error"] == "Invalid field value."
        merged_data = json.loads(so["tool_input"]["data"])
        assert merged_data["subsidiary"] == "2"
        assert so["proposed_fields"]["subsidiary"] == "2"
