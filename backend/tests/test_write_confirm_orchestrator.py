"""Tests for write_confirm flow through chat.py and orchestrator.py.

Task 4: Handle confirmation_required events in the orchestrator's streaming loop,
add write_confirm parameter to SendMessageRequest, and handle write_confirm at
the start of run_chat_turn.
"""

from __future__ import annotations

import asyncio
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


def _make_terminal_confirmation_msg(
    session_id,
    tool_name,
    tool_input,
    *,
    invariant_errors=None,
    unfillable_line_fields=None,
    status="pending",
):
    """Like ``_make_real_confirmation_msg`` but carries a populated
    ``ValidationResult`` so the card's ``invariant_errors`` /
    ``unfillable_line_fields`` are set — a REAL ``ChatMessage`` instance
    (needed for ``flag_modified`` in the orchestrator's approve branch, same
    as ``_make_real_confirmation_msg``)."""
    from app.services.chat.write_confirmation_service import build_confirmation_payload
    from app.services.chat.write_validator import ValidationResult

    validation = ValidationResult(
        ok=not (invariant_errors or unfillable_line_fields),
        missing_line_required=list(unfillable_line_fields or []),
        invariant_errors=list(invariant_errors or []),
    )
    payload = build_confirmation_payload(
        mutation_type="create",
        record_type="salesOrder",
        tool_name=tool_name,
        tool_input=tool_input,
        session_id=str(session_id),
        validation=validation,
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


def _make_db(confirm_msg, cas_rowcount: int = 1):
    """Build a mock AsyncSession.

    ``db.execute`` branches on statement shape: the approve path's initial
    SELECT (by confirmation id) returns ``confirm_msg``; the atomic
    pending->executing CAS claim (an UPDATE) returns ``cas_rowcount`` —
    defaults to 1 (claim succeeds) so every single-approve test using this
    helper doesn't need to know the CAS exists. Pass ``cas_rowcount=0`` to
    simulate "another request already claimed it". A raw ``text()`` call
    (``set_tenant_context``, re-established right after the CAS commit)
    falls through to the SELECT-shaped branch — its return value is
    discarded by the caller, so any harmless object works there too.
    """
    from sqlalchemy import Update

    db = MagicMock()

    async def _execute(stmt, *args, **kwargs):
        if isinstance(stmt, Update):
            return MagicMock(rowcount=cas_rowcount)
        return _FakeScalarResult(confirm_msg)

    db.execute = _execute
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
    async def test_failed_card_cannot_be_re_rejected(self):
        """Mirror of test_failed_card_cannot_be_re_approved on the reject
        side — the same status != "pending" gate sits before BOTH branches
        (it's checked once, before the approve/reject dispatch), so reject
        must be refused against an already-'failed' card too, and the
        stored status must stay 'failed' rather than getting silently
        flipped to 'rejected'."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
        confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input, status="failed")
        confirm_msg.structured_output = {**confirm_msg.structured_output, "error": "boom"}
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        events = [
            event
            async for event in run_chat_turn(
                db=db,
                session=session,
                user_message="reject",
                user_id=_USER_ID,
                tenant_id=_TENANT_ID,
                write_confirm={"action": "reject", "confirmation_id": str(confirm_msg.id)},
            )
        ]

        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "not in a pending state" in error_events[0]["error"].lower()
        assert confirm_msg.structured_output["status"] == "failed"

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


# ---------------------------------------------------------------------------
# I) Orchestrator: a terminal card (invariant_errors / unfillable_line_fields)
#    must never reach execute_tool_call, no matter who sends the approve.
#
# Operator ruling: a card carrying a proven posting-invariant violation
# (unbalanced journal entry, closed accounting period) or an unfillable
# line-level required field is TERMINAL. That shipped as "no Approve button"
# on the frontend only — the server accepted the approve anyway. These tests
# drive the REAL run_chat_turn approve branch so a regression that lets the
# write through would actually be caught, not just inferred from the card
# rendering no button.
# ---------------------------------------------------------------------------


class TestWriteConfirmTerminalMarkersBlockApproval:
    @pytest.mark.asyncio
    async def test_invariant_errors_refuses_approve_and_never_executes(self):
        """A card with invariant_errors must be refused; execute_tool_call
        must never be called (the property that matters — not just that an
        error event was yielded)."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "journalEntry", "data": '{"memo": "unbalanced JE"}'}
        confirm_msg = _make_terminal_confirmation_msg(
            session_id,
            tool_name,
            tool_input,
            invariant_errors=["Journal entry lines are not balanced: debit 100.00 != credit 80.00"],
        )
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        # execute_tool_call returns a harmless success shape here — if the
        # gate fails to block the approve, the turn must not blow up on an
        # unrelated mock gap; the assertions below are what prove the block.
        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999", "success": True}))
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

        mock_execute_tool_call.assert_not_called()
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "not balanced" in error_events[0]["error"]
        # The card must not have been silently flipped to "approved".
        assert confirm_msg.structured_output["status"] == "pending"

    @pytest.mark.asyncio
    async def test_unfillable_line_fields_refuses_approve_and_never_executes(self):
        """Mirror of the invariant_errors test for unfillable_line_fields —
        same terminal standing per the operator's ruling."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "salesOrder", "data": '{"entity": "123"}'}
        confirm_msg = _make_terminal_confirmation_msg(
            session_id,
            tool_name,
            tool_input,
            unfillable_line_fields=["line[0].item"],
        )
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999", "success": True}))
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

        mock_execute_tool_call.assert_not_called()
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "line[0].item" in error_events[0]["error"]
        assert confirm_msg.structured_output["status"] == "pending"

    @pytest.mark.asyncio
    async def test_clean_card_still_executes_normally(self):
        """A clean pending card (no invariant_errors, no
        unfillable_line_fields) must still execute exactly as before the
        gate was added — the negative tests alone wouldn't catch a guard
        placed too aggressively."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "clean co"}'}
        confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input)
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999", "success": True}))
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

        mock_execute_tool_call.assert_awaited_once()
        assert not [e for e in events if e.get("type") == "error"]
        assert confirm_msg.structured_output["status"] == "approved"

    @pytest.mark.asyncio
    async def test_clean_card_with_slot_values_still_merges_and_executes(self):
        """Task 7's slot-merge path on a clean card must be untouched by the
        new gate."""
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

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999", "success": True}))
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

        mock_execute_tool_call.assert_awaited_once()
        assert not [e for e in events if e.get("type") == "error"]
        so = confirm_msg.structured_output
        assert so["status"] == "approved"
        merged_data = json.loads(so["tool_input"]["data"])
        assert merged_data["subsidiary"] == "2"
        assert so["proposed_fields"]["subsidiary"] == "2"


# ---------------------------------------------------------------------------
# Finding B — the MERGED, slot-filled payload must be re-validated against
# live invariants before execute_tool_call runs. The pre-merge
# invariant_errors check (7d04b6fe) was computed against the AGENT'S
# pre-merge proposal — exactly why a missing trandate could never trip
# _check_period_open at build time. Only runs when a merge actually
# happened (a no-merge approve executes bytes identical to what the
# intercept already validated).
# ---------------------------------------------------------------------------


def _make_journal_entry_card_with_trandate_slot(session_id):
    """A journalEntry create card whose trandate was ABSENT at build time —
    so _check_period_open honestly returned [] (nothing to check), NOT a
    pre-seeded violation. That's the realistic repair-exhaustion shape this
    finding is about."""
    from app.services.chat.mutation_guard import generate_confirmation_token
    from app.services.chat.write_confirmation_service import _build_payload_json

    tool_name = _ext("ns_createRecord")
    fields = {"memo": "Q3 accrual", "subsidiary": "1"}
    tool_input = {"recordType": "journalEntry", "data": json.dumps(fields)}
    slots = [{"name": "trandate", "label": "Transaction Date", "type": "text", "allowed": None}]
    structured_output = {
        "type": "write_confirmation",
        "mutation_type": "create",
        "record_type": "journalEntry",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "editable_slots": slots,
        "proposed_fields": fields,
        "proposed_lines": [],
        "confirmation_token": generate_confirmation_token(
            str(session_id), _build_payload_json(tool_name, tool_input, slots)
        ),
        "invariant_errors": [],
        "unfillable_line_fields": [],
        "unvalidated": False,
        "status": "pending",
    }
    msg = ChatMessage(
        tenant_id=_TENANT_ID,
        session_id=session_id,
        role="assistant",
        content="",
        structured_output=structured_output,
        created_at=datetime.now(timezone.utc),
    )
    msg.id = uuid.uuid4()
    return msg, tool_name


async def _fake_metadata_none(**kwargs):
    return None


def _fake_invariants_closed_on(closed_date: str):
    async def _fake(*, payload, **kwargs):
        if payload.fields.get("trandate") == closed_date:
            return ["Accounting period is closed — posting is not permitted."]
        return []

    return _fake


def _fake_metadata_requiring_header_field(field_name: str):
    """Record-type metadata that marks a single HEADER field required —
    used to prove the merged-payload re-check still refuses on
    `missing_required` (existing behaviour, protected against regression)."""
    from app.services.chat.record_metadata_service import FieldSpec, RecordMetadata

    async def _fake(**kwargs):
        return RecordMetadata(
            record_type="journalEntry",
            fields=[FieldSpec(name=field_name, label=field_name, required=True)],
            line_fields=[],
        )

    return _fake


def _fake_metadata_requiring_line_field(field_name: str):
    """Record-type metadata that marks a single LINE field required — the
    realistic shape for the gap this wave closes: the metadata cache (1h
    TTL) refetches between card build and approve and a line field becomes
    required only at merge time."""
    from app.services.chat.record_metadata_service import FieldSpec, RecordMetadata

    async def _fake(**kwargs):
        return RecordMetadata(
            record_type="journalEntry",
            fields=[],
            line_fields=[FieldSpec(name=field_name, label=field_name, required=True)],
        )

    return _fake


def _make_journal_entry_card_with_trandate_slot_and_incomplete_line(session_id):
    """Like `_make_journal_entry_card_with_trandate_slot` but carries one
    line item. `unfillable_line_fields: []` mirrors the honest pre-merge
    result at build time — the metadata that will (at merge time) mark a
    line field required hasn't been fetched yet, so the pre-merge gate had
    nothing to flag. This is the realistic staleness-window shape the T2
    finding describes, not a pre-seeded violation."""
    from app.services.chat.mutation_guard import generate_confirmation_token
    from app.services.chat.write_confirmation_service import _build_payload_json

    tool_name = _ext("ns_createRecord")
    fields = {"memo": "Q3 accrual", "subsidiary": "1"}
    lines = [{"debit": "100"}]
    tool_input = {"recordType": "journalEntry", "data": json.dumps({**fields, "line": lines})}
    slots = [{"name": "trandate", "label": "Transaction Date", "type": "text", "allowed": None}]
    structured_output = {
        "type": "write_confirmation",
        "mutation_type": "create",
        "record_type": "journalEntry",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "editable_slots": slots,
        "proposed_fields": fields,
        "proposed_lines": lines,
        "confirmation_token": generate_confirmation_token(
            str(session_id), _build_payload_json(tool_name, tool_input, slots)
        ),
        "invariant_errors": [],
        "unfillable_line_fields": [],
        "unvalidated": False,
        "status": "pending",
    }
    msg = ChatMessage(
        tenant_id=_TENANT_ID,
        session_id=session_id,
        role="assistant",
        content="",
        structured_output=structured_output,
        created_at=datetime.now(timezone.utc),
    )
    msg.id = uuid.uuid4()
    return msg, tool_name


class TestPostMergeReValidation:
    @pytest.mark.asyncio
    async def test_merged_trandate_in_closed_period_is_refused_and_never_executes(self):
        """B1: the proof case — a merged trandate landing in a closed period
        must refuse the approve, even though the pre-merge card was clean
        (because trandate was absent, not because the period was checked and
        passed)."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        confirm_msg, _tool_name = _make_journal_entry_card_with_trandate_slot(session_id)
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", AsyncMock(return_value=None)),
            patch("app.services.chat.write_validation.get_record_metadata", _fake_metadata_none),
            patch(
                "app.services.chat.write_validation.check_posting_invariants", _fake_invariants_closed_on("2026-07-15")
            ),
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
                        "slot_values": {"trandate": "2026-07-15"},
                    },
                )
            ]

        mock_execute_tool_call.assert_not_called()
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "closed" in error_events[0]["error"].lower()
        assert confirm_msg.structured_output["status"] == "pending"

    @pytest.mark.asyncio
    async def test_merged_trandate_in_open_period_executes(self):
        """B2: mirror positive — an open period lets the merged write
        through."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        confirm_msg, _tool_name = _make_journal_entry_card_with_trandate_slot(session_id)
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", AsyncMock(return_value=None)),
            patch("app.services.chat.write_validation.get_record_metadata", _fake_metadata_none),
            patch(
                "app.services.chat.write_validation.check_posting_invariants", _fake_invariants_closed_on("2026-07-15")
            ),
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
                        "slot_values": {"trandate": "2026-01-15"},
                    },
                )
            ]

        assert not [e for e in events if e.get("type") == "error"]
        mock_execute_tool_call.assert_awaited_once()
        sent_tool_input = mock_execute_tool_call.await_args.kwargs["tool_input"]
        assert json.loads(sent_tool_input["data"])["trandate"] == "2026-01-15"
        assert confirm_msg.structured_output["status"] == "approved"

    @pytest.mark.asyncio
    async def test_revalidation_is_invoked_against_the_merged_trandate_specifically(self):
        """B3: proves re-validation runs against the MERGED payload, not
        that a coincidentally stale result happened to refuse."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        confirm_msg, _tool_name = _make_journal_entry_card_with_trandate_slot(session_id)
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        captured_trandates = []

        async def spy_invariants(*, payload, **kwargs):
            captured_trandates.append(payload.fields.get("trandate"))
            return []

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", AsyncMock(return_value=None)),
            patch("app.services.chat.write_validation.get_record_metadata", _fake_metadata_none),
            patch("app.services.chat.write_validation.check_posting_invariants", spy_invariants),
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
                        "slot_values": {"trandate": "2026-03-03"},
                    },
                )
            ]

        assert not [e for e in events if e.get("type") == "error"]
        assert captured_trandates == ["2026-03-03"]

    @pytest.mark.asyncio
    async def test_infra_failure_during_revalidation_fails_open_and_still_executes(self):
        """B4 (fail-open pin): an infra failure inside the REAL
        check_posting_invariants (an MCP outage reaching its own
        execute_tool_call) must not block the write — a human explicitly
        approved this payload, and check_posting_invariants already fails
        open by its own internal contract (never fabricate a violation).
        Pins the deliberate choice so a future 'fail closed on outage'
        change, or a silent 'skip re-validation' regression, both go red."""
        from app.services.chat import posting_invariants as pi
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        confirm_msg, _tool_name = _make_journal_entry_card_with_trandate_slot(session_id)
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        async def outage(**kwargs):
            raise RuntimeError("MCP outage")

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", AsyncMock(return_value=None)),
            patch("app.services.chat.write_validation.get_record_metadata", _fake_metadata_none),
            # check_posting_invariants itself is REAL here — only its own
            # internal execute_tool_call is broken — proving the fail-open
            # behavior comes from the real function's own contract, not from
            # a try/except this wave adds around it.
            patch.object(pi, "execute_tool_call", outage),
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
                        "slot_values": {"trandate": "2026-03-03"},
                    },
                )
            ]

        assert not [e for e in events if e.get("type") == "error"]
        mock_execute_tool_call.assert_awaited_once()
        assert confirm_msg.structured_output["status"] == "approved"

    @pytest.mark.asyncio
    async def test_merged_payload_missing_required_header_field_still_refused(self):
        """Existing behaviour, protected against regression: `missing_required`
        (a header field) must still refuse the merged approve. The pre-merge
        card is clean — trandate is the only slot merged in; a different
        header field (`approvalstatus`) becomes required only once the
        merge-time metadata fetch runs."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        confirm_msg, _tool_name = _make_journal_entry_card_with_trandate_slot(session_id)
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", AsyncMock(return_value=None)),
            patch(
                "app.services.chat.write_validation.get_record_metadata",
                _fake_metadata_requiring_header_field("approvalstatus"),
            ),
            patch("app.services.chat.write_validation.check_posting_invariants", AsyncMock(return_value=[])),
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
                        "slot_values": {"trandate": "2026-03-03"},
                    },
                )
            ]

        mock_execute_tool_call.assert_not_called()
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "missing required field: approvalstatus" in error_events[0]["error"]
        assert confirm_msg.structured_output["status"] == "pending"

    @pytest.mark.asyncio
    async def test_merged_payload_missing_line_required_is_refused_and_never_executes(self):
        """The defect this wave fixes: the post-merge re-check omitted
        `missing_line_required` from its refusal condition, even though the
        pre-merge gate treats it as a hard block (`_unfillable_line_fields`,
        whose own comment says such a payload 'must never be approved'). A
        payload that becomes line-incomplete only after the merge — e.g. the
        record-type metadata cache refetched between card build and approve
        and a line field became required — must be refused here too, not
        executed."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        confirm_msg, _tool_name = _make_journal_entry_card_with_trandate_slot_and_incomplete_line(session_id)
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", AsyncMock(return_value=None)),
            patch(
                "app.services.chat.write_validation.get_record_metadata",
                _fake_metadata_requiring_line_field("account"),
            ),
            patch("app.services.chat.write_validation.check_posting_invariants", AsyncMock(return_value=[])),
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
                        "slot_values": {"trandate": "2026-03-03"},
                    },
                )
            ]

        mock_execute_tool_call.assert_not_called()
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "missing required line field: line[0].account" in error_events[0]["error"]
        assert confirm_msg.structured_output["status"] == "pending"

    @pytest.mark.asyncio
    async def test_no_slot_values_on_a_slot_free_card_skips_revalidation_entirely(self):
        """B5: a no-merge approve executes bytes identical to what the
        intercept already validated — validate_mutation must not be
        invoked."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "clean co"}'}
        confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input)
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999", "success": True}))
        validate_mutation_spy = AsyncMock()

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", AsyncMock(return_value=None)),
            patch("app.services.chat.write_validation.validate_mutation", validate_mutation_spy),
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

        validate_mutation_spy.assert_not_called()
        mock_execute_tool_call.assert_awaited_once()
        assert not [e for e in events if e.get("type") == "error"]
        assert confirm_msg.structured_output["status"] == "approved"


# ---------------------------------------------------------------------------
# J) Atomic claim — T2 gate finding #9. The old code re-read status='pending'
# at the top of the write_confirm block and only wrote 'approved'/'failed'
# AFTER execute_tool_call returned — no row lock, no compare-and-swap. Two
# concurrent approves of the same confirmation_id both pass every gate (both
# read 'pending' before either commits) and BOTH would post to NetSuite: a
# double-posted journal entry or duplicate customer deposit. The fix claims
# the row (pending -> executing) via an atomic UPDATE ... WHERE status =
# 'pending', committed BEFORE the external call, immediately before
# execute_tool_call and after every other gate.
# ---------------------------------------------------------------------------


class TestWriteConfirmAtomicClaim:
    @pytest.mark.asyncio
    async def test_concurrent_approves_execute_tool_call_exactly_once(self):
        """Two requests approving the SAME confirmation, genuinely racing —
        both pass every gate above (both would read status='pending' before
        either commits, exactly like two live HTTP requests) — must still
        result in execute_tool_call being awaited exactly ONCE. That is the
        property that matters, not merely that one side reports an error: a
        caller could satisfy 'yields an error' while STILL having executed
        first. The loser must get a clear error and the row must end in a
        single coherent terminal state.

        Real concurrency, not a sequential double-call: `execute_tool_call`
        genuinely suspends (`await asyncio.sleep(0)`) before returning —
        standing in for the multi-second NetSuite HTTP call that IS the race
        window in production ("seconds, not microseconds" per the report).
        Without a genuine suspension SOMEWHERE in this fully-mocked flow,
        `asyncio.gather` on two mock-backed generators never actually
        interleaves — nothing else here yields to the event loop — so one
        drain would run to full completion before the other even started,
        which would "pass" this test even against the OLD, unfixed code, not
        because the race is closed but because there was no race to begin
        with. Putting the suspension inside `execute_tool_call` itself (the
        one call that's genuinely slow in reality) is what makes the second
        request reach its OWN claim attempt while the first request's write
        is still in flight — exactly the reported failure mode.
        """
        from sqlalchemy import Update

        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "race co"}'}
        confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input)
        session = _make_session(session_id=str(session_id))

        # The shared "row" both requests' CAS checks against — analogous to
        # the one Postgres row two real connections would race on.
        row_state = {"status": "pending"}

        async def _execute(stmt, *args, **kwargs):
            if isinstance(stmt, Update):
                # Single-threaded asyncio makes this check-then-mutate
                # atomic with respect to the other task — nothing awaits
                # between reading and writing row_state.
                if row_state["status"] == "pending":
                    row_state["status"] = "executing"
                    return MagicMock(rowcount=1)
                return MagicMock(rowcount=0)
            # The initial SELECT, and a text() call (set_tenant_context,
            # re-established after the claim commit — its return value is
            # discarded by the caller).
            return _FakeScalarResult(confirm_msg)

        def _make_racy_db():
            db = MagicMock()
            db.execute = _execute
            db.commit = AsyncMock()
            db.refresh = AsyncMock()
            db.add = MagicMock()
            return db

        # Two separate mock sessions — two separate requests would hold two
        # separate DB connections in reality — wired to the SAME row_state
        # so the race is real, not simulated by sharing one mock.
        db_a = _make_racy_db()
        db_b = _make_racy_db()

        async def _racy_execute_tool_call(**kwargs):
            await asyncio.sleep(0)  # the multi-second NetSuite call, standing in
            return json.dumps({"id": "999", "success": True})

        mock_execute_tool_call = AsyncMock(side_effect=_racy_execute_tool_call)
        mock_log_event = AsyncMock(return_value=None)

        async def _drain(db):
            return [
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

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
        ):
            events_a, events_b = await asyncio.gather(_drain(db_a), _drain(db_b))

        # The property that matters — not "an error was yielded somewhere".
        mock_execute_tool_call.assert_awaited_once()

        all_events = events_a + events_b
        error_events = [e for e in all_events if e.get("type") == "error"]
        message_events = [e for e in all_events if e.get("type") == "message"]
        assert len(error_events) == 1, f"expected exactly one refusal, got {error_events}"
        assert "already being processed" in error_events[0]["error"].lower()
        assert len(message_events) == 1, f"expected exactly one success message, got {message_events}"

        # The row ends in ONE coherent terminal state — approved, from the
        # winner's execute_tool_call succeeding. Never stuck at 'executing'
        # (that only happens on a genuine crash between claim and write —
        # see the comment at the claim site) and never double-applied.
        assert confirm_msg.structured_output["status"] == "approved"

    @pytest.mark.asyncio
    async def test_approve_against_executing_row_is_refused(self):
        """A confirmation already claimed (status='executing' — e.g. the
        winner of a prior race, still mid-flight against NetSuite) must be
        refused by the same pending-only gate that already blocks 'approved'
        / 'failed' / 'rejected' — 'executing' is just one more status that
        isn't 'pending'."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
        confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input, status="executing")
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
    async def test_claim_loses_yields_clear_error_without_executing(self):
        """Unit-level mirror of the concurrent test above, at the CAS
        boundary itself: when the UPDATE ... WHERE status='pending' returns
        rowcount=0 (another request already won), this request must refuse
        cleanly — never call execute_tool_call, never touch the stored
        status itself (the winner owns that transition)."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "clean co"}'}
        confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input)
        db = _make_db(confirm_msg, cas_rowcount=0)
        session = _make_session(session_id=str(session_id))

        mock_execute_tool_call = AsyncMock()
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

        mock_execute_tool_call.assert_not_awaited()
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "already being processed" in error_events[0]["error"].lower()


# ---------------------------------------------------------------------------
# K) T2 gate Finding B, full approve-path integration — a stored card whose
# tool_input already carries two coerced payload keys (a legacy card minted
# before the write_payload.py fix, or a tampered structured_output) must be
# refused BEFORE the atomic claim, never reaching execute_tool_call. The
# real refusal fires inside merge_slot_values's own normalize_write_payload
# call (write_confirmation_service.py) — proven unreachable past that point
# for a genuinely MERGED payload (blast_radius analysis, T2 gate finding B).
# This test drives the REAL run_chat_turn approve branch end to end, not
# just the merge_slot_values unit function, so a wiring regression between
# the two would be caught here.
# ---------------------------------------------------------------------------


class TestApprovePathRefusesDualKeyStoredPayload:
    @pytest.mark.asyncio
    async def test_dual_key_stored_payload_errors_before_cas_no_execute(self):
        from sqlalchemy import Update

        from app.services.chat.mutation_guard import generate_confirmation_token
        from app.services.chat.orchestrator import run_chat_turn
        from app.services.chat.write_confirmation_service import _build_payload_json

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        # A dual-coerced tool_input — only reachable today as a legacy/
        # tampered stored card, since build_confirmation_payload itself
        # would now refuse to mint one.
        tool_input = {"recordType": "customer", "data": {"companyname": "A"}, "body": {"companyname": "B"}}
        slots = [
            {
                "name": "subsidiary",
                "label": "Primary Subsidiary",
                "type": "select",
                "allowed": [{"value": "1", "label": "Framework Inc"}],
            }
        ]
        structured_output = {
            "type": "write_confirmation",
            "mutation_type": "create",
            "record_type": "customer",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "editable_slots": slots,
            "proposed_fields": {"companyname": "A"},
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

        cas_attempted = False

        def _make_watching_db():
            nonlocal cas_attempted

            async def _execute(stmt, *args, **kwargs):
                nonlocal cas_attempted
                if isinstance(stmt, Update):
                    cas_attempted = True
                return _FakeScalarResult(confirm_msg)

            db = MagicMock()
            db.execute = _execute
            db.commit = AsyncMock()
            db.refresh = AsyncMock()
            db.add = MagicMock()
            return db

        db = _make_watching_db()
        session = _make_session(session_id=str(session_id))

        mock_execute_tool_call = AsyncMock(return_value=json.dumps({"id": "999"}))

        with patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call):
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
                        "slot_values": {"subsidiary": "1"},
                    },
                )
            ]

        mock_execute_tool_call.assert_not_awaited()
        assert cas_attempted is False, "the atomic claim must never even run for an unparseable stored payload"
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "unparseable" in error_events[0]["error"].lower()
        assert confirm_msg.structured_output["status"] == "pending"


# ---------------------------------------------------------------------------
# L) T2 gate Finding A (mirror) — the reject branch had no compare-and-swap,
# while approve was hardened with one (section J above). Both branches read
# the SAME stale `_so` snapshot once at the top of the write_confirm block;
# reject then applied an UNCONDITIONAL overwrite off that snapshot. A
# concurrent approve + reject (double-click, retried request): approve wins
# the CAS, transitions to 'executing', and calls NetSuite — reject then
# overwrites the row to 'rejected' from its stale snapshot and tells the
# human "No changes were made", while the write actually executed. The card
# permanently misreports whether the mutation happened.
#
# Fix: reject claims the row via the SAME atomic UPDATE ... WHERE
# status='pending' shape approve uses, extracted into one shared helper
# (`_cas_claim_write_confirmation`) both branches call — this repo's SIXTH
# instance of a fix applied at one site and not its twin is why this is a
# helper, not two hand-maintained copies of the same race guard.
# ---------------------------------------------------------------------------


class TestWriteConfirmRejectAtomicClaim:
    @pytest.mark.asyncio
    async def test_plain_reject_on_pending_card_still_works(self):
        """Unraced reject: exactly the pre-fix behavior — status flips to
        'rejected', a 'no changes were made' message is yielded."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "clean co"}'}
        confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input)
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        events = [
            event
            async for event in run_chat_turn(
                db=db,
                session=session,
                user_message="reject",
                user_id=_USER_ID,
                tenant_id=_TENANT_ID,
                write_confirm={"action": "reject", "confirmation_id": str(confirm_msg.id)},
            )
        ]

        assert not [e for e in events if e.get("type") == "error"]
        message_events = [e for e in events if e.get("type") == "message"]
        assert message_events
        assert "no changes were made" in message_events[-1]["message"]["content"].lower()
        assert confirm_msg.structured_output["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_reject_against_executing_row_is_refused_without_overwrite(self):
        """A confirmation already claimed by an in-flight approve (status=
        'executing') must be refused by the SAME pending-only gate that
        already blocks 'approved'/'failed'/'rejected' — not silently
        flipped to 'rejected'."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "test ai customer"}'}
        confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input, status="executing")
        db = _make_db(confirm_msg)
        session = _make_session(session_id=str(session_id))

        events = [
            event
            async for event in run_chat_turn(
                db=db,
                session=session,
                user_message="reject",
                user_id=_USER_ID,
                tenant_id=_TENANT_ID,
                write_confirm={"action": "reject", "confirmation_id": str(confirm_msg.id)},
            )
        ]

        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "not in a pending state" in error_events[0]["error"].lower()
        assert confirm_msg.structured_output["status"] == "executing"

    @pytest.mark.asyncio
    async def test_reject_claim_loses_yields_clear_error_without_overwriting(self):
        """Unit-level mirror of the approve CAS-loses test: when reject's own
        UPDATE ... WHERE status='pending' returns rowcount=0 (another
        request already claimed/resolved the row between the top-of-block
        read and here), it must refuse cleanly — never touch
        confirm_msg.structured_output."""
        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "clean co"}'}
        confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input)
        db = _make_db(confirm_msg, cas_rowcount=0)
        session = _make_session(session_id=str(session_id))

        events = [
            event
            async for event in run_chat_turn(
                db=db,
                session=session,
                user_message="reject",
                user_id=_USER_ID,
                tenant_id=_TENANT_ID,
                write_confirm={"action": "reject", "confirmation_id": str(confirm_msg.id)},
            )
        ]

        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events
        assert "already" in error_events[0]["error"].lower()
        # Must NOT have been overwritten — the winner (whoever it was) owns
        # the transition, not this request's stale snapshot.
        assert confirm_msg.structured_output["status"] == "pending"

    @pytest.mark.asyncio
    async def test_concurrent_approve_and_reject_approve_wins_reject_refused_without_overwrite(self):
        """THE reported bug, reproduced with genuine interleaving.

        Approve and reject race on the SAME confirmation — both read the
        SAME stale pre-CAS `_so` snapshot (mirrors two live HTTP requests
        both reading status='pending' before either commits). Approve's
        synchronous CAS wins first (nothing suspends it before that point),
        THEN it suspends inside execute_tool_call (standing in for the
        multi-second NetSuite call), giving reject its turn. Pre-fix,
        reject's unconditional overwrite would flip the row to 'rejected'
        out from under the in-flight approve, and tell the human "No
        changes were made" while the write had actually executed. Post-fix,
        reject's own CAS attempt loses (the row is already 'executing') and
        it refuses cleanly — the row ends up 'approved', never 'rejected'.

        Same real-interleaving technique as
        test_concurrent_approves_execute_tool_call_exactly_once above: the
        genuine suspension lives inside execute_tool_call
        (`await asyncio.sleep(0)`), not anywhere else in this fully-mocked
        flow — without it, asyncio.gather on two mock-backed generators
        never actually interleaves.
        """
        from sqlalchemy import Update

        from app.services.chat.orchestrator import run_chat_turn

        session_id = uuid.uuid4()
        tool_name = _ext("ns_createRecord")
        tool_input = {"recordType": "customer", "data": '{"companyname": "race co"}'}
        confirm_msg = _make_real_confirmation_msg(session_id, tool_name, tool_input)
        session = _make_session(session_id=str(session_id))

        row_state = {"status": "pending"}

        async def _execute(stmt, *args, **kwargs):
            if isinstance(stmt, Update):
                if row_state["status"] == "pending":
                    row_state["status"] = "claimed"
                    return MagicMock(rowcount=1)
                return MagicMock(rowcount=0)
            return _FakeScalarResult(confirm_msg)

        def _make_racy_db():
            db = MagicMock()
            db.execute = _execute
            db.commit = AsyncMock()
            db.refresh = AsyncMock()
            db.add = MagicMock()
            return db

        db_approve = _make_racy_db()
        db_reject = _make_racy_db()

        async def _racy_execute_tool_call(**kwargs):
            await asyncio.sleep(0)  # the multi-second NetSuite call, standing in
            return json.dumps({"id": "999", "success": True})

        mock_execute_tool_call = AsyncMock(side_effect=_racy_execute_tool_call)
        mock_log_event = AsyncMock(return_value=None)

        async def _drain(db, action):
            return [
                event
                async for event in run_chat_turn(
                    db=db,
                    session=session,
                    user_message=action,
                    user_id=_USER_ID,
                    tenant_id=_TENANT_ID,
                    write_confirm={"action": action, "confirmation_id": str(confirm_msg.id)},
                )
            ]

        with (
            patch("app.services.chat.orchestrator.execute_tool_call", mock_execute_tool_call),
            patch("app.services.chat.orchestrator.log_event", mock_log_event),
        ):
            events_approve, events_reject = await asyncio.gather(
                _drain(db_approve, "approve"), _drain(db_reject, "reject")
            )

        # The property that matters — not "an error was yielded somewhere".
        mock_execute_tool_call.assert_awaited_once()

        all_events = events_approve + events_reject
        error_events = [e for e in all_events if e.get("type") == "error"]
        message_events = [e for e in all_events if e.get("type") == "message"]
        assert len(error_events) == 1, f"expected exactly one refusal, got {error_events}"
        assert "already" in error_events[0]["error"].lower()
        assert len(message_events) == 1, f"expected exactly one terminal message, got {message_events}"

        # THE bug this pins: approve won the race — the row must NOT end up
        # 'rejected', and the message shown must be the approve outcome,
        # never "No changes were made" for a write that actually executed.
        assert confirm_msg.structured_output["status"] == "approved"
        assert "no changes were made" not in message_events[0]["message"]["content"].lower()
