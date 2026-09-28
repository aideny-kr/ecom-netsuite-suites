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

# Beta that returns only Sonnet 5.5's progress updates in thinking blocks (reasoning stays
# omitted), so the chat keeps showing what the model says between tool calls.
_PROGRESS_UPDATES_BETA = "thinking-display-updates-2026-08-18"


def _forced_tool_instruction(tool_choice) -> str:
    """The prompt line that replaces a forced tool_choice on a model that cannot be forced."""
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":
        return f"Respond only by calling the {tool_choice['name']} tool."
    return "Respond only by calling one of the provided tools."


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
    - SONNET 5.5 → "off" is thinking={type:between_tools} (disabled is a 400), and "on" asks
      for display="updates": the notes Sonnet 5 wrote as text between tool calls now come
      back as thinking blocks, empty unless requested.
    """
    mode = _thinking.thinking_mode(model)
    between_tools = _thinking.uses_between_tools(model)
    off = {"type": "between_tools"} if between_tools else {"type": "disabled"}

    if thinking_level in (None, "none") or _thinking.is_forced_tool_choice(tool_choice):
        # Adaptive-default models (Sonnet 5) think unless explicitly disabled —
        # omitting leaves thinking ON. Legacy/Haiku are off-by-default when omitted.
        if mode == "adaptive":
            kwargs["thinking"] = off
        return

    if mode == "adaptive":
        effort = _thinking.anthropic_effort(thinking_level, model)
        if effort is None:
            kwargs["thinking"] = off
            return
        kwargs["thinking"] = {"type": "adaptive"}
        if between_tools:
            kwargs["thinking"]["display"] = "updates"
            headers = dict(kwargs.get("extra_headers") or {})
            betas = [b for b in (headers.get("anthropic-beta") or "").split(",") if b]
            headers["anthropic-beta"] = ",".join([*betas, _PROGRESS_UPDATES_BETA])
            kwargs["extra_headers"] = headers
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


async def _chat_text(stream, progress_in_thinking: bool):
    """What the chat shows while one step streams. Sonnet 5 and earlier: the text deltas.
    Sonnet 5.5 also writes its notes between tool calls as thinking blocks; with
    display="updates" (or between_tools) those carry only the progress updates, never the
    reasoning, so they are shown as Sonnet 5's text between tool calls was."""
    if not progress_in_thinking:
        async for text in stream.text_stream:
            yield text
        return
    new_block = shown = False
    async for event in stream:
        if event.type == "content_block_start":
            new_block = True
            continue
        if event.type == "text":
            piece = event.text
        elif event.type == "thinking":
            piece = event.thinking
        else:
            piece = None
        if not piece:
            continue
        if new_block and shown:
            yield "\n\n"
        new_block, shown = False, True
        yield piece


def _progress_updates_requested(kwargs: dict) -> bool:
    """Whether this request's thinking blocks carry only progress updates, never reasoning:
    between_tools, or adaptive with display="updates" (both Sonnet 5.5 only). Anything else
    keeps thinking out of the chat."""
    thinking = kwargs.get("thinking") or {}
    return thinking.get("type") == "between_tools" or thinking.get("display") == "updates"


def _response_from(message, *, progress: bool) -> LLMResponse:
    """One parse for the blocking and streaming paths, keeping the order the blocks arrived
    in: a Sonnet 5.5 thinking block is signed over everything before it, so a replay that
    moves it can be rejected."""
    text_blocks: list[str] = []
    tool_use_blocks: list[ToolUseBlock] = []
    order: list[tuple[str, int]] = []
    thinking_blocks = _extract_thinking_blocks(message.content)
    thinking_seen = 0
    for block in message.content:
        if block.type == "text":
            order.append(("text", len(text_blocks)))
            text_blocks.append(block.text)
        elif block.type == "tool_use":
            order.append(("tool_use", len(tool_use_blocks)))
            tool_use_blocks.append(ToolUseBlock(id=block.id, name=block.name, input=block.input))
        elif block.type in ("thinking", "redacted_thinking"):
            order.append(("thinking", thinking_seen))
            thinking_seen += 1
    if progress and not text_blocks and not tool_use_blocks:
        # A note with no call after it is the answer: the chat already showed it, so it is
        # also what is saved and carried into the next turn.
        notes = [b["thinking"] for b in thinking_blocks if b.get("thinking")]
        if notes:
            text_blocks = ["\n\n".join(notes)]
    return LLMResponse(
        text_blocks=text_blocks,
        tool_use_blocks=tool_use_blocks,
        usage=_usage_from(message.usage),
        thinking_blocks=thinking_blocks,
        content_order=order,
    )


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
    if _thinking.uses_between_tools(model) and _thinking.is_forced_tool_choice(tool_choice):
        # Sonnet 5.5 rejects type tool/any with a 400. Every caller already treats a reply
        # without the expected tool call as a failed call, so ask in the prompt instead;
        # callers that can do without forcing are told so by force_tool_choice.
        kwargs["tool_choice"] = {"type": "auto"}
        kwargs["system"] = [*system_blocks, {"type": "text", "text": _forced_tool_instruction(tool_choice)}]
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
        Sonnet 5.5 cannot be forced (type tool/any is a 400): its callers get
        PlanModeUnsupportedError and take their no-forcing path (JSON routing, no Plan Mode).
        """
        if not tool_name or not isinstance(tool_name, str):
            raise ValueError(f"tool_name must be a non-empty string, got {tool_name!r}")
        if _thinking.uses_between_tools(model):
            # Imported here: the plan_mode package imports the orchestrator.
            from app.services.chat.plan_mode.errors import PlanModeUnsupportedError

            raise PlanModeUnsupportedError("anthropic", f"{model} does not support forced tool use")
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
        _log_usage(model, response.usage, stream=False, elapsed_ms=elapsed_ms, kwargs=kwargs)
        return _response_from(response, progress=_progress_updates_requested(kwargs))

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
        progress_in_thinking = _progress_updates_requested(kwargs)
        while True:
            try:
                async with self._client.messages.stream(**kwargs) as stream:
                    async for text in _chat_text(stream, progress_in_thinking):
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

        _log_usage(
            model,
            final_message.usage,
            stream=True,
            elapsed_ms=int((time.monotonic() - attempt_started) * 1000),
            kwargs=kwargs,
            retries=attempt,
        )

        yield "response", _response_from(final_message, progress=progress_in_thinking)

    def build_tool_result_message(self, tool_results: list[dict]) -> dict:
        return {"role": "user", "content": tool_results}

    def build_assistant_message(self, response: LLMResponse) -> dict:
        def tool_use(tool):
            return {"type": "tool_use", "id": tool.id, "name": tool.name, "input": tool.input}

        order = response.content_order
        counts = {"thinking": len(response.thinking_blocks), "tool_use": len(response.tool_use_blocks)}
        if (
            order
            and all(sorted(i for kind, i in order if kind == key) == list(range(n)) for key, n in counts.items())
            and all(i < len(response.text_blocks) for kind, i in order if kind == "text")
        ):
            # Replay exactly as returned: a Sonnet 5.5 thinking block is signed over what
            # precedes it, so [note, call, note, call] must not become [note, note, call, call].
            render = {
                "thinking": lambda i: response.thinking_blocks[i],
                "text": lambda i: {"type": "text", "text": response.text_blocks[i]},
                "tool_use": lambda i: tool_use(response.tool_use_blocks[i]),
            }
            return {"role": "assistant", "content": [render[kind](i) for kind, i in order]}
        content: list[dict] = []
        # Thinking blocks MUST come first when present (required for tool-use
        # continuation in a thinking-enabled turn).
        content.extend(response.thinking_blocks)
        for text in response.text_blocks:
            content.append({"type": "text", "text": text})
        for tool in response.tool_use_blocks:
            content.append(tool_use(tool))
        return {"role": "assistant", "content": content}
