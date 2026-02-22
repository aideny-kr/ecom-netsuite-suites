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
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

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
        messages: list[dict],
        tools: list[dict] | None = None,
    ):
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

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
