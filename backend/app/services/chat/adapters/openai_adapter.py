"""OpenAI adapter — translates between Anthropic tool format and OpenAI's function calling."""

import json

import openai

from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage, ToolUseBlock


class OpenAIAdapter(BaseLLMAdapter):
    def __init__(self, api_key: str):
        self._client = openai.AsyncOpenAI(api_key=api_key)

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """Convert Anthropic tool format to OpenAI function format."""
        openai_tools = []
        for tool in tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        return openai_tools

    def _convert_messages(self, messages: list[dict], system: str) -> list[dict]:
        """Convert Anthropic message format to OpenAI format."""
        openai_messages: list[dict] = [{"role": "system", "content": system}]

        for msg in messages:
            role = msg["role"]
            content = msg.get("content")

            if role == "user" and isinstance(content, list):
                # Could be tool results or mixed content
                tool_results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"]
                if tool_results:
                    for tr in tool_results:
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tr["tool_use_id"],
                            "content": tr.get("content", ""),
                        })
                    continue

            if role == "assistant" and isinstance(content, list):
                # Extract text and tool_use blocks
                text_parts = []
                tool_calls = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block["text"])
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "id": block["id"],
                                "type": "function",
                                "function": {
                                    "name": block["name"],
                                    "arguments": json.dumps(block["input"]),
                                },
                            })

                assistant_msg: dict = {"role": "assistant"}
                if text_parts:
                    assistant_msg["content"] = "\n".join(text_parts)
                else:
                    assistant_msg["content"] = None
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                openai_messages.append(assistant_msg)
                continue

            # Simple text message
            openai_messages.append({"role": role, "content": content if isinstance(content, str) else str(content)})

        return openai_messages

    async def create_message(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        openai_messages = self._convert_messages(messages, system)

        kwargs: dict = {
            "model": model,
            "max_completion_tokens": max_tokens,
            "messages": openai_messages,
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        response = await self._client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        text_blocks: list[str] = []
        tool_use_blocks: list[ToolUseBlock] = []

        if choice.message.content:
            text_blocks.append(choice.message.content)

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_use_blocks.append(
                    ToolUseBlock(
                        id=tc.id,
                        name=tc.function.name,
                        input=json.loads(tc.function.arguments),
                    )
                )

        usage = TokenUsage(
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
        )

        return LLMResponse(
            text_blocks=text_blocks,
            tool_use_blocks=tool_use_blocks,
            usage=usage,
        )

    def build_tool_result_message(self, tool_results: list[dict]) -> dict:
        # OpenAI expects tool results as the user message in Anthropic format,
        # but we store them in Anthropic format and convert in _convert_messages
        return {"role": "user", "content": tool_results}

    def build_assistant_message(self, response: LLMResponse) -> dict:
        content: list[dict] = []
        for text in response.text_blocks:
            content.append({"type": "text", "text": text})
        for tool in response.tool_use_blocks:
            content.append({
                "type": "tool_use",
                "id": tool.id,
                "name": tool.name,
                "input": tool.input,
            })
        return {"role": "assistant", "content": content}
