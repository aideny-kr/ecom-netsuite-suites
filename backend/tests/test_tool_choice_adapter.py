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


@pytest.mark.asyncio
async def test_openai_adapter_converts_tool_choice_format():
    """OpenAI uses different tool_choice format — adapter must convert."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.openai_adapter.openai") as mock_openai:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Hello"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_openai.AsyncOpenAI.return_value = mock_client

        from app.services.chat.adapters.openai_adapter import OpenAIAdapter
        adapter = OpenAIAdapter(api_key="test-key")

        tool_choice_anthropic = {"type": "tool", "name": "netsuite_suiteql"}
        tools = [{"name": "netsuite_suiteql", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

        await adapter.create_message(
            model="gpt-4o",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice=tool_choice_anthropic,
        )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["tool_choice"] == {
            "type": "function",
            "function": {"name": "netsuite_suiteql"},
        }


@pytest.mark.asyncio
async def test_openai_adapter_converts_any_to_required():
    """Anthropic's {"type": "any"} maps to OpenAI's "required"."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.openai_adapter.openai") as mock_openai:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Hello"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_openai.AsyncOpenAI.return_value = mock_client

        from app.services.chat.adapters.openai_adapter import OpenAIAdapter
        adapter = OpenAIAdapter(api_key="test-key")

        tools = [{"name": "test", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

        await adapter.create_message(
            model="gpt-4o",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice={"type": "any"},
        )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_openai_adapter_converts_auto():
    """Anthropic's {"type": "auto"} maps to OpenAI's "auto"."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.openai_adapter.openai") as mock_openai:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Hello"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_openai.AsyncOpenAI.return_value = mock_client

        from app.services.chat.adapters.openai_adapter import OpenAIAdapter
        adapter = OpenAIAdapter(api_key="test-key")

        tools = [{"name": "test", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

        await adapter.create_message(
            model="gpt-4o",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice={"type": "auto"},
        )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_gemini_adapter_converts_tool_choice_to_tool_config():
    """Gemini uses function_calling_config — adapter must convert."""
    from unittest.mock import AsyncMock, MagicMock, patch

    with patch("app.services.chat.adapters.gemini_adapter.genai") as mock_genai:
        mock_client = MagicMock()

        mock_part = MagicMock()
        mock_part.text = "Hello"
        mock_part.function_call = None
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_part]
        mock_candidate.finish_reason = MagicMock(name="STOP")
        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.usage_metadata = MagicMock(prompt_token_count=10, candidates_token_count=5)
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        mock_genai.Client.return_value = mock_client

        from app.services.chat.adapters.gemini_adapter import GeminiAdapter
        adapter = GeminiAdapter(api_key="test-key")

        tool_choice = {"type": "tool", "name": "netsuite_suiteql"}
        tools = [{"name": "netsuite_suiteql", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

        await adapter.create_message(
            model="gemini-2.0-flash",
            max_tokens=1024,
            system="test",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice=tool_choice,
        )

        call_kwargs = mock_client.aio.models.generate_content.call_args[1]
        config = call_kwargs["config"]
        assert config.tool_config is not None
