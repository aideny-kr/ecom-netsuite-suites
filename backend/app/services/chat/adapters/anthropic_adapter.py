"""Anthropic (Claude) adapter — identity mapping since tools are already in Anthropic format."""

import asyncio
import hashlib
import json
import logging
import random
import time

import anthropic
import httpx

from app.core.config import settings
from app.services.chat import thinking as _thinking
from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, TokenUsage, ToolUseBlock
from app.services.chat.llm_purpose import current_purpose

logger = logging.getLogger(__name__)


# Adaptive thinking tokens count toward max_tokens, so give the answer room on
# adaptive-thinking turns (mirrors the headroom the legacy budget_tokens path adds).
_ADAPTIVE_MIN_MAX_TOKENS = 32768


def _apply_thinking(
    kwargs: dict,
    model: str,
    max_tokens: int,
    thinking_level: str | None,
    tool_choice: dict | str | None,
) -> None:
    """Mutate `kwargs` to enable Anthropic thinking for this turn, MODEL-GATED.

    - SUPPRESS (level none / forced tool_choice): thinking off. Thinking (either mode)
      is INCOMPATIBLE with a forced tool_choice (type tool/any) — both → HTTP 400. On
      ADAPTIVE-DEFAULT models (Sonnet 5) thinking is ON unless explicitly disabled, so
      omitting is NOT enough — we must send thinking={type:disabled}. (Legacy/Haiku
      default to no thinking when omitted.) This is load-bearing for the kill-switch,
      simple/chitchat turns, AND forced-tool turns (plan-mode clarify).
    - ADAPTIVE models (Sonnet 5, Sonnet 4.6, Opus 4.6+, Fable) → thinking={type:adaptive}
      + output_config.effort (low..xhigh). budget_tokens/temperature would 400 here.
    - LEGACY models (4.5 / 4.0 / 4.1) → thinking={type:enabled,budget_tokens} + temperature=1
      + max_tokens reserved on top of the budget. (effort would error on these.)
    - HAIKU → no thinking (unsupported).
    """
    mode = _thinking.thinking_mode(model)

    if thinking_level in (None, "none") or _thinking.is_forced_tool_choice(tool_choice):
        # Adaptive-default models (Sonnet 5) think unless explicitly disabled —
        # omitting leaves thinking ON. Legacy/Haiku are off-by-default when omitted.
        if mode == "adaptive":
            kwargs["thinking"] = {"type": "disabled"}
        return

    if mode == "adaptive":
        effort = _thinking.anthropic_effort(thinking_level, model)
        if effort is None:
            kwargs["thinking"] = {"type": "disabled"}
            return
        kwargs["thinking"] = {"type": "adaptive"}
        output_config = dict(kwargs.get("output_config") or {})
        output_config["effort"] = effort
        kwargs["output_config"] = output_config
        if max_tokens < _ADAPTIVE_MIN_MAX_TOKENS:
            kwargs["max_tokens"] = _ADAPTIVE_MIN_MAX_TOKENS
    elif mode == "legacy":
        budget = _thinking.budget_for(thinking_level)
        if budget <= 0:
            return
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        kwargs["temperature"] = 1
        kwargs["max_tokens"] = budget + max_tokens


def _extract_thinking_blocks(content) -> list[dict]:
    """Pull thinking / redacted_thinking blocks out of a message's content,
    preserving signatures — required to echo them back across tool-use turns."""
    blocks: list[dict] = []
    for block in content:
        if block.type == "thinking":
            blocks.append({"type": "thinking", "thinking": block.thinking, "signature": block.signature})
        elif block.type == "redacted_thinking":
            blocks.append({"type": "redacted_thinking", "data": block.data})
    return blocks


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


def _stable_cache_control() -> dict:
    """cache_control for the STABLE prefix (tool definitions + static system block).

    PROMPT_CACHE_STABLE_TTL="1h" keeps a tenant's prefix warm across the gaps staging
    shows (72% of turns are the first of a session and pay cold-cache prices). The
    growing conversation keeps the 5-minute auto-cache: the API requires 1h entries to
    precede 5m ones, and tools -> system -> messages is exactly that order. A write at
    1h costs 2x instead of 1.25x, so this is a measured switch, default off.
    """
    if settings.PROMPT_CACHE_STABLE_TTL == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


def _build_request_kwargs(
    *,
    model: str,
    max_tokens: int,
    system: str,
    system_dynamic: str = "",
    messages: list[dict],
    tools: list[dict] | None = None,
    tool_choice: dict | str | None = None,
    thinking_level: str | None = None,
) -> dict:
    """The one place the request is assembled, for both the blocking and streaming paths."""
    stable = _stable_cache_control()
    system_blocks = [{"type": "text", "text": system, "cache_control": stable}]
    if system_dynamic:
        system_blocks.append({"type": "text", "text": system_dynamic})
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": messages,
        # Cache the growing conversation too, including dynamic context and tool results.
        # Tool + static-system breakpoints use two of the four available slots.
        # The pinned SDK predates this top-level option; extra_body passes it to the API.
        "extra_body": {"cache_control": {"type": "ephemeral"}},
    }
    if tools:
        # Cache tool definitions — they're large and identical every step
        # The adapter owns tool breakpoints: a caller's marker on an earlier tool could
        # put a 5m entry ahead of the 1h stable one, which the API rejects.
        cached_tools = [{k: v for k, v in _to_api_tool(t).items() if k != "cache_control"} for t in tools]
        if cached_tools:
            cached_tools[-1] = {**cached_tools[-1], "cache_control": stable}
        kwargs["tools"] = cached_tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    _apply_thinking(kwargs, model, max_tokens, thinking_level, tool_choice)
    return kwargs


def _usage_from(raw) -> TokenUsage:
    return TokenUsage(
        input_tokens=raw.input_tokens,
        output_tokens=raw.output_tokens,
        cache_creation_input_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
    )


def _prefix_fingerprint(kwargs: dict) -> str:
    """12 hex chars identifying the STABLE prefix of the request actually sent: model,
    tool definitions and the cache-marked system block(s), cache_control included.

    Within the TTL, every call on a repeated fingerprint should show ``cache_read``
    of at least the prefix. ``cache_read=0`` on a fingerprint seen minutes earlier is
    the invalidator we are hunting; a fresh ``cache_write`` alone is not, because the
    growing conversation is written on every turn."""
    stable_system = [b for b in kwargs.get("system") or [] if "cache_control" in b]
    body = json.dumps(
        {"model": kwargs.get("model"), "tools": kwargs.get("tools") or [], "system": stable_system},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(body.encode()).hexdigest()[:12]


def _ttl_split(raw) -> tuple[int, int]:
    breakdown = getattr(raw, "cache_creation", None)
    if breakdown is None:
        return 0, 0
    return (
        int(getattr(breakdown, "ephemeral_5m_input_tokens", 0) or 0),
        int(getattr(breakdown, "ephemeral_1h_input_tokens", 0) or 0),
    )


def _log_usage(model: str, raw, *, stream: bool, elapsed_ms: int, kwargs: dict, retries: int = 0) -> None:
    """One line per provider call, with enough to attribute a turn's cost to its calls:
    the caller's purpose label, wall time, the stable-prefix fingerprint (repeated
    fingerprint + fresh write = invalidation) and the 5m/1h cache-write split.

    ``ms`` is the attempt that answered, SDK connection retries included; this
    adapter's own overload retries (stream path) are counted in ``retries`` and their
    backoff is excluded. On the stream path ``ms`` runs to the final message, so it
    includes time the caller spent between chunks. A stream that ends without a final
    message (deadline, cancellation) has no usage to report and emits no line.

    Printed to stdout, not logged: the app configures no handler for stdlib INFO, so a
    logger.info line never reached the container logs (0 lines on staging, 2026-09-23).
    stdout is what docker logs and the Celery worker's stdout redirect both capture."""
    usage = _usage_from(raw)
    write_5m, write_1h = _ttl_split(raw)
    print(
        f"llm.usage purpose={current_purpose()} model={model} in={usage.input_tokens} "
        f"cache_write={usage.cache_creation_input_tokens} cache_write_5m={write_5m} cache_write_1h={write_1h} "
        f"cache_read={usage.cache_read_input_tokens} out={usage.output_tokens} ms={elapsed_ms} "
        f"retries={retries} prefix={_prefix_fingerprint(kwargs)} stream={'true' if stream else 'false'}",
        flush=True,
    )


class AnthropicAdapter(BaseLLMAdapter):
    def __init__(self, api_key: str):
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=_CLIENT_TIMEOUT,
            max_retries=_CLIENT_MAX_RETRIES,
        )

    def force_tool_choice(self, tool_name: str, model: str | None = None) -> dict:
        """Build the Anthropic API tool_choice param to force a specific tool.

        Per Anthropic SDK: `tool_choice={"type": "tool", "name": "<tool_name>"}`
        forces the model's first response to be a tool_use block for that tool.
        Model-agnostic — `model` param accepted only for protocol uniformity.
        """
        if not tool_name or not isinstance(tool_name, str):
            raise ValueError(f"tool_name must be a non-empty string, got {tool_name!r}")
        return {"type": "tool", "name": tool_name}

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
        kwargs = _build_request_kwargs(
            model=model,
            max_tokens=max_tokens,
            system=system,
            system_dynamic=system_dynamic,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            thinking_level=thinking_level,
        )

        started = time.monotonic()
        response = await self._client.messages.create(**kwargs)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        text_blocks: list[str] = []
        tool_use_blocks: list[ToolUseBlock] = []

        for block in response.content:
            if block.type == "text":
                text_blocks.append(block.text)
            elif block.type == "tool_use":
                tool_use_blocks.append(ToolUseBlock(id=block.id, name=block.name, input=block.input))

        usage = _usage_from(response.usage)
        _log_usage(model, response.usage, stream=False, elapsed_ms=elapsed_ms, kwargs=kwargs)

        return LLMResponse(
            text_blocks=text_blocks,
            tool_use_blocks=tool_use_blocks,
            usage=usage,
            thinking_blocks=_extract_thinking_blocks(response.content),
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
        thinking_level: str | None = None,
    ):
        kwargs = _build_request_kwargs(
            model=model,
            max_tokens=max_tokens,
            system=system,
            system_dynamic=system_dynamic,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            thinking_level=thinking_level,
        )

        # Retry the stream open (and the first chunk) on transient overloads.
        # Once any text has been yielded we do NOT retry — partial output
        # cannot be rewound without confusing the caller.
        # One clock reading starts both the overall deadline and the first attempt; a
        # retry restarts only the attempt clock, after its backoff.
        attempt_started = time.monotonic()
        deadline = attempt_started + _STREAM_TIMEOUT_SECONDS
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
                attempt_started = time.monotonic()

        text_blocks: list[str] = []
        tool_use_blocks: list[ToolUseBlock] = []

        for block in final_message.content:
            if block.type == "text":
                text_blocks.append(block.text)
            elif block.type == "tool_use":
                tool_use_blocks.append(ToolUseBlock(id=block.id, name=block.name, input=block.input))

        usage = _usage_from(final_message.usage)
        _log_usage(
            model,
            final_message.usage,
            stream=True,
            elapsed_ms=int((time.monotonic() - attempt_started) * 1000),
            kwargs=kwargs,
            retries=attempt,
        )

        response = LLMResponse(
            text_blocks=text_blocks,
            tool_use_blocks=tool_use_blocks,
            usage=usage,
            thinking_blocks=_extract_thinking_blocks(final_message.content),
        )
        yield "response", response

    def build_tool_result_message(self, tool_results: list[dict]) -> dict:
        return {"role": "user", "content": tool_results}

    def build_assistant_message(self, response: LLMResponse) -> dict:
        content: list[dict] = []
        # Thinking blocks MUST come first when present (required for tool-use
        # continuation in a thinking-enabled turn).
        content.extend(response.thinking_blocks)
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
