"""Tests for tool_choice parameter support across LLM adapters."""

import pytest
from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage


def test_create_message_accepts_tool_choice():
    """BaseLLMAdapter.create_message signature must accept tool_choice param."""
    import inspect
    sig = inspect.signature(BaseLLMAdapter.create_message)
    assert "tool_choice" in sig.parameters
    param = sig.parameters["tool_choice"]
    assert param.default is None


def test_stream_message_accepts_tool_choice():
    """BaseLLMAdapter.stream_message signature must accept tool_choice param."""
    import inspect
    sig = inspect.signature(BaseLLMAdapter.stream_message)
    assert "tool_choice" in sig.parameters
    param = sig.parameters["tool_choice"]
    assert param.default is None


@pytest.mark.asyncio
async def test_anthropic_adapter_passes_tool_choice_to_kwargs():
    """AnthropicAdapter should include tool_choice in API kwargs when provided."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.anthropic_adapter.anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="Hello")]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic.AsyncAnthropic.return_value = mock_client

        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter
        adapter = AnthropicAdapter(api_key="test-key")

        tool_choice = {"type": "tool", "name": "netsuite_suiteql"}
        tools = [{"name": "netsuite_suiteql", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

        await adapter.create_message(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice=tool_choice,
        )

        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["tool_choice"] == {"type": "tool", "name": "netsuite_suiteql"}


@pytest.mark.asyncio
async def test_anthropic_adapter_omits_tool_choice_when_none():
    """AnthropicAdapter should NOT include tool_choice in kwargs when None."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.anthropic_adapter.anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="Hello")]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic.AsyncAnthropic.return_value = mock_client

        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter
        adapter = AnthropicAdapter(api_key="test-key")

        await adapter.create_message(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
        )

        call_kwargs = mock_client.messages.create.call_args[1]
        assert "tool_choice" not in call_kwargs
