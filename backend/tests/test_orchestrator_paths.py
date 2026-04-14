"""Regression tests for orchestrator code path safety.

These tests verify that all branches in run_chat_turn() initialize
required variables before use. They catch UnboundLocalError bugs
that occur when new code paths skip variable assignments.

Tests use lightweight mocks — they exercise the orchestrator's branching
logic without running the full agent pipeline.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.chat import ChatMessage, ChatSession


def _make_session(source_pin=None, messages=None, session_type="chat"):
    """Create a minimal ChatSession mock."""
    session = MagicMock(spec=ChatSession)
    session.id = uuid.uuid4()
    session.tenant_id = uuid.uuid4()
    session.session_type = session_type
    session.source_pin = source_pin
    session.workspace_id = None
    session.agent_id = None
    session.messages = messages or []
    session.title = None
    return session


def _make_user_msg(content="test"):
    """Create a minimal ChatMessage mock."""
    msg = MagicMock(spec=ChatMessage)
    msg.id = uuid.uuid4()
    msg.content = content
    msg.created_at = datetime.now(timezone.utc)
    return msg


def _make_assistant_msg(content="", structured_output=None):
    """Create a minimal assistant ChatMessage mock for history."""
    msg = MagicMock(spec=ChatMessage)
    msg.id = uuid.uuid4()
    msg.role = "assistant"
    msg.content = content
    msg.tool_calls = None
    msg.citations = None
    msg.created_at = datetime.now(timezone.utc)
    msg.structured_output = structured_output
    msg.token_count = None
    msg.content_summary = None
    msg.agent_id = None
    msg.query_importance = None
    msg.confidence_score = None
    return msg


def _make_user_msg_dict(content="how many orders"):
    return {"role": "user", "content": content}


def _make_assistant_msg_dict(content="Based on the data, there were 1,247 orders this week. " * 3):
    return {"role": "assistant", "content": content}


class TestOrchestratorVariableInit:
    """Verify variables are initialized across all orchestrator branches.

    These tests import the orchestrator module and check that key variables
    referenced after branch points are always initialized before use.
    This prevents the UnboundLocalError class of bugs.
    """

    def test_chitchat_flags_initialized(self):
        """Chitchat messages must have is_web_search, is_netsuite_entity,
        _has_data_reference initialized even when the else-branch is skipped."""
        # Verify the initialization lines exist in the source code
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)

        # These must appear BEFORE _is_chitchat check, not inside else
        # Find the chitchat detection line
        chitchat_idx = source.index("_is_chitchat = bool(_CHITCHAT_RE")

        # Find the if _is_chitchat: line
        if_chitchat_idx = source.index("if _is_chitchat:", chitchat_idx)

        # These initializations must be between chitchat detection and the if branch
        init_region = source[chitchat_idx:if_chitchat_idx]
        assert "is_web_search = False" in init_region, (
            "is_web_search must be initialized before chitchat branch"
        )
        assert "is_netsuite_entity = False" in init_region, (
            "is_netsuite_entity must be initialized before chitchat branch"
        )
        assert "_has_data_reference = False" in init_region, (
            "_has_data_reference must be initialized before chitchat branch"
        )

    def test_selected_agent_id_initialized(self):
        """_selected_agent_id must be initialized before the routing elif chain."""
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)

        # Find the routing block
        routing_idx = source.index("Three-tier routing")
        # Find the first use of _selected_agent_id after routing comment
        first_if_idx = source.index("if agent_id and not is_financial:", routing_idx)

        # _selected_agent_id = None must appear between routing comment and first if
        init_region = source[routing_idx:first_if_idx]
        assert "_selected_agent_id = None" in init_region, (
            "_selected_agent_id must be initialized before routing elif chain"
        )


class TestOrchestratorChitchatPath:
    """Smoke test: chitchat messages through the orchestrator don't crash."""

    @pytest.mark.asyncio
    async def test_chitchat_does_not_raise_unbound(self):
        """'good job' (chitchat) must not raise UnboundLocalError."""
        from app.services.chat.orchestrator import run_chat_turn

        session = _make_session(messages=[
            _make_user_msg_dict("how many orders"),
            _make_assistant_msg_dict("There were 500 orders this week."),
        ])
        # Patch so we convert messages list to the expected dict format
        session.messages = [
            MagicMock(
                id=uuid.uuid4(), role="user", content="how many orders",
                tool_calls=None, citations=None,
                created_at=datetime.now(timezone.utc),
                structured_output=None, token_count=None,
                content_summary=None, agent_id=None,
                query_importance=None, confidence_score=None,
            ),
            MagicMock(
                id=uuid.uuid4(), role="assistant",
                content="There were 500 orders this week. " * 3,
                tool_calls=None, citations=None,
                created_at=datetime.now(timezone.utc),
                structured_output=None, token_count=None,
                content_summary=None, agent_id=None,
                query_importance=None, confidence_score=None,
            ),
        ]

        db = AsyncMock()
        user_msg = _make_user_msg("good job")

        chunks = []
        try:
            async for chunk in run_chat_turn(
                db=db,
                session=session,
                user_message="good job",
                user_id=uuid.uuid4(),
                tenant_id=session.tenant_id,
                user_msg=user_msg,
            ):
                chunks.append(chunk)
                # We only need to get past the branching — break after first chunk
                # or after the orchestrator reaches agent execution (which will fail
                # on mock, but that's fine — we're testing variable initialization)
                if len(chunks) > 5:
                    break
        except UnboundLocalError:
            pytest.fail("UnboundLocalError: chitchat path has uninitialized variables")
        except Exception:
            # Other exceptions are expected (mocked dependencies) — we only care
            # about UnboundLocalError
            pass


class TestOrchestratorPickerSkipPath:
    """Smoke test: sessions with prior results skip picker without crashing."""

    @pytest.mark.asyncio
    async def test_prior_result_skip_picker_does_not_raise_unbound(self):
        """Ambiguous query in session with prior results must not raise UnboundLocalError."""
        from app.services.chat.orchestrator import run_chat_turn

        session = _make_session(source_pin=None, messages=[
            MagicMock(
                id=uuid.uuid4(), role="user", content="how many orders",
                tool_calls=None, citations=None,
                created_at=datetime.now(timezone.utc),
                structured_output=None, token_count=None,
                content_summary=None, agent_id=None,
                query_importance=None, confidence_score=None,
            ),
            MagicMock(
                id=uuid.uuid4(), role="assistant",
                content="Based on the analysis, there were 1,247 orders. " * 3,
                tool_calls=None, citations=None,
                created_at=datetime.now(timezone.utc),
                structured_output=None, token_count=None,
                content_summary=None, agent_id=None,
                query_importance=None, confidence_score=None,
            ),
        ])

        db = AsyncMock()
        user_msg = _make_user_msg("what about last month")

        chunks = []
        try:
            async for chunk in run_chat_turn(
                db=db,
                session=session,
                user_message="what about last month",
                user_id=uuid.uuid4(),
                tenant_id=session.tenant_id,
                user_msg=user_msg,
            ):
                chunks.append(chunk)
                if len(chunks) > 5:
                    break
        except UnboundLocalError:
            pytest.fail("UnboundLocalError: picker-skip path has uninitialized variables")
        except Exception:
            # Other exceptions expected from mocked deps
            pass
