"""Tests for Tier 2 semantic routing via Haiku classification."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig
from app.services.chat.routing.semantic_router import SemanticRouter


def _make_config(agent_id: str, desc: str) -> AgentYAMLConfig:
    return AgentYAMLConfig(
        agent_id=agent_id,
        display_name=agent_id.replace("-", " ").title(),
        description=desc,
    )


def _mock_adapter(response_text: str) -> AsyncMock:
    """Create a mock adapter that returns the given text."""
    adapter = AsyncMock()
    mock_response = MagicMock()
    mock_response.text_blocks = [response_text]
    mock_response.tool_use_blocks = []
    mock_response.usage = MagicMock(
        input_tokens=100,
        output_tokens=10,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    adapter.create_message = AsyncMock(return_value=mock_response)
    return adapter


class TestSemanticRouter:
    @pytest.mark.asyncio
    async def test_returns_agent_id_from_llm(self):
        adapter = _mock_adapter("pricing-agent")
        agents = [_make_config("pricing-agent", "Handles pricing queries")]
        router = SemanticRouter()
        result = await router.route("what's the price", agents, adapter)
        assert result == "pricing-agent"

    @pytest.mark.asyncio
    async def test_returns_unified_for_unclear(self):
        adapter = _mock_adapter("unified-agent")
        agents = [_make_config("pricing-agent", "Handles pricing")]
        router = SemanticRouter()
        result = await router.route("hello", agents, adapter)
        assert result == "unified-agent"

    @pytest.mark.asyncio
    async def test_prompt_includes_all_agent_descriptions(self):
        adapter = _mock_adapter("pricing-agent")
        agents = [
            _make_config("pricing-agent", "Handles pricing queries"),
            _make_config("recon-agent", "Handles reconciliation"),
            _make_config("inventory-agent", "Handles inventory lookups"),
        ]
        router = SemanticRouter()
        await router.route("test query", agents, adapter)
        # Check the prompt sent to the adapter
        call_kwargs = adapter.create_message.call_args.kwargs
        system_prompt = call_kwargs.get("system", "")
        assert "pricing" in system_prompt.lower()
        assert "reconciliation" in system_prompt.lower()
        assert "inventory" in system_prompt.lower()

    @pytest.mark.asyncio
    async def test_uses_haiku_model(self):
        adapter = _mock_adapter("pricing-agent")
        agents = [_make_config("pricing-agent", "Pricing")]
        router = SemanticRouter()
        await router.route("price check", agents, adapter)
        call_kwargs = adapter.create_message.call_args.kwargs
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_strips_whitespace_from_response(self):
        adapter = _mock_adapter("  pricing-agent\n")
        agents = [_make_config("pricing-agent", "Pricing")]
        router = SemanticRouter()
        result = await router.route("price check", agents, adapter)
        assert result == "pricing-agent"

    @pytest.mark.asyncio
    async def test_invalid_agent_id_returns_unified(self):
        adapter = _mock_adapter("nonexistent-agent")
        agents = [_make_config("pricing-agent", "Pricing")]
        router = SemanticRouter()
        result = await router.route("test", agents, adapter)
        assert result == "unified-agent"

    @pytest.mark.asyncio
    async def test_timeout_returns_unified(self):
        adapter = AsyncMock()
        adapter.create_message = AsyncMock(side_effect=TimeoutError("timeout"))
        agents = [_make_config("pricing-agent", "Pricing")]
        router = SemanticRouter()
        result = await router.route("test", agents, adapter)
        assert result == "unified-agent"
