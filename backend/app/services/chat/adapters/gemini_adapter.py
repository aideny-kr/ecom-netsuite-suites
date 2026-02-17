"""Google Gemini adapter — translates between Anthropic tool format and Gemini's API."""

import uuid

from google import genai
from google.genai import types as genai_types

from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage, ToolUseBlock


class GeminiAdapter(BaseLLMAdapter):
    def __init__(self, api_key: str):
        self._client = genai.Client(api_key=api_key)

    def _convert_tools(self, tools: list[dict]) -> list[genai_types.Tool]:
        """Convert Anthropic tool format to Gemini FunctionDeclarations."""
        declarations = []
        for tool in tools:
            schema = tool.get("input_schema", {})
            # Gemini doesn't support additionalProperties in schema
            properties = schema.get("properties", {})
            cleaned_props = {}
            for k, v in properties.items():
                cleaned_props[k] = {pk: pv for pk, pv in v.items() if pk != "additionalProperties"}

            declarations.append(genai_types.FunctionDeclaration(
                name=tool["name"],
                description=tool.get("description", ""),
                parameters={
                    "type": "OBJECT",
                    "properties": cleaned_props,
                    "required": schema.get("required", []),
                } if cleaned_props else None,
            ))
        return [genai_types.Tool(function_declarations=declarations)]

    def _convert_messages(self, messages: list[dict]) -> list[genai_types.Content]:
        """Convert Anthropic messages to Gemini Content format."""
        gemini_contents: list[genai_types.Content] = []

        for msg in messages:
            role = msg["role"]
            content = msg.get("content")

            # Map roles: Gemini uses "user" and "model"
            gemini_role = "model" if role == "assistant" else "user"

            if isinstance(content, str):
                gemini_contents.append(genai_types.Content(
                    role=gemini_role,
                    parts=[genai_types.Part.from_text(text=content)],
                ))
            elif isinstance(content, list):
                parts: list[genai_types.Part] = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(genai_types.Part.from_text(text=block["text"]))
                        elif block.get("type") == "tool_use":
                            parts.append(genai_types.Part.from_function_call(
                                name=block["name"],
                                args=block["input"],
                            ))
                        elif block.get("type") == "tool_result":
                            parts.append(genai_types.Part.from_function_response(
                                name=block.get("tool_name", "tool"),
                                response={"result": block.get("content", "")},
                            ))
                if parts:
                    gemini_contents.append(genai_types.Content(
                        role=gemini_role,
                        parts=parts,
                    ))

        return gemini_contents

    async def create_message(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        gemini_contents = self._convert_messages(messages)

        config = genai_types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        )
        if tools:
            config.tools = self._convert_tools(tools)

        response = await self._client.aio.models.generate_content(
            model=model,
            contents=gemini_contents,
            config=config,
        )

        text_blocks: list[str] = []
        tool_use_blocks: list[ToolUseBlock] = []

        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.text:
                    text_blocks.append(part.text)
                elif part.function_call:
                    tool_use_blocks.append(
                        ToolUseBlock(
                            id=str(uuid.uuid4()),  # Gemini doesn't provide tool_call IDs
                            name=part.function_call.name,
                            input=dict(part.function_call.args) if part.function_call.args else {},
                        )
                    )

        usage = TokenUsage(
            input_tokens=response.usage_metadata.prompt_token_count if response.usage_metadata else 0,
            output_tokens=response.usage_metadata.candidates_token_count if response.usage_metadata else 0,
        )

        return LLMResponse(
            text_blocks=text_blocks,
            tool_use_blocks=tool_use_blocks,
            usage=usage,
        )

    def build_tool_result_message(self, tool_results: list[dict]) -> dict:
        # Store in Anthropic format with extra tool_name for Gemini conversion
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
