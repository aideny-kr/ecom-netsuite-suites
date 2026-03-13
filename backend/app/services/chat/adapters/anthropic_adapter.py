"""Anthropic (Claude) adapter — identity mapping since tools are already in Anthropic format."""

import anthropic

from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage, ToolUseBlock


class AnthropicAdapter(BaseLLMAdapter):
    def __init__(self, api_key: str):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def create_message(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        system_dynamic: str = "",
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
    ) -> LLMResponse:
        system_blocks = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if system_dynamic:
            system_blocks.append({"type": "text", "text": system_dynamic})

        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": messages,
        }
        if tools:
            # Cache tool definitions — they're large and identical every step
            cached_tools = list(tools)
            if cached_tools:
                cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = cached_tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        response = await self._client.messages.create(**kwargs)

        text_blocks: list[str] = []
        tool_use_blocks: list[ToolUseBlock] = []

        for block in response.content:
            if block.type == "text":
                text_blocks.append(block.text)
            elif block.type == "tool_use":
                tool_use_blocks.append(ToolUseBlock(id=block.id, name=block.name, input=block.input))

        usage = TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        )

        return LLMResponse(
            text_blocks=text_blocks,
            tool_use_blocks=tool_use_blocks,
            usage=usage,
        )

    async def stream_message(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        system_dynamic: str = "",
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
    ):
        system_blocks = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if system_dynamic:
            system_blocks.append({"type": "text", "text": system_dynamic})

        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": messages,
        }
        if tools:
            cached_tools = list(tools)
            if cached_tools:
                cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = cached_tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield "text", text

            final_message = await stream.get_final_message()

            text_blocks: list[str] = []
            tool_use_blocks: list[ToolUseBlock] = []

            for block in final_message.content:
                if block.type == "text":
                    text_blocks.append(block.text)
                elif block.type == "tool_use":
                    tool_use_blocks.append(ToolUseBlock(id=block.id, name=block.name, input=block.input))

            usage = TokenUsage(
                input_tokens=final_message.usage.input_tokens,
                output_tokens=final_message.usage.output_tokens,
                cache_creation_input_tokens=getattr(final_message.usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_input_tokens=getattr(final_message.usage, "cache_read_input_tokens", 0) or 0,
            )

            response = LLMResponse(
                text_blocks=text_blocks,
                tool_use_blocks=tool_use_blocks,
                usage=usage,
            )
            yield "response", response

    def build_tool_result_message(self, tool_results: list[dict]) -> dict:
        return {"role": "user", "content": tool_results}

    def build_assistant_message(self, response: LLMResponse) -> dict:
        content: list[dict] = []
        for text in response.text_blocks:
            content.append({"type": "text", "text": text})
        for tool in response.tool_use_blocks:
            content.append(
                {
                    "type": "tool_use",
                    "id": tool.id,
                    "name": tool.name,
                    "input": tool.input,
                }
            )
        return {"role": "assistant", "content": content}
