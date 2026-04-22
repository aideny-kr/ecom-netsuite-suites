"""Regression tests for orchestrator code path safety.

These tests verify that all branches in run_chat_turn() initialize
required variables before use. They catch UnboundLocalError bugs
that occur when new code paths skip variable assignments.

Tests use lightweight mocks — they exercise the orchestrator's branching
logic without running the full agent pipeline.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

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
        assert "is_web_search = False" in init_region, "is_web_search must be initialized before chitchat branch"
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

        session = _make_session(
            messages=[
                _make_user_msg_dict("how many orders"),
                _make_assistant_msg_dict("There were 500 orders this week."),
            ]
        )
        # Patch so we convert messages list to the expected dict format
        session.messages = [
            MagicMock(
                id=uuid.uuid4(),
                role="user",
                content="how many orders",
                tool_calls=None,
                citations=None,
                created_at=datetime.now(timezone.utc),
                structured_output=None,
                token_count=None,
                content_summary=None,
                agent_id=None,
                query_importance=None,
                confidence_score=None,
            ),
            MagicMock(
                id=uuid.uuid4(),
                role="assistant",
                content="There were 500 orders this week. " * 3,
                tool_calls=None,
                citations=None,
                created_at=datetime.now(timezone.utc),
                structured_output=None,
                token_count=None,
                content_summary=None,
                agent_id=None,
                query_importance=None,
                confidence_score=None,
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

        session = _make_session(
            source_pin=None,
            messages=[
                MagicMock(
                    id=uuid.uuid4(),
                    role="user",
                    content="how many orders",
                    tool_calls=None,
                    citations=None,
                    created_at=datetime.now(timezone.utc),
                    structured_output=None,
                    token_count=None,
                    content_summary=None,
                    agent_id=None,
                    query_importance=None,
                    confidence_score=None,
                ),
                MagicMock(
                    id=uuid.uuid4(),
                    role="assistant",
                    content="Based on the analysis, there were 1,247 orders. " * 3,
                    tool_calls=None,
                    citations=None,
                    created_at=datetime.now(timezone.utc),
                    structured_output=None,
                    token_count=None,
                    content_summary=None,
                    agent_id=None,
                    query_importance=None,
                    confidence_score=None,
                ),
            ],
        )

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


class TestOrchestratorWriteConfirmPaths:
    """Smoke tests: write_confirm approve/reject paths don't crash with UnboundLocalError.

    The write_confirm short-circuit runs at the very top of run_chat_turn, before
    any history/RAG/context assembly. These tests verify the short-circuit path
    returns cleanly without raising UnboundLocalError on any variable it references.
    """

    @pytest.mark.asyncio
    async def test_write_confirm_approve_path(self):
        """write_confirm approve path must not raise UnboundLocalError.

        The approve path: db lookup → validate HMAC → execute tool → yield message → return.
        We drive it to an early-exit ('Confirmation message not found') to verify the
        branching variables are always initialized before use.
        """
        from unittest.mock import AsyncMock, MagicMock

        from app.services.chat.orchestrator import run_chat_turn

        session = _make_session()
        confirmation_id = str(uuid.uuid4())

        # Simulate db.execute() returning a result where scalar_one_or_none() returns None
        # This triggers the early-exit: "Confirmation message not found."
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        db = AsyncMock()
        db.execute = AsyncMock(return_value=mock_result)

        user_msg = _make_user_msg("approve")

        chunks = []
        try:
            async for chunk in run_chat_turn(
                db=db,
                session=session,
                user_message="approve",
                user_id=uuid.uuid4(),
                tenant_id=session.tenant_id,
                user_msg=user_msg,
                write_confirm={"action": "approve", "confirmation_id": confirmation_id},
            ):
                chunks.append(chunk)
                if len(chunks) > 10:
                    break
        except UnboundLocalError:
            pytest.fail("UnboundLocalError: write_confirm approve path has uninitialized variables")
        except Exception:
            # Other exceptions are expected (mocked deps, DB errors, etc.)
            pass

        # The early-exit should yield an error event (message not found).
        # If we got here without UnboundLocalError, the path is safe — whether
        # or not the error chunk arrived depends on mock depth, so we don't
        # assert on `chunks` content here.

    @pytest.mark.asyncio
    async def test_write_confirm_reject_path(self):
        """write_confirm reject path must not raise UnboundLocalError.

        The reject path: db lookup → check status → update status → yield message → return.
        We drive it to an early-exit ('Confirmation message not found') to verify the path.
        """
        from unittest.mock import AsyncMock, MagicMock

        from app.services.chat.orchestrator import run_chat_turn

        session = _make_session()
        confirmation_id = str(uuid.uuid4())

        # Simulate db.execute() returning None — triggers early-exit
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        db = AsyncMock()
        db.execute = AsyncMock(return_value=mock_result)

        user_msg = _make_user_msg("reject")

        chunks = []
        try:
            async for chunk in run_chat_turn(
                db=db,
                session=session,
                user_message="reject",
                user_id=uuid.uuid4(),
                tenant_id=session.tenant_id,
                user_msg=user_msg,
                write_confirm={"action": "reject", "confirmation_id": confirmation_id},
            ):
                chunks.append(chunk)
                if len(chunks) > 10:
                    break
        except UnboundLocalError:
            pytest.fail("UnboundLocalError: write_confirm reject path has uninitialized variables")
        except Exception:
            # Other exceptions are expected (mocked deps, DB errors, etc.)
            pass

    @pytest.mark.asyncio
    async def test_write_confirm_not_pending_early_exit(self):
        """write_confirm when structured_output.status != 'pending' yields error and returns.

        This exercises the branch: 'Confirmation is not in a pending state.'
        """
        from unittest.mock import AsyncMock, MagicMock

        from app.services.chat.orchestrator import run_chat_turn

        session = _make_session()
        confirmation_id = str(uuid.uuid4())

        # Return a msg whose structured_output has status='approved' (already done)
        mock_confirm_msg = MagicMock()
        mock_confirm_msg.structured_output = {
            "type": "write_confirmation",
            "status": "approved",  # not pending — triggers early-exit
        }

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_confirm_msg

        db = AsyncMock()
        db.execute = AsyncMock(return_value=mock_result)

        user_msg = _make_user_msg("approve again")

        chunks = []
        try:
            async for chunk in run_chat_turn(
                db=db,
                session=session,
                user_message="approve again",
                user_id=uuid.uuid4(),
                tenant_id=session.tenant_id,
                user_msg=user_msg,
                write_confirm={"action": "approve", "confirmation_id": confirmation_id},
            ):
                chunks.append(chunk)
                if len(chunks) > 10:
                    break
        except UnboundLocalError:
            pytest.fail("UnboundLocalError: write_confirm not-pending path has uninitialized variables")
        except Exception:
            pass

        # Should yield exactly one error event
        error_chunks = [c for c in chunks if c.get("type") == "error"]
        assert len(error_chunks) == 1
        assert "pending" in error_chunks[0]["error"].lower()


class TestOrchestratorVariableInitExtended:
    """Additional static source inspection tests for safety net coverage.

    These verify that variables used after branch points are always initialized
    before those branches, preventing the UnboundLocalError class of bug.
    """

    def test_system_prompt_initialized_before_routing(self):
        """system_prompt must be initialized (in onboarding AND else branch)
        before the routing block that eventually uses it in the legacy agent path.

        Both branches (is_onboarding=True and is_onboarding=False) must assign
        system_prompt so the legacy coordinator path (which reads system_prompt)
        always has a value.
        """
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)

        # Both the onboarding branch and the else branch must assign system_prompt
        # before the multi-agent routing block
        routing_idx = source.index("Multi-agent routing")
        system_prompt_region = source[:routing_idx]

        # system_prompt = ONBOARDING_SYSTEM_PROMPT (onboarding branch)
        assert "system_prompt = ONBOARDING_SYSTEM_PROMPT" in system_prompt_region, (
            "system_prompt must be assigned in the onboarding branch before routing"
        )
        # system_prompt = await get_active_template(...) (normal branch)
        assert "system_prompt = await get_active_template" in system_prompt_region, (
            "system_prompt must be assigned in the normal branch before routing"
        )

    def test_importance_tier_initialized_before_routing(self):
        """importance_tier must be initialized before the routing elif chain.

        It has a default assignment early in the function, then is overridden
        inside the unified-agent path. The default must exist so legacy paths
        that skip the unified block still have importance_tier defined.
        """
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)

        # Find the importance tier default assignment
        assert "importance_tier = classify_importance(sanitized_input)" in source, (
            "importance_tier must have a default assignment via classify_importance"
        )

        # The default assignment must come BEFORE the multi-agent routing block
        routing_idx = source.index("Multi-agent routing")
        importance_idx = source.index("importance_tier = classify_importance(sanitized_input)")
        assert importance_idx < routing_idx, "importance_tier default must be initialized before routing block"

    def test_context_need_gating_for_schema_injection(self):
        """The orchestrator must gate schema injection on context_need.

        This verifies the injection matrix is enforced — schemas are only
        fetched when _need_schemas is True (FULL or DATA context need).
        This prevents expensive DB calls for CASUAL/DOCS queries.
        """
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)

        # The context_need gating check must exist
        assert "_need_schemas = context_need in" in source, "_need_schemas flag must gate schema injection"
        # The schema injection must check _need_schemas before executing
        assert "if not _need_schemas:" in source, "Schema injection must be guarded by 'if not _need_schemas:' check"

    def test_importance_tier_casual_gates_haiku_routing(self):
        """importance_tier.value <= 2 (CASUAL/OPERATIONAL) gates Haiku routing.

        This verifies the importance tier check exists and uses the right threshold
        so CASUAL queries can be routed to the cheaper/faster Haiku model.
        """
        import inspect

        from app.services.chat.orchestrator import run_chat_turn

        source = inspect.getsource(run_chat_turn)

        # The Haiku routing check must use importance_tier.value
        assert "importance_tier.value <= 2" in source, "Haiku routing must be gated by importance_tier.value <= 2"
        # And it must reference HAIKU_MODEL
        assert "HAIKU_MODEL" in source, "HAIKU_MODEL constant must be used when routing simple lookups"
