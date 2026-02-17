"""Tests for the LLM adapter layer — factory, format translation, response normalization."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.llm_adapter import (
    DEFAULT_MODELS,
    VALID_MODELS,
    VALID_PROVIDERS,
    LLMResponse,
    TokenUsage,
    ToolUseBlock,
    get_adapter,
)

# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


class TestGetAdapter:
    def test_returns_anthropic_adapter(self):
        adapter = get_adapter("anthropic", "sk-test")
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter
        assert isinstance(adapter, AnthropicAdapter)

    def test_returns_openai_adapter(self):
        adapter = get_adapter("openai", "sk-test")
        from app.services.chat.adapters.openai_adapter import OpenAIAdapter
        assert isinstance(adapter, OpenAIAdapter)

    def test_returns_gemini_adapter(self):
        adapter = get_adapter("gemini", "test-key")
        from app.services.chat.adapters.gemini_adapter import GeminiAdapter
        assert isinstance(adapter, GeminiAdapter)

    def test_invalid_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            get_adapter("mistral", "key")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_valid_providers(self):
        assert VALID_PROVIDERS == {"anthropic", "openai", "gemini"}

    def test_all_providers_have_default_model(self):
        for provider in VALID_PROVIDERS:
            assert provider in DEFAULT_MODELS

    def test_all_providers_have_model_list(self):
        for provider in VALID_PROVIDERS:
            assert provider in VALID_MODELS
            assert len(VALID_MODELS[provider]) > 0


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------


class TestAnthropicAdapter:
    @pytest.mark.asyncio
    async def test_text_response(self):
        adapter = get_adapter("anthropic", "sk-test")

        mock_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Hello!"
        mock_response.content = [text_block]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)

        with patch.object(adapter._client.messages, "create", new_callable=AsyncMock, return_value=mock_response):
            result = await adapter.create_message(
                model="claude-sonnet-4-20250514",
                max_tokens=100,
                system="test",
                messages=[{"role": "user", "content": "Hi"}],
            )

        assert result.text_blocks == ["Hello!"]
        assert result.tool_use_blocks == []
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5

    @pytest.mark.asyncio
    async def test_tool_use_response(self):
        adapter = get_adapter("anthropic", "sk-test")

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "tool_1"
        tool_block.name = "search"
        tool_block.input = {"query": "test"}

        mock_response = MagicMock()
        mock_response.content = [tool_block]
        mock_response.usage = MagicMock(input_tokens=20, output_tokens=10)

        with patch.object(adapter._client.messages, "create", new_callable=AsyncMock, return_value=mock_response):
            result = await adapter.create_message(
                model="claude-sonnet-4-20250514",
                max_tokens=100,
                system="test",
                messages=[{"role": "user", "content": "Search"}],
            )

        assert len(result.tool_use_blocks) == 1
        assert result.tool_use_blocks[0].name == "search"
        assert result.tool_use_blocks[0].input == {"query": "test"}

    def test_build_assistant_message(self):
        adapter = get_adapter("anthropic", "sk-test")
        response = LLMResponse(
            text_blocks=["hello"],
            tool_use_blocks=[ToolUseBlock(id="t1", name="search", input={"q": "x"})],
        )
        msg = adapter.build_assistant_message(response)
        assert msg["role"] == "assistant"
        assert len(msg["content"]) == 2
        assert msg["content"][0] == {"type": "text", "text": "hello"}
        assert msg["content"][1]["type"] == "tool_use"

    def test_build_tool_result_message(self):
        adapter = get_adapter("anthropic", "sk-test")
        results = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
        msg = adapter.build_tool_result_message(results)
        assert msg["role"] == "user"
        assert msg["content"] == results


# ---------------------------------------------------------------------------
# OpenAI adapter — message conversion
# ---------------------------------------------------------------------------


class TestOpenAIAdapter:
    def test_convert_tools(self):
        adapter = get_adapter("openai", "sk-test")
        tools = [{
            "name": "search",
            "description": "Search data",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }]
        converted = adapter._convert_tools(tools)
        assert len(converted) == 1
        assert converted[0]["type"] == "function"
        assert converted[0]["function"]["name"] == "search"
        assert converted[0]["function"]["parameters"]["properties"]["q"]["type"] == "string"

    def test_convert_simple_messages(self):
        adapter = get_adapter("openai", "sk-test")
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        converted = adapter._convert_messages(messages, "You are helpful")
        assert converted[0]["role"] == "system"
        assert converted[0]["content"] == "You are helpful"
        assert converted[1]["role"] == "user"
        assert converted[2]["role"] == "assistant"

    def test_convert_tool_results(self):
        adapter = get_adapter("openai", "sk-test")
        messages = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "result data"},
            ]},
        ]
        converted = adapter._convert_messages(messages, "sys")
        # system + tool result
        assert len(converted) == 2
        assert converted[1]["role"] == "tool"
        assert converted[1]["tool_call_id"] == "call_1"

    @pytest.mark.asyncio
    async def test_text_response(self):
        adapter = get_adapter("openai", "sk-test")

        mock_choice = MagicMock()
        mock_choice.message.content = "Hi there"
        mock_choice.message.tool_calls = None

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=8)

        with patch.object(
            adapter._client.chat.completions, "create",
            new_callable=AsyncMock, return_value=mock_response,
        ):
            result = await adapter.create_message(
                model="gpt-4o",
                max_tokens=100,
                system="test",
                messages=[{"role": "user", "content": "Hi"}],
            )

        assert result.text_blocks == ["Hi there"]
        assert result.usage.input_tokens == 15
        assert result.usage.output_tokens == 8

    @pytest.mark.asyncio
    async def test_tool_call_response(self):
        adapter = get_adapter("openai", "sk-test")

        tool_call = MagicMock()
        tool_call.id = "call_abc"
        tool_call.function.name = "search"
        tool_call.function.arguments = '{"q": "test"}'

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.tool_calls = [tool_call]

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=10)

        with patch.object(
            adapter._client.chat.completions, "create",
            new_callable=AsyncMock, return_value=mock_response,
        ):
            result = await adapter.create_message(
                model="gpt-4o",
                max_tokens=100,
                system="test",
                messages=[{"role": "user", "content": "Search"}],
            )

        assert len(result.tool_use_blocks) == 1
        assert result.tool_use_blocks[0].id == "call_abc"
        assert result.tool_use_blocks[0].name == "search"
        assert result.tool_use_blocks[0].input == {"q": "test"}


# ---------------------------------------------------------------------------
# Gemini adapter
# ---------------------------------------------------------------------------


class TestGeminiAdapter:
    def test_convert_tools(self):
        adapter = get_adapter("gemini", "test-key")
        tools = [{
            "name": "search",
            "description": "Search data",
            "input_schema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        }]
        converted = adapter._convert_tools(tools)
        assert len(converted) == 1
        # Should be a genai Tool with function_declarations
        assert hasattr(converted[0], "function_declarations")

    def test_build_assistant_message(self):
        adapter = get_adapter("gemini", "test-key")
        response = LLMResponse(
            text_blocks=["result"],
            tool_use_blocks=[ToolUseBlock(id="t1", name="fn", input={"k": "v"})],
        )
        msg = adapter.build_assistant_message(response)
        assert msg["role"] == "assistant"
        assert len(msg["content"]) == 2


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_token_usage_defaults(self):
        usage = TokenUsage()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_llm_response_defaults(self):
        resp = LLMResponse()
        assert resp.text_blocks == []
        assert resp.tool_use_blocks == []
        assert resp.usage.input_tokens == 0

    def test_tool_use_block(self):
        block = ToolUseBlock(id="1", name="test", input={"a": 1})
        assert block.id == "1"
        assert block.name == "test"
        assert block.input == {"a": 1}
