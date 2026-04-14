"""Anthropic (Claude) adapter — identity mapping since tools are already in Anthropic format."""

import asyncio
import logging
import time

import anthropic
import httpx

from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage, ToolUseBlock

logger = logging.getLogger(__name__)

# Wall-clock deadline for a single stream_message call (seconds).
_STREAM_TIMEOUT_SECONDS = 120  # 2 minutes

# Per-request socket timeouts. The SDK default is read=600s, which means a
# single stalled request (TCP open, no bytes flowing) can eat the entire
# 300s chat budget before the outer asyncio.wait_for kills it — producing a
# blank-screen timeout with no user-facing progress. read=60s is 6–100× the
# typical Haiku/Sonnet response, so it only trips on actual hangs, and
# max_retries=2 turns a transient stall into a ~1s retry instead of a dead turn.
_CLIENT_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=60.0)
_CLIENT_MAX_RETRIES = 2

# Transient errors worth retrying with exponential backoff before any tokens stream.
_RETRYABLE_ERROR_TYPES = {"overloaded_error", "rate_limit_error", "api_error"}
_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)  # 3 retries = 4 total attempts


def _is_retryable(exc: anthropic.APIStatusError) -> bool:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict) and err.get("type") in _RETRYABLE_ERROR_TYPES:
            return True
    # Also retry on 529/503 even if body shape is unexpected
    status_code = getattr(exc, "status_code", None)
    return status_code in {503, 529}


class AnthropicAdapter(BaseLLMAdapter):
    def __init__(self, api_key: str):
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=_CLIENT_TIMEOUT,
            max_retries=_CLIENT_MAX_RETRIES,
        )

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

        # Retry the stream open (and the first chunk) on transient overloads.
        # Once any text has been yielded we do NOT retry — partial output
        # cannot be rewound without confusing the caller.
        deadline = time.monotonic() + _STREAM_TIMEOUT_SECONDS
        attempt = 0
        first_chunk_received = False
        while True:
            try:
                async with self._client.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        if time.monotonic() > deadline:
                            logger.warning(
                                "stream_message deadline exceeded (%ds)",
                                _STREAM_TIMEOUT_SECONDS,
                            )
                            return  # No "response" event — caller sees timeout
                        first_chunk_received = True
                        yield "text", text

                    # Check deadline before awaiting final_message
                    if time.monotonic() > deadline:
                        logger.warning(
                            "stream_message deadline exceeded before final_message (%ds)",
                            _STREAM_TIMEOUT_SECONDS,
                        )
                        return

                    final_message = await stream.get_final_message()
                break
            except anthropic.APIStatusError as exc:
                if first_chunk_received or not _is_retryable(exc) or attempt >= len(_RETRY_DELAYS_SECONDS):
                    raise
                delay = _RETRY_DELAYS_SECONDS[attempt]
                attempt += 1
                logger.warning(
                    "anthropic stream transient error (%s), retry %d/%d after %.1fs",
                    getattr(exc, "status_code", "?"),
                    attempt,
                    len(_RETRY_DELAYS_SECONDS),
                    delay,
                )
                await asyncio.sleep(delay)

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
