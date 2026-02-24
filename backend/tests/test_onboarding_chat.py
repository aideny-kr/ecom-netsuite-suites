"""Tests for chat-based onboarding flow."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatMessage, ChatSession
from app.models.tenant import TenantConfig
from app.services.chat.onboarding_tools import (
    ONBOARDING_TOOL_DEFINITIONS,
    execute_onboarding_tool,
)
from app.services.chat.prompts import ONBOARDING_SYSTEM_PROMPT
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers

# ---------------------------------------------------------------------------
# Async generator helpers
# ---------------------------------------------------------------------------


async def _collect_stream_result(async_gen):
    """Consume the run_chat_turn async generator and return the final message dict."""
    result = None
    async for chunk in async_gen:
        if chunk.get("type") == "message":
            result = chunk["message"]
    return result


def _make_stream_side_effect(responses):
    call_count = 0

    async def stream_fn(**kwargs):
        nonlocal call_count
        resp = responses[call_count] if call_count < len(responses) else responses[-1]
        call_count += 1
        for text in resp.text_blocks:
            yield "text", text
        yield "response", resp

    return stream_fn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tenant(db: AsyncSession):
    return await create_test_tenant(db)


@pytest_asyncio.fixture
async def user_and_headers(db: AsyncSession, tenant):
    user, _ = await create_test_user(db, tenant)
    return user, make_auth_headers(user)


@pytest_asyncio.fixture
async def tenant_b(db: AsyncSession):
    return await create_test_tenant(db, name="Other Corp", slug=f"other-{uuid.uuid4().hex[:6]}")


@pytest_asyncio.fixture
async def user_b_and_headers(db: AsyncSession, tenant_b):
    user, _ = await create_test_user(db, tenant_b)
    return user, make_auth_headers(user)


# ---------------------------------------------------------------------------
# Onboarding status endpoint tests
# ---------------------------------------------------------------------------


class TestOnboardingStatus:
    @pytest.mark.asyncio
    async def test_onboarding_status_not_completed(self, client, user_and_headers):
        _, headers = user_and_headers
        resp = await client.get("/api/v1/onboarding/status", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["completed"] is False
        assert data["completed_at"] is None

    @pytest.mark.asyncio
    async def test_onboarding_status_after_completion(self, client, db, user_and_headers, tenant):
        user, headers = user_and_headers
        # Mark onboarding as completed
        result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant.id))
        config = result.scalar_one()
        config.onboarding_completed_at = datetime.now(timezone.utc)
        await db.flush()

        resp = await client.get("/api/v1/onboarding/status", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["completed"] is True
        assert data["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_onboarding_requires_auth(self, client):
        resp = await client.get("/api/v1/onboarding/status")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Onboarding chat start endpoint tests
# ---------------------------------------------------------------------------


class TestOnboardingChatStart:
    @pytest.mark.asyncio
    async def test_start_onboarding_creates_session_with_type(self, client, db, user_and_headers):
        _, headers = user_and_headers
        with patch("app.api.v1.onboarding.run_chat_turn", new_callable=AsyncMock) as mock_turn:
            mock_msg = AsyncMock()
            mock_msg.id = uuid.uuid4()
            mock_msg.role = "assistant"
            mock_msg.content = "Welcome! Let's get you set up."
            mock_msg.created_at = datetime.now(timezone.utc)
            mock_turn.return_value = mock_msg

            resp = await client.post("/api/v1/onboarding/chat/start", headers=headers)
            assert resp.status_code == 201
            data = resp.json()
            assert "session_id" in data
            assert data["message"]["role"] == "assistant"

            # Verify session type
            session_result = await db.execute(
                select(ChatSession).where(ChatSession.id == uuid.UUID(data["session_id"]))
            )
            session = session_result.scalar_one()
            assert session.session_type == "onboarding"

    @pytest.mark.asyncio
    async def test_start_onboarding_returns_greeting(self, client, user_and_headers):
        _, headers = user_and_headers
        with patch("app.api.v1.onboarding.run_chat_turn", new_callable=AsyncMock) as mock_turn:
            mock_msg = AsyncMock()
            mock_msg.id = uuid.uuid4()
            mock_msg.role = "assistant"
            mock_msg.content = "Hello! Welcome to the platform."
            mock_msg.created_at = datetime.now(timezone.utc)
            mock_turn.return_value = mock_msg

            resp = await client.post("/api/v1/onboarding/chat/start", headers=headers)
            assert resp.status_code == 201
            data = resp.json()
            assert len(data["message"]["content"]) > 0

    @pytest.mark.asyncio
    async def test_duplicate_start_returns_existing(self, client, db, user_and_headers):
        user, headers = user_and_headers

        # Create existing onboarding session with a message
        session = ChatSession(
            tenant_id=user.tenant_id,
            user_id=user.id,
            title="Onboarding",
            session_type="onboarding",
        )
        db.add(session)
        await db.flush()

        msg = ChatMessage(
            tenant_id=user.tenant_id,
            session_id=session.id,
            role="assistant",
            content="Existing greeting.",
        )
        db.add(msg)
        await db.flush()

        resp = await client.post("/api/v1/onboarding/chat/start", headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["session_id"] == str(session.id)
        assert data["message"]["content"] == "Existing greeting."

    @pytest.mark.asyncio
    async def test_onboarding_start_requires_auth(self, client):
        resp = await client.post("/api/v1/onboarding/chat/start")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Orchestrator routing tests
# ---------------------------------------------------------------------------


class TestOrchestratorOnboardingRouting:
    @pytest.mark.asyncio
    async def test_onboarding_session_uses_onboarding_prompt(self, db, user_and_headers, tenant):
        """Verify that onboarding sessions use ONBOARDING_SYSTEM_PROMPT."""
        from sqlalchemy.orm import selectinload

        user, _ = user_and_headers
        session = ChatSession(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Onboarding",
            session_type="onboarding",
        )
        db.add(session)
        await db.flush()

        # Re-fetch with eager loading to avoid lazy load errors
        result = await db.execute(
            select(ChatSession).options(selectinload(ChatSession.messages)).where(ChatSession.id == session.id)
        )
        session = result.scalar_one()

        with (
            patch("app.services.chat.orchestrator.get_adapter") as mock_adapter_fn,
            patch(
                "app.services.chat.orchestrator.get_tenant_ai_config",
                return_value=("anthropic", "claude-sonnet-4-5-20250929", "fake-key", False),
            ),
        ):
            mock_adapter = AsyncMock()
            mock_response = AsyncMock()
            mock_response.tool_use_blocks = []
            mock_response.text_blocks = ["Welcome!"]
            mock_response.usage = AsyncMock(input_tokens=10, output_tokens=20)
            mock_adapter.create_message = AsyncMock(return_value=mock_response)
            mock_adapter.stream_message.side_effect = _make_stream_side_effect([mock_response])
            mock_adapter_fn.return_value = mock_adapter

            from app.services.chat.orchestrator import run_chat_turn

            async for _ in run_chat_turn(
                db=db,
                session=session,
                user_message="Hello",
                user_id=user.id,
                tenant_id=tenant.id,
            ):
                pass

            # Verify the system prompt used was the onboarding prompt
            call_args = mock_adapter.stream_message.call_args
            assert call_args.kwargs["system"] == ONBOARDING_SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_onboarding_skips_rag(self, db, user_and_headers, tenant):
        """Verify that onboarding sessions skip RAG retrieval."""
        from sqlalchemy.orm import selectinload

        user, _ = user_and_headers
        session = ChatSession(
            tenant_id=tenant.id,
            user_id=user.id,
            title="Onboarding",
            session_type="onboarding",
        )
        db.add(session)
        await db.flush()

        # Re-fetch with eager loading
        result = await db.execute(
            select(ChatSession).options(selectinload(ChatSession.messages)).where(ChatSession.id == session.id)
        )
        session = result.scalar_one()

        with (
            patch("app.services.chat.orchestrator.get_adapter") as mock_adapter_fn,
            patch("app.services.chat.orchestrator.retriever_node") as mock_retriever,
            patch(
                "app.services.chat.orchestrator.get_tenant_ai_config",
                return_value=("anthropic", "claude-sonnet-4-5-20250929", "fake-key", False),
            ),
        ):
            mock_adapter = AsyncMock()
            mock_response = AsyncMock()
            mock_response.tool_use_blocks = []
            mock_response.text_blocks = ["Welcome!"]
            mock_response.usage = AsyncMock(input_tokens=10, output_tokens=20)
            mock_adapter.create_message = AsyncMock(return_value=mock_response)
            mock_adapter.stream_message.side_effect = _make_stream_side_effect([mock_response])
            mock_adapter_fn.return_value = mock_adapter

            from app.services.chat.orchestrator import run_chat_turn

            async for _ in run_chat_turn(
                db=db,
                session=session,
                user_message="Hello",
                user_id=user.id,
                tenant_id=tenant.id,
            ):
                pass

            # RAG retriever should NOT have been called
            mock_retriever.assert_not_called()


# ---------------------------------------------------------------------------
# Onboarding tool execution tests
# ---------------------------------------------------------------------------


class TestOnboardingTools:
    @pytest.mark.asyncio
    async def test_save_profile_tool_creates_confirmed_profile(self, db, user_and_headers, tenant):
        user, _ = user_and_headers
        result = await execute_onboarding_tool(
            tool_name="save_onboarding_profile",
            tool_input={
                "industry": "retail",
                "business_description": "Online clothing store",
            },
            tenant_id=tenant.id,
            user_id=user.id,
            db=db,
        )

        import json

        data = json.loads(result)
        assert data["success"] is True
        assert "profile_id" in data

    @pytest.mark.asyncio
    async def test_save_profile_tool_marks_onboarding_complete(self, db, user_and_headers, tenant):
        user, _ = user_and_headers
        await execute_onboarding_tool(
            tool_name="save_onboarding_profile",
            tool_input={"industry": "tech"},
            tenant_id=tenant.id,
            user_id=user.id,
            db=db,
        )

        config_result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant.id))
        config = config_result.scalar_one()
        assert config.onboarding_completed_at is not None

    @pytest.mark.asyncio
    async def test_save_profile_tool_generates_prompt_template(self, db, user_and_headers, tenant):
        user, _ = user_and_headers
        await execute_onboarding_tool(
            tool_name="save_onboarding_profile",
            tool_input={"industry": "manufacturing", "business_description": "Custom furniture"},
            tenant_id=tenant.id,
            user_id=user.id,
            db=db,
        )

        from app.models.prompt_template import SystemPromptTemplate

        tmpl_result = await db.execute(
            select(SystemPromptTemplate).where(
                SystemPromptTemplate.tenant_id == tenant.id,
                SystemPromptTemplate.is_active.is_(True),
            )
        )
        template = tmpl_result.scalar_one_or_none()
        assert template is not None
        assert "manufacturing" in template.template_text

    @pytest.mark.asyncio
    async def test_start_netsuite_oauth_tool_returns_url(self, db, user_and_headers, tenant):
        user, _ = user_and_headers
        result = await execute_onboarding_tool(
            tool_name="start_netsuite_oauth",
            tool_input={"account_id": "TSTDRV1234567"},
            tenant_id=tenant.id,
            user_id=user.id,
            db=db,
        )

        import json

        data = json.loads(result)
        assert "authorize_url" in data
        assert "netsuite.com" in data["authorize_url"]
        assert "TSTDRV1234567" not in data["authorize_url"]  # account_id not in URL (it's encoded in state)

    @pytest.mark.asyncio
    async def test_check_connection_tool_no_connection(self, db, user_and_headers, tenant):
        user, _ = user_and_headers
        result = await execute_onboarding_tool(
            tool_name="check_netsuite_connection",
            tool_input={},
            tenant_id=tenant.id,
            user_id=user.id,
            db=db,
        )

        import json

        data = json.loads(result)
        assert data["connected"] is False

    @pytest.mark.asyncio
    async def test_check_connection_tool_with_connection(self, db, user_and_headers, tenant):
        user, _ = user_and_headers

        # Create an active connection
        from app.core.encryption import encrypt_credentials
        from app.models.connection import Connection

        conn = Connection(
            tenant_id=tenant.id,
            provider="netsuite",
            label="NetSuite Prod",
            status="active",
            encrypted_credentials=encrypt_credentials({"account_id": "TEST123"}),
        )
        db.add(conn)
        await db.flush()

        result = await execute_onboarding_tool(
            tool_name="check_netsuite_connection",
            tool_input={},
            tenant_id=tenant.id,
            user_id=user.id,
            db=db,
        )

        import json

        data = json.loads(result)
        assert data["connected"] is True
        assert data["connection_id"] == str(conn.id)


# ---------------------------------------------------------------------------
# Tenant isolation tests
# ---------------------------------------------------------------------------


class TestOnboardingTenantIsolation:
    @pytest.mark.asyncio
    async def test_tenant_isolation_onboarding_session(self, client, db, user_and_headers, user_b_and_headers, tenant):
        """Tenant B cannot access Tenant A's onboarding session."""
        user_a, headers_a = user_and_headers
        _, headers_b = user_b_and_headers

        # Create session for tenant A
        session = ChatSession(
            tenant_id=tenant.id,
            user_id=user_a.id,
            title="Onboarding",
            session_type="onboarding",
        )
        db.add(session)
        await db.flush()

        # Tenant B tries to access Tenant A's session
        resp = await client.get(f"/api/v1/chat/sessions/{session.id}", headers=headers_b)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tool definition structure tests
# ---------------------------------------------------------------------------


class TestOnboardingToolDefinitions:
    def test_tool_definitions_have_correct_structure(self):
        for tool in ONBOARDING_TOOL_DEFINITIONS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"

    def test_save_profile_tool_exists(self):
        names = [t["name"] for t in ONBOARDING_TOOL_DEFINITIONS]
        assert "save_onboarding_profile" in names

    def test_start_oauth_tool_exists(self):
        names = [t["name"] for t in ONBOARDING_TOOL_DEFINITIONS]
        assert "start_netsuite_oauth" in names

    def test_check_connection_tool_exists(self):
        names = [t["name"] for t in ONBOARDING_TOOL_DEFINITIONS]
        assert "check_netsuite_connection" in names


# ---------------------------------------------------------------------------
# Auth /me endpoint returns onboarding status
# ---------------------------------------------------------------------------


class TestAuthMeOnboarding:
    @pytest.mark.asyncio
    async def test_me_returns_onboarding_completed_at_null(self, client, user_and_headers):
        _, headers = user_and_headers
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "onboarding_completed_at" in data
        assert data["onboarding_completed_at"] is None

    @pytest.mark.asyncio
    async def test_me_returns_onboarding_completed_at_set(self, client, db, user_and_headers, tenant):
        _, headers = user_and_headers
        result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant.id))
        config = result.scalar_one()
        config.onboarding_completed_at = datetime.now(timezone.utc)
        await db.flush()

        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["onboarding_completed_at"] is not None
