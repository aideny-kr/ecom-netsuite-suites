"""Google Gemini adapter — translates between Anthropic tool format and Gemini's API."""

import uuid

from google import genai
from google.genai import types as genai_types

from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage, ToolUseBlock

# See anthropic_adapter._CLIENT_TIMEOUT for rationale. google-genai uses
# milliseconds for http_options.timeout; 60000ms keeps stalled calls from
# burning the 300s chat budget.
_CLIENT_TIMEOUT_MS = 60_000

# Models that support function_calling_config.mode='ANY' for forcing a single tool.
_FCC_SUPPORTED_PREFIXES = ("gemini-1.5-", "gemini-2.", "gemini-3-")


class GeminiAdapter(BaseLLMAdapter):
    def __init__(self, api_key: str):
        self._client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(timeout=_CLIENT_TIMEOUT_MS),
        )

    def force_tool_choice(self, tool_name: str, model: str | None = None) -> dict:
        """Return the INTERNAL tool_choice shape for forcing a single tool.

        We return `{"type": "tool", "name": tool_name}` (the same shape Anthropic
        uses natively) so the orchestrator can pass a uniform value across
        providers. `create_message` translates internal → native (Gemini's
        `function_calling_config.mode='ANY'` + `allowed_function_names=[...]`)
        at the SDK call site. See `test_gemini_force_tool_choice_reaches_api_kwargs`
        for the end-to-end contract.

        Returning Gemini-native shape here would never match `create_message`'s
        `tc_type == "tool"` branch, so the kwarg would silently be dropped (the
        original P2 bug).

        Only Gemini 1.5+, 2.x, and 3.x support function_calling_config.mode='ANY';
        we still gate by model version here so PlanModeUnsupportedError fires
        before the request goes out — the orchestrator can then disable Plan
        Mode for the turn instead of hitting an API error later.
        """
        from app.services.chat.plan_mode.errors import PlanModeUnsupportedError

        if not tool_name or not isinstance(tool_name, str):
            raise ValueError(f"tool_name must be a non-empty string, got {tool_name!r}")
        if model is None:
            raise PlanModeUnsupportedError(
                "gemini",
                reason="model required to determine function_calling_config support",
            )
        if not any(model.startswith(p) for p in _FCC_SUPPORTED_PREFIXES):
            raise PlanModeUnsupportedError(
                model,
                reason="function_calling_config requires Gemini 1.5+",
            )
        return {"type": "tool", "name": tool_name}

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

            declarations.append(
                genai_types.FunctionDeclaration(
                    name=tool["name"],
                    description=tool.get("description", ""),
                    parameters={
                        "type": "OBJECT",
                        "properties": cleaned_props,
                        "required": schema.get("required", []),
                    }
                    if cleaned_props
                    else None,
                )
            )
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
                gemini_contents.append(
                    genai_types.Content(
                        role=gemini_role,
                        parts=[genai_types.Part.from_text(text=content)],
                    )
                )
            elif isinstance(content, list):
                parts: list[genai_types.Part] = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(genai_types.Part.from_text(text=block["text"]))
                        elif block.get("type") == "tool_use":
                            parts.append(
                                genai_types.Part.from_function_call(
                                    name=block["name"],
                                    args=block["input"],
                                )
                            )
                        elif block.get("type") == "tool_result":
                            parts.append(
                                genai_types.Part.from_function_response(
                                    name=block.get("tool_name", "tool"),
                                    response={"result": block.get("content", "")},
                                )
                            )
                if parts:
                    gemini_contents.append(
                        genai_types.Content(
                            role=gemini_role,
                            parts=parts,
                        )
                    )

        return gemini_contents

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
        thinking_level: str | None = None,
    ) -> LLMResponse:
        # thinking_level accepted for interface parity (BaseLLMAdapter declares it
        # and the agent loop passes it to whatever adapter it holds). Gemini native
        # extended thinking (thinking_config) is a follow-up; ignored here so a
        # Gemini-BYOK tenant doesn't TypeError on the kwarg.
        _ = thinking_level
        gemini_contents = self._convert_messages(messages)
        full_system = f"{system}\n\n{system_dynamic}".strip() if system_dynamic else system

        config = genai_types.GenerateContentConfig(
            system_instruction=full_system,
            max_output_tokens=max_tokens,
        )
        if tools:
            config.tools = self._convert_tools(tools)

        if tool_choice is not None:
            tc_type = tool_choice.get("type") if isinstance(tool_choice, dict) else tool_choice
            if tc_type == "tool" and isinstance(tool_choice, dict):
                config.tool_config = genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(
                        mode="ANY",
                        allowed_function_names=[tool_choice["name"]],
                    )
                )
            elif tc_type == "any":
                config.tool_config = genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(mode="ANY")
                )
            elif tc_type == "none":
                config.tool_config = genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(mode="NONE")
                )

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
            content.append(
                {
                    "type": "tool_use",
                    "id": tool.id,
                    "name": tool.name,
                    "input": tool.input,
                }
            )
        return {"role": "assistant", "content": content}
