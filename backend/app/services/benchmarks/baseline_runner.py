"""Claude + Oracle NetSuite MCP baseline runner.

Runs a single user question against Anthropic's Claude with ONLY the
external NetSuite MCP tools and a deliberately minimal system prompt.
This is the comparison target our in-house agent must beat.

The whole point of this baseline is to measure how Claude performs with
just the Oracle-provided tool descriptions — no tenant vernacular, no
learned rules, no proven SuiteQL patterns, no schema dump. Whatever the
in-house agent contributes on top of that minimal setup must be worth
the extra cost and complexity, otherwise we should be using Claude
direct + MCP instead.

Public surface
--------------

* :class:`BaselineResult` — dataclass returned by :func:`run_baseline`.
* :func:`run_baseline` — async entry point. Builds tools, runs the
  agentic loop, returns a populated :class:`BaselineResult`.

Implementation notes
--------------------

* :func:`_build_baseline_tools` and :func:`_get_anthropic_client` are
  thin indirection layers so the unit tests can patch them without
  reaching into the real Anthropic SDK or DB.
* :func:`execute_tool_call` is imported at module level so the tests can
  patch ``app.services.benchmarks.baseline_runner.execute_tool_call``.
* The system prompt template is exposed as a module constant
  (``BASELINE_SYSTEM_PROMPT_TEMPLATE``) so a unit test can assert it
  stays small and free of internal-agent concepts.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

# Re-exported so tests can patch it via this module's namespace.
from app.services.chat.tools import execute_tool_call  # noqa: F401

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Total wall-clock budget for a single baseline run.
_TOTAL_TIMEOUT_SECONDS = 120.0

# Cap on how much of a tool result we keep in BaselineResult.tool_calls.
# The full result is still passed back to Claude — this is just for
# logging/inspection by the benchmark scorer.
_TOOL_RESULT_PREVIEW_CHARS = 1500

# Pricing in USD per million tokens. Source: Anthropic public pricing.
# Keep this table flat so future model rows are trivial to add.
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_mtok, output_per_mtok)
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-4-5-20251101": (15.0, 75.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
_DEFAULT_PRICING_KEY = "claude-sonnet-4-6"


# Deliberately tiny system prompt. Oracle's MCP tool descriptions already
# encode SuiteQL dialect rules, so we don't repeat them here. Anything we
# add to this prompt is "help" the in-house agent gets that the baseline
# does not — it would defeat the point of the comparison.
BASELINE_SYSTEM_PROMPT_TEMPLATE = (
    "You are a NetSuite assistant. You have access to Oracle's NetSuite MCP "
    "tools via the SuiteApp. Use them to answer the user's question accurately. "
    "If you need to discover a table's structure, call ns_getSuiteQLMetadata "
    "first. Do not guess column names. If a tool call fails, read the error "
    "and try a corrected call. Current date: {today}."
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class BaselineResult:
    """Outcome of a single Claude+MCP baseline run."""

    answer_text: str
    tool_calls: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    success: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _calculate_cost(*, model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute USD cost from token counts using the static pricing table.

    Unknown models fall back to sonnet pricing rather than raising — the
    benchmark should always produce a number we can compare.
    """
    in_rate, out_rate = _MODEL_PRICING.get(model, _MODEL_PRICING[_DEFAULT_PRICING_KEY])
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000.0


def _render_system_prompt() -> str:
    return BASELINE_SYSTEM_PROMPT_TEMPLATE.format(today=date.today().isoformat())


def _truncate_preview(value: Any) -> str:
    s = value if isinstance(value, str) else str(value)
    if len(s) <= _TOOL_RESULT_PREVIEW_CHARS:
        return s
    return s[:_TOOL_RESULT_PREVIEW_CHARS] + f"... [+{len(s) - _TOOL_RESULT_PREVIEW_CHARS} chars truncated]"


async def _build_baseline_tools(
    db: "AsyncSession",
    tenant_id: uuid.UUID,
) -> list[dict]:
    """Return Anthropic-format tool definitions for ALL active NetSuite MCP connectors.

    Pulls from the same code path the agent uses, so the baseline sees
    Oracle's tool descriptions verbatim (we just uncapped them upstream).
    Local tools are intentionally excluded — the baseline is "Claude with
    only Oracle's MCP tools".
    """
    from app.services.chat.tools import build_external_tool_definitions
    from app.services.mcp_connector_service import get_active_connectors_for_tenant

    connectors = await get_active_connectors_for_tenant(db, tenant_id)
    netsuite_connectors = [c for c in connectors if c.provider == "netsuite"]
    return build_external_tool_definitions(netsuite_connectors)


def _get_anthropic_client():
    """Return an AsyncAnthropic client configured from settings.

    Wrapped in a function so unit tests can patch it without importing
    anthropic.
    """
    import anthropic

    from app.core.config import settings

    return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


def _extract_text_and_tool_uses(response: Any) -> tuple[list[str], list[Any]]:
    text_blocks: list[str] = []
    tool_uses: list[Any] = []
    for block in getattr(response, "content", []) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_blocks.append(getattr(block, "text", ""))
        elif btype == "tool_use":
            tool_uses.append(block)
    return text_blocks, tool_uses


# Sentinel actor id for benchmark-originated tool calls. Audit logs etc.
# will see this id and can filter benchmark traffic out of analytics.
_BENCHMARK_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_BENCHMARK_CORRELATION_ID = "benchmark-baseline"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_baseline(
    *,
    tenant_id: uuid.UUID,
    question: str,
    model: str = "claude-sonnet-4-6",
    max_steps: int = 12,
    db: "AsyncSession",
) -> BaselineResult:
    """Run a single question against Claude + Oracle NetSuite MCP.

    Parameters
    ----------
    tenant_id:
        Tenant whose MCP connectors should provide the tools.
    question:
        The user's natural-language question.
    model:
        Anthropic model id. Defaults to ``claude-sonnet-4-6``.
    max_steps:
        Hard ceiling on the agentic loop. Each step is one Anthropic
        ``messages.create`` call. If we hit this limit while still
        emitting tool_use blocks we return ``success=False``.
    db:
        Async DB session, used only to look up MCP connectors and to
        execute tool calls (which may need their own DB access).

    Returns
    -------
    BaselineResult
        Always returns — never raises. API errors and timeouts are
        captured in ``result.error`` with ``success=False``.
    """
    start = time.monotonic()
    system_prompt = _render_system_prompt()

    result = BaselineResult(answer_text="")

    try:
        tools = await _build_baseline_tools(db, tenant_id)
    except Exception as exc:  # pragma: no cover - defensive
        result.error = f"failed_to_build_tools: {exc}"
        result.latency_ms = int((time.monotonic() - start) * 1000)
        return result

    try:
        client = _get_anthropic_client()
    except Exception as exc:  # pragma: no cover - defensive
        result.error = f"failed_to_init_client: {exc}"
        result.latency_ms = int((time.monotonic() - start) * 1000)
        return result

    messages: list[dict] = [{"role": "user", "content": question}]
    final_text_blocks: list[str] = []
    last_text_blocks: list[str] = []

    for step in range(max_steps):
        # Total wall-clock guard.
        if time.monotonic() - start > _TOTAL_TIMEOUT_SECONDS:
            result.error = f"timeout: exceeded {_TOTAL_TIMEOUT_SECONDS:.0f}s wall clock"
            break

        try:
            # max_tokens bumped 8192 → 16384 on 2026-04-09. The first real
            # benchmark run showed the baseline hitting exactly 8192 on a
            # metadata-discovery step (Oracle's transaction table has 209
            # columns and the metadata response was ~7500 tokens, leaving
            # no budget for the model to write even a single sentence of
            # reasoning before being cut off). 16384 gives Claude room to
            # think between tool calls without letting a runaway answer
            # blow the budget.
            create_kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": 16384,
                "system": system_prompt,
                "messages": messages,
            }
            if tools:
                create_kwargs["tools"] = tools

            response = await client.messages.create(**create_kwargs)
        except Exception as exc:
            result.error = f"anthropic_api_error: {exc}"
            break

        # Accumulate token usage every step.
        usage = getattr(response, "usage", None)
        if usage is not None:
            result.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            result.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)

        text_blocks, tool_use_blocks = _extract_text_and_tool_uses(response)

        if text_blocks:
            last_text_blocks = text_blocks

        if not tool_use_blocks:
            # No tool requests → this is the final answer.
            final_text_blocks = text_blocks
            result.success = True
            break

        # Echo the assistant message back into history exactly as the
        # SDK delivered it. Mixing text + tool_use is allowed and we
        # preserve order so Claude sees its own reasoning.
        assistant_content: list[dict] = []
        for block in getattr(response, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                assistant_content.append({"type": "text", "text": getattr(block, "text", "")})
            elif btype == "tool_use":
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        messages.append({"role": "assistant", "content": assistant_content})

        # Run every tool call requested in this step, then feed the
        # results back as a single user turn.
        tool_results_blocks: list[dict] = []
        for tu in tool_use_blocks:
            try:
                tool_result_str = await execute_tool_call(
                    tool_name=tu.name,
                    tool_input=tu.input,
                    tenant_id=tenant_id,
                    actor_id=_BENCHMARK_ACTOR_ID,
                    correlation_id=_BENCHMARK_CORRELATION_ID,
                    db=db,
                )
            except Exception as exc:
                tool_result_str = f'{{"error": "tool_execution_failed: {exc}"}}'

            result.tool_calls.append(
                {
                    "name": tu.name,
                    "input": tu.input,
                    "result_preview": _truncate_preview(tool_result_str),
                }
            )

            tool_results_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": tool_result_str if isinstance(tool_result_str, str) else str(tool_result_str),
                }
            )

        messages.append({"role": "user", "content": tool_results_blocks})
    else:
        # Loop exhausted without break — we never saw a final
        # text-only response.
        result.error = f"max_steps exhausted ({max_steps})"

    # Whatever text Claude produced last is the best we have.
    if final_text_blocks:
        result.answer_text = "\n".join(final_text_blocks).strip()
    elif last_text_blocks:
        result.answer_text = "\n".join(last_text_blocks).strip()

    result.cost_usd = _calculate_cost(
        model=model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    result.latency_ms = int((time.monotonic() - start) * 1000)

    return result
