"""Tests for chat pipeline resilience: error handling, graceful degradation, health endpoint."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.nodes import (
    OrchestratorState,
    retriever_node,
)


def _make_state(**overrides) -> OrchestratorState:
    defaults = {
        "user_message": "What are my recent orders?",
        "tenant_id": uuid.uuid4(),
        "actor_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
    }
    defaults.update(overrides)
    return OrchestratorState(**defaults)


def _mock_anthropic_response(text: str):
    mock_response = MagicMock()
    mock_content = MagicMock()
    mock_content.text = text
    mock_response.content = [mock_content]
    return mock_response


# ---------------------------------------------------------------------------
# B1: API key validation
# ---------------------------------------------------------------------------


class TestApiKeyValidation:
    @pytest.mark.asyncio
    async def test_get_tenant_ai_config_raises_without_key(self):
        """get_tenant_ai_config raises ValueError when no AI key is configured."""
        from app.services.chat.nodes import get_tenant_ai_config

        db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.chat.nodes.settings") as mock_settings:
            mock_settings.ANTHROPIC_API_KEY = ""
            mock_settings.ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
            with pytest.raises(ValueError, match="No AI provider configured"):
                await get_tenant_ai_config(db, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_get_tenant_ai_config_returns_platform_default(self):
        """get_tenant_ai_config returns platform defaults when no tenant config."""
        from app.services.chat.nodes import get_tenant_ai_config

        db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.chat.nodes.settings") as mock_settings:
            mock_settings.ANTHROPIC_API_KEY = "sk-test"
            mock_settings.ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
            provider, model, key = await get_tenant_ai_config(db, uuid.uuid4())
            assert provider == "anthropic"
            assert key == "sk-test"

    def test_voyage_client_raises_without_key(self):
        """get_voyage_client raises ValueError when VOYAGE_API_KEY is empty."""
        from app.services.chat import embeddings

        old_client = embeddings._client
        embeddings._client = None
        try:
            with patch("app.services.chat.embeddings.settings") as mock_settings:
                mock_settings.VOYAGE_API_KEY = ""
                with pytest.raises(ValueError, match="VOYAGE_API_KEY"):
                    embeddings.get_voyage_client()
        finally:
            embeddings._client = old_client


# ---------------------------------------------------------------------------
# B2: Fault tolerance
# ---------------------------------------------------------------------------


class TestRetrieverFaultTolerance:
    @pytest.mark.asyncio
    async def test_retriever_continues_on_embedding_failure(self):
        """retriever_node should set doc_chunks=[] and continue on failure."""
        state = _make_state(route={"needs_docs": True})
        db = AsyncMock(spec=AsyncSession)

        with patch("app.services.chat.nodes.embed_query", side_effect=Exception("Voyage API down")):
            await retriever_node(state, db)

        assert state.doc_chunks == []

    @pytest.mark.asyncio
    async def test_retriever_continues_on_db_failure(self):
        """retriever_node should set doc_chunks=[] on DB query failure."""
        state = _make_state(route={"needs_docs": True})
        db = AsyncMock(spec=AsyncSession)
        db.execute = AsyncMock(side_effect=Exception("DB connection lost"))

        with patch("app.services.chat.nodes.embed_query", new_callable=AsyncMock, return_value=[0.1] * 128):
            await retriever_node(state, db)

        assert state.doc_chunks == []


# ---------------------------------------------------------------------------
# B3: Endpoint error handling
# ---------------------------------------------------------------------------


class TestSendMessageErrorHandling:
    @pytest.mark.asyncio
    async def test_send_message_missing_api_key(self, client, db, admin_user):
        """POST /messages with no ANTHROPIC_API_KEY → 503."""
        user, headers = admin_user
        await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

        create_resp = await client.post("/api/v1/chat/sessions", json={"title": "Error Test"}, headers=headers)
        session_id = create_resp.json()["id"]

        with patch("app.api.v1.chat.run_chat_turn", side_effect=ValueError("ANTHROPIC_API_KEY is not configured")):
            resp = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"content": "Hello"},
                headers=headers,
            )

        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_send_message_auth_error(self, client, db, admin_user):
        """POST /messages with invalid API key → 503."""
        user, headers = admin_user
        await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

        create_resp = await client.post("/api/v1/chat/sessions", json={"title": "Auth Error Test"}, headers=headers)
        session_id = create_resp.json()["id"]

        auth_error = anthropic.AuthenticationError(
            message="Invalid API key",
            response=MagicMock(status_code=401),
            body={"error": {"message": "Invalid API key"}},
        )

        with patch("app.api.v1.chat.run_chat_turn", side_effect=auth_error):
            resp = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"content": "Hello"},
                headers=headers,
            )

        assert resp.status_code == 503
        assert "invalid" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_send_message_generic_error(self, client, db, admin_user):
        """POST /messages with unexpected error → 502."""
        user, headers = admin_user
        await db.execute(text(f"SET LOCAL app.current_tenant_id = '{user.tenant_id}'"))

        create_resp = await client.post("/api/v1/chat/sessions", json={"title": "Generic Error"}, headers=headers)
        session_id = create_resp.json()["id"]

        with patch("app.api.v1.chat.run_chat_turn", side_effect=RuntimeError("Unexpected")):
            resp = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"content": "Hello"},
                headers=headers,
            )

        assert resp.status_code == 502
        assert "temporarily unavailable" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# B4: Health endpoint
# ---------------------------------------------------------------------------


class TestChatHealth:
    @pytest.mark.asyncio
    async def test_health_configured(self, client):
        """GET /chat/health returns configured=true when keys are set."""
        with patch("app.api.v1.chat.settings") as mock_settings:
            mock_settings.ANTHROPIC_API_KEY = "sk-test-key"
            mock_settings.VOYAGE_API_KEY = "va-test-key"
            mock_settings.ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

            resp = await client.get("/api/v1/chat/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["anthropic_configured"] is True
        assert data["voyage_configured"] is True
        assert data["model"] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_health_unconfigured(self, client):
        """GET /chat/health returns configured=false when keys are empty."""
        with patch("app.api.v1.chat.settings") as mock_settings:
            mock_settings.ANTHROPIC_API_KEY = ""
            mock_settings.VOYAGE_API_KEY = ""
            mock_settings.ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

            resp = await client.get("/api/v1/chat/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["anthropic_configured"] is False
        assert data["voyage_configured"] is False

    @pytest.mark.asyncio
    async def test_health_no_auth_required(self, client):
        """GET /chat/health should work without auth."""
        with patch("app.api.v1.chat.settings") as mock_settings:
            mock_settings.ANTHROPIC_API_KEY = ""
            mock_settings.VOYAGE_API_KEY = ""
            mock_settings.ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

            resp = await client.get("/api/v1/chat/health")

        assert resp.status_code == 200
