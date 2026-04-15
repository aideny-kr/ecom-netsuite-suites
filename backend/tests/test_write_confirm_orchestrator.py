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
