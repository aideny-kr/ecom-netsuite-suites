"""Tests for multi-provider orchestrator integration — adapter resolution, token tracking, fallback."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.llm_adapter import LLMResponse, TokenUsage, ToolUseBlock
from app.services.chat.orchestrator import run_chat_turn

_ORCH = "app.services.chat.orchestrator"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_response(
    text: str | None = None,
    tool_blocks: list[ToolUseBlock] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> LLMResponse:
    return LLMResponse(
        text_blocks=[text] if text else [],
        tool_use_blocks=tool_blocks or [],
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _make_session(tenant_id: uuid.UUID):
    session = MagicMock()
    session.id = uuid.uuid4()
    session.title = None
    session.messages = []
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMultiProviderOrchestrator:
    """Test that the orchestrator works via the adapter layer."""

    @pytest.mark.asyncio
    async def test_text_response_with_tokens(self):
        """Text-only response populates token fields on ChatMessage."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = _make_session(tenant_id)

        text_response = _make_llm_response(text="Here are your orders.", input_tokens=50, output_tokens=30)

        mock_adapter = MagicMock()
        mock_adapter.create_message = AsyncMock(return_value=text_response)

        db = AsyncMock(spec=AsyncSession)
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        with (
            patch(
                f"{_ORCH}.get_tenant_ai_config",
                new_callable=AsyncMock,
                return_value=("openai", "gpt-4o", "sk-test", True),
            ),
            patch(f"{_ORCH}.get_adapter", return_value=mock_adapter),
            patch(f"{_ORCH}.retriever_node", new_callable=AsyncMock),
            patch(f"{_ORCH}.build_all_tool_definitions", new_callable=AsyncMock, return_value=[]),
            patch(f"{_ORCH}.log_event", new_callable=AsyncMock),
            patch(f"{_ORCH}.get_active_template", new_callable=AsyncMock, return_value="You are a helpful assistant."),
            patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        ):
            result = await run_chat_turn(
                db=db,
                session=session,
                user_message="Show orders",
                user_id=user_id,
                tenant_id=tenant_id,
            )

        assert result.content == "Here are your orders."
        assert result.input_tokens == 50
        assert result.output_tokens == 30
        assert result.token_count == 80
        assert result.model_used == "gpt-4o"
        assert result.provider_used == "openai"

    @pytest.mark.asyncio
    async def test_tool_call_accumulates_tokens(self):
        """Token counts accumulate across multiple loop iterations."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = _make_session(tenant_id)

        tool_response = _make_llm_response(
            tool_blocks=[ToolUseBlock(id="t1", name="search", input={"q": "test"})],
            input_tokens=100,
            output_tokens=20,
        )
        text_response = _make_llm_response(text="Found it.", input_tokens=200, output_tokens=50)

        mock_adapter = MagicMock()
        mock_adapter.create_message = AsyncMock(side_effect=[tool_response, text_response])
        mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": []})
        mock_adapter.build_tool_result_message = MagicMock(return_value={"role": "user", "content": []})

        db = AsyncMock(spec=AsyncSession)
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        with (
            patch(
                f"{_ORCH}.get_tenant_ai_config",
                new_callable=AsyncMock,
                return_value=("gemini", "gemini-2.0-flash", "key", True),
            ),
            patch(f"{_ORCH}.get_adapter", return_value=mock_adapter),
            patch(f"{_ORCH}.retriever_node", new_callable=AsyncMock),
            patch(
                f"{_ORCH}.build_all_tool_definitions",
                new_callable=AsyncMock,
                return_value=[{"name": "search", "description": "d", "input_schema": {}}],
            ),
            patch(
                f"{_ORCH}.execute_tool_call",
                new_callable=AsyncMock,
                return_value='{"ok": true}',
            ),
            patch(f"{_ORCH}.log_event", new_callable=AsyncMock),
            patch(f"{_ORCH}.get_active_template", new_callable=AsyncMock, return_value="You are a helpful assistant."),
            patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        ):
            result = await run_chat_turn(
                db=db,
                session=session,
                user_message="Search",
                user_id=user_id,
                tenant_id=tenant_id,
            )

        assert result.content == "Found it."
        assert result.input_tokens == 300  # 100 + 200
        assert result.output_tokens == 70  # 20 + 50
        assert result.token_count == 370
        assert result.provider_used == "gemini"

    @pytest.mark.asyncio
    async def test_anthropic_fallback(self):
        """When no tenant config, falls back to platform default."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = _make_session(tenant_id)

        text_response = _make_llm_response(text="Default response")

        mock_adapter = MagicMock()
        mock_adapter.create_message = AsyncMock(return_value=text_response)

        db = AsyncMock(spec=AsyncSession)
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        with (
            patch(
                f"{_ORCH}.get_tenant_ai_config",
                new_callable=AsyncMock,
                return_value=("anthropic", "claude-sonnet-4-20250514", "platform-key", False),
            ),
            patch(f"{_ORCH}.get_adapter", return_value=mock_adapter),
            patch(f"{_ORCH}.retriever_node", new_callable=AsyncMock),
            patch(f"{_ORCH}.build_all_tool_definitions", new_callable=AsyncMock, return_value=[]),
            patch(f"{_ORCH}.log_event", new_callable=AsyncMock),
            patch(f"{_ORCH}.get_active_template", new_callable=AsyncMock, return_value="You are a helpful assistant."),
            patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
        ):
            result = await run_chat_turn(
                db=db,
                session=session,
                user_message="Hello",
                user_id=user_id,
                tenant_id=tenant_id,
            )

        assert result.provider_used == "anthropic"
        assert result.model_used == "claude-sonnet-4-20250514"


class TestGetTenantAiConfig:
    """Test the get_tenant_ai_config helper."""

    @pytest.mark.asyncio
    async def test_returns_platform_defaults_when_no_config(self):
        from app.core.config import settings
        from app.services.chat.nodes import get_tenant_ai_config

        db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        original_key = settings.ANTHROPIC_API_KEY
        settings.ANTHROPIC_API_KEY = "test-platform-key"
        try:
            provider, model, key, is_byok = await get_tenant_ai_config(db, uuid.uuid4())
            assert provider == "anthropic"
            assert model == settings.ANTHROPIC_MODEL
            assert key == "test-platform-key"
            assert is_byok is False
        finally:
            settings.ANTHROPIC_API_KEY = original_key

    @pytest.mark.asyncio
    async def test_raises_when_no_key_configured(self):
        from app.core.config import settings
        from app.services.chat.nodes import get_tenant_ai_config

        db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        original_key = settings.ANTHROPIC_API_KEY
        settings.ANTHROPIC_API_KEY = ""
        try:
            with pytest.raises(ValueError, match="No AI provider configured"):
                await get_tenant_ai_config(db, uuid.uuid4())
        finally:
            settings.ANTHROPIC_API_KEY = original_key

    @pytest.mark.asyncio
    async def test_returns_tenant_config_when_set(self):
        from app.core.encryption import encrypt_credentials
        from app.services.chat.nodes import get_tenant_ai_config

        config = MagicMock()
        config.ai_provider = "openai"
        config.ai_model = "gpt-4o"
        config.ai_api_key_encrypted = encrypt_credentials({"api_key": "sk-tenant-key"})

        db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = config
        db.execute = AsyncMock(return_value=mock_result)

        provider, model, key, is_byok = await get_tenant_ai_config(db, uuid.uuid4())
        assert provider == "openai"
        assert model == "gpt-4o"
        assert key == "sk-tenant-key"
        assert is_byok is True

    @pytest.mark.asyncio
    async def test_uses_default_model_when_none(self):
        from app.core.encryption import encrypt_credentials
        from app.services.chat.nodes import get_tenant_ai_config

        config = MagicMock()
        config.ai_provider = "gemini"
        config.ai_model = None
        config.ai_api_key_encrypted = encrypt_credentials({"api_key": "gem-key"})

        db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = config
        db.execute = AsyncMock(return_value=mock_result)

        provider, model, key, is_byok = await get_tenant_ai_config(db, uuid.uuid4())
        assert provider == "gemini"
        assert model == "gemini-2.5-flash"
        assert key == "gem-key"
        assert is_byok is True
