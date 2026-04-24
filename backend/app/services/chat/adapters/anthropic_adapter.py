"""Anthropic (Claude) adapter — identity mapping since tools are already in Anthropic format."""

import asyncio
import logging
import random
import time

import anthropic
import httpx

from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage, ToolUseBlock

logger = logging.getLogger(__name__)

# Wall-clock deadline for a single stream_message call — PER LLM HOP, not per
# turn. Each tool-use step in `base_agent.py` opens a fresh stream with its own
# deadline; the outer 300s `_BACKGROUND_TASK_TIMEOUT` in `api/v1/chat.py` bounds
# the whole turn (context gather + N hops + tool exec). Sized to fit (10+30+60)s
# worst-case overload backoff plus a stream attempt, without implying that a
# multi-hop turn has 180s * N of headroom — it doesn't.
_STREAM_TIMEOUT_SECONDS = 180  # 3 minutes per hop

# Per-request socket timeouts. The SDK default is read=600s, which means a
# single stalled request (TCP open, no bytes flowing) can eat the entire
# 300s chat budget before the outer asyncio.wait_for kills it — producing a
# blank-screen timeout with no user-facing progress. read=60s is 6–100× the
# typical Haiku/Sonnet response, so it only trips on actual hangs, and
# max_retries=2 turns a transient stall into a ~1s retry instead of a dead turn.
_CLIENT_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=60.0)
_CLIENT_MAX_RETRIES = 2

# Per-error-type backoff schedule. Overload pools empirically recover in 30–120s,
# so short (1, 2, 4)s retries almost always land on the same overloaded pool and
# all fail. Rate limits carry a Retry-After header we honour directly when present.
_OVERLOAD_BACKOFF_SECONDS = (10.0, 30.0, 60.0)
_RATE_LIMIT_BACKOFF_SECONDS = (5.0, 15.0, 30.0)
_GENERIC_BACKOFF_SECONDS = (1.0, 2.0, 4.0)

# Uniform ±25% jitter prevents multiple workers from retrying in lockstep and
# thundering-herding the recovered pool.
_JITTER_MIN = 0.75
_JITTER_MAX = 1.25

# Cap a misbehaving upstream's Retry-After so a 3600s value doesn't hang the turn.
_MAX_RETRY_AFTER_SECONDS = 120.0

# Leave this much of the deadline budget for the actual stream attempt after a sleep.
_RETRY_BUDGET_SLACK_SECONDS = 5.0

# Tool dicts in this codebase carry internal-only fields like `category` (stamped
# by `tool_categories.categorize()`). Anthropic rejects unknown keys with
# `tools.0.custom.<field>: Extra inputs are not permitted`, so the adapter
# allowlists API-recognised keys before sending.
_ANTHROPIC_TOOL_API_KEYS = {"name", "description", "input_schema", "cache_control", "type"}


def _to_api_tool(tool: dict) -> dict:
    return {k: v for k, v in tool.items() if k in _ANTHROPIC_TOOL_API_KEYS}


def _jitter(delay: float) -> float:
    return delay * random.uniform(_JITTER_MIN, _JITTER_MAX)


def _retry_after_seconds(exc: anthropic.APIStatusError) -> float | None:
    """Parse the Retry-After header (RFC 7231 seconds form) off a rate-limit error."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return min(value, _MAX_RETRY_AFTER_SECONDS)


def _classify_error(exc: anthropic.APIStatusError) -> str | None:
    """Return 'overloaded' | 'rate_limit' | 'generic' | None (non-retryable)."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") or {}
        if isinstance(err, dict):
            t = err.get("type")
            if t == "overloaded_error":
                return "overloaded"
            if t == "rate_limit_error":
                return "rate_limit"
            if t == "api_error":
                return "generic"
    status = getattr(exc, "status_code", None)
    if status in {503, 529}:
        return "overloaded"
    if status == 429:
        return "rate_limit"
    if status is not None and 500 <= status < 600:
        return "generic"
    return None


def _compute_retry_delay(kind: str, attempt: int, exc: anthropic.APIStatusError) -> float | None:
    """Jittered delay for this attempt, or None when retries are exhausted."""
    if kind == "overloaded":
        if attempt >= len(_OVERLOAD_BACKOFF_SECONDS):
            return None
        return _jitter(_OVERLOAD_BACKOFF_SECONDS[attempt])
    if kind == "rate_limit":
        header_delay = _retry_after_seconds(exc)
        if header_delay is not None:
            return _jitter(header_delay)
        if attempt >= len(_RATE_LIMIT_BACKOFF_SECONDS):
            return None
        return _jitter(_RATE_LIMIT_BACKOFF_SECONDS[attempt])
    if kind == "generic":
        if attempt >= len(_GENERIC_BACKOFF_SECONDS):
            return None
        return _jitter(_GENERIC_BACKOFF_SECONDS[attempt])
    return None


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
            cached_tools = [_to_api_tool(t) for t in tools]
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
            cached_tools = [_to_api_tool(t) for t in tools]
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
                if first_chunk_received:
                    raise
                kind = _classify_error(exc)
                if kind is None:
                    raise
                delay = _compute_retry_delay(kind, attempt, exc)
                if delay is None:
                    raise
                remaining = deadline - time.monotonic()
                if delay > remaining - _RETRY_BUDGET_SLACK_SECONDS:
                    logger.warning(
                        "anthropic_adapter.retry_abandoned kind=%s attempt=%d delay=%.1fs remaining=%.1fs",
                        kind,
                        attempt,
                        delay,
                        remaining,
                    )
                    raise
                attempt += 1
                logger.warning(
                    "anthropic stream %s error, retry %d after %.1fs (request_id=%s)",
                    kind,
                    attempt,
                    delay,
                    getattr(exc, "request_id", "?"),
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
