"""In-house UnifiedAgent runner for the benchmark harness.

Runs a single question through ``UnifiedAgent`` in-process and returns a
dataclass that mirrors ``BaselineResult`` from :mod:`baseline_runner` so
the benchmark harness can compare "our agent" vs "Claude + MCP baseline"
side-by-side on identical metrics.

This module is a DELIBERATELY MINIMAL slice of the orchestrator's agentic
path. It reuses the same context-loading helpers the orchestrator calls
(metadata, connectors, vernacular, domain knowledge, proven patterns,
learned rules) but skips everything that belongs to the HTTP/SSE/persistence
layer: no ChatMessage rows, no RunManager, no SSE event fan-out, no session
memory, no confidence extraction hooks, no audit logging. We want to
measure the agent's reasoning — not the orchestrator's plumbing.

Public surface
--------------

* :class:`AgentRunResult` — dataclass returned by :func:`run_agent`. Shape
  is compatible with ``BaselineResult`` plus three extras specific to the
  in-house agent (``confidence_score``, ``num_steps``, ``context_chars``).
* :func:`run_agent` — async entry point. Loads context, constructs the
  agent, streams a response, and returns a populated :class:`AgentRunResult`.

Every helper the unit tests need to patch is imported at module level so
tests can swap them out via ``app.services.benchmarks.agent_runner.XXX``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# ---------------------------------------------------------------------------
# Re-exported imports — tests patch these via this module's namespace.
# ---------------------------------------------------------------------------
from app.services.chat.agents.unified_agent import UnifiedAgent  # noqa: F401
from app.services.chat.domain_knowledge import retrieve_domain_knowledge  # noqa: F401
from app.services.chat.tenant_resolver import TenantEntityResolver  # noqa: F401
from app.services.chat.tools import build_all_tool_definitions  # noqa: F401
from app.services.learned_rules_service import retrieve_learned_rules  # noqa: F401
from app.services.mcp_connector_service import (  # noqa: F401
    get_active_connectors_for_tenant,
)
from app.services.netsuite_metadata_service import get_active_metadata  # noqa: F401
from app.services.query_pattern_service import retrieve_similar_patterns  # noqa: F401

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.chat.agents.base_agent import AgentResult


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Hard wall-clock ceiling for a single run. Agent runs can be slow when
# investigating through long tool loops; the benchmark harness will retry
# the case separately if this trips.
_TOTAL_TIMEOUT_SECONDS = 180.0

# Cap on how much of a tool result we keep in AgentRunResult.tool_calls.
# Mirrors baseline_runner's preview budget so the two outputs look the same.
_TOOL_RESULT_PREVIEW_CHARS = 1500

# Pricing in USD per million tokens — MUST match baseline_runner._MODEL_PRICING
# so cost comparisons are apples-to-apples. Any new models need to be added
# to both tables in lockstep.
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-4-5-20251101": (15.0, 75.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
_DEFAULT_PRICING_KEY = "claude-sonnet-4-6"

# Sentinel actor id for benchmark-originated tool calls. Audit logs and
# analytics can filter on this to exclude benchmark traffic.
_BENCHMARK_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_BENCHMARK_CORRELATION_ID = "benchmark-agent"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class AgentRunResult:
    """Outcome of a single in-house UnifiedAgent run.

    The first eight fields match ``BaselineResult`` exactly so the harness
    can treat both runners uniformly. The last three expose signals that
    only the in-house agent produces — baseline runs leave them at their
    defaults when compared.
    """

    answer_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    success: bool = False
    error: str | None = None

    # In-house extras
    confidence_score: float | None = None
    num_steps: int = 0
    context_chars: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _calculate_cost(*, model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute USD cost from token counts using the static pricing table.

    Unknown models fall back to sonnet pricing rather than raising — the
    benchmark must always return a comparable number.
    """
    in_rate, out_rate = _MODEL_PRICING.get(model, _MODEL_PRICING[_DEFAULT_PRICING_KEY])
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000.0


def _truncate_preview(value: Any) -> str:
    s = value if isinstance(value, str) else str(value)
    if len(s) <= _TOOL_RESULT_PREVIEW_CHARS:
        return s
    return s[:_TOOL_RESULT_PREVIEW_CHARS] + f"... [+{len(s) - _TOOL_RESULT_PREVIEW_CHARS} chars truncated]"


def _tool_log_to_baseline_shape(tool_calls_log: list[dict]) -> list[dict]:
    """Normalize AgentResult.tool_calls_log into BaselineResult.tool_calls shape.

    UnifiedAgent logs entries built by ``build_tool_call_log_entry`` with
    keys ``tool``/``params``/``result_summary``. The baseline uses
    ``name``/``input``/``result_preview``. We map between the two so the
    benchmark harness sees the same schema from both runners.
    """
    normalized: list[dict] = []
    for entry in tool_calls_log:
        # `result_summary` is a structured dict — flatten to a short preview
        # string so it round-trips through JSON / printing cleanly.
        summary = entry.get("result_summary")
        if isinstance(summary, dict):
            preview_source: Any = json.dumps(summary, default=str)
        elif summary is None:
            preview_source = ""
        else:
            preview_source = summary

        normalized.append(
            {
                "name": entry.get("tool", "unknown"),
                "input": entry.get("params", {}) or {},
                "result_preview": _truncate_preview(preview_source),
            }
        )
    return normalized


def _count_steps(tool_calls_log: list[dict]) -> int:
    """Count distinct agent steps — one step per unique ``step`` index.

    The agent runs its loop in discrete steps; a single step can fire
    multiple tool calls in parallel. For benchmark comparison we care
    about the loop iteration count, not the total tool invocation count.
    """
    if not tool_calls_log:
        return 0
    steps = {entry.get("step", 0) for entry in tool_calls_log}
    return len(steps)


def _build_adapter(*, provider: str, api_key: str):
    """Indirection layer so tests can patch without importing anthropic.

    The benchmark always uses the platform default adapter (not per-tenant
    BYOK) so comparisons against the baseline are apples-to-apples: same
    key, same rate limits, same pricing.
    """
    from app.services.chat.llm_adapter import get_adapter

    return get_adapter(provider, api_key)


async def _load_tenant_config(db: "AsyncSession", tenant_id: uuid.UUID):
    """Return (best-effort) the tenant's config row, or None on failure."""
    try:
        from sqlalchemy import select

        from app.models.tenant import TenantConfig

        result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))
        return result.scalar_one_or_none()
    except Exception:  # pragma: no cover - defensive
        logger.warning("agent_runner.tenant_config_failed", exc_info=True)
        return None


async def _assemble_context(
    *,
    db: "AsyncSession",
    tenant_id: uuid.UUID,
    question: str,
    adapter: Any,
    entity_resolver_model: str,
    tenant_config: Any,
) -> dict[str, Any]:
    """Replicate the subset of ``_chat_agentic`` context assembly needed by
    UnifiedAgent._setup_context.

    Every sub-step is wrapped in its own try/except so a single bad
    connection (e.g. missing vector index, no connectors, empty entity
    table) downgrades to an empty value rather than failing the whole run.
    """
    context: dict[str, Any] = {
        "user_timezone": None,
        "tenant_vernacular": "",
        "domain_knowledge": [],
        "proven_patterns": [],
        "learned_rules": [],
        "fiscal_year_start_month": 1,
        "table_schemas": "",
    }

    # Entity resolution → vernacular XML
    try:
        vernacular = await TenantEntityResolver.resolve_entities(
            user_message=question,
            tenant_id=tenant_id,
            db=db,
            adapter=adapter,
            model=entity_resolver_model,
        )
        context["tenant_vernacular"] = vernacular or ""
    except Exception:
        logger.warning("agent_runner.entity_resolver_failed", exc_info=True)

    # Domain knowledge
    try:
        dk_chunks = await retrieve_domain_knowledge(db=db, query_text=question, top_k=6)
        context["domain_knowledge"] = [c["raw_text"] for c in dk_chunks if isinstance(c, dict) and c.get("raw_text")]
    except Exception:
        logger.warning("agent_runner.domain_knowledge_failed", exc_info=True)

    # Proven patterns
    try:
        patterns = await retrieve_similar_patterns(db, tenant_id, question)
        context["proven_patterns"] = patterns or []
    except Exception:
        logger.warning("agent_runner.proven_patterns_failed", exc_info=True)

    # Learned rules
    try:
        rules = await retrieve_learned_rules(db=db, tenant_id=tenant_id)
        context["learned_rules"] = rules or []
    except Exception:
        logger.warning("agent_runner.learned_rules_failed", exc_info=True)

    # Fiscal year start
    if tenant_config is not None:
        fy_start = getattr(tenant_config, "fiscal_year_start_month", 1) or 1
        context["fiscal_year_start_month"] = fy_start

    return context


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_agent(
    *,
    tenant_id: uuid.UUID,
    question: str,
    db: "AsyncSession",
    model: str = "claude-sonnet-4-6",
    max_steps: int = 12,
    session_id: uuid.UUID | None = None,  # noqa: ARG001 - kept for harness parity
) -> AgentRunResult:
    """Run a single question through UnifiedAgent in-process.

    Parameters
    ----------
    tenant_id:
        Tenant whose context (metadata, connectors, vernacular, etc.) will
        drive the agent setup.
    question:
        The user's natural-language question.
    db:
        Async DB session. Re-used for context loading and tool execution.
    model:
        Anthropic model id. Defaults to ``claude-sonnet-4-6`` to match the
        baseline runner.
    max_steps:
        Upper bound on agent loop iterations. The UnifiedAgent has its own
        internal ``max_steps`` property (12 normal, 15 investigation) —
        this parameter is here for harness parity and is currently
        informational only. If we ever need to override the agent's
        internal cap for a benchmark we can wire it in here.
    session_id:
        Unused today. Accepted so the benchmark harness can pass a stable
        session id for agents that cache per-session state in the future.

    Returns
    -------
    AgentRunResult
        Always returns — never raises. Setup failures, agent exceptions,
        and wall-clock timeouts are captured in ``result.error`` with
        ``result.success`` = False.
    """
    start = time.monotonic()
    result = AgentRunResult()

    # ── 1. Build adapter ────────────────────────────────────────────────
    try:
        from app.core.config import settings

        api_key = getattr(settings, "ANTHROPIC_API_KEY", None)
        if not api_key:
            result.error = "missing_anthropic_api_key"
            result.latency_ms = int((time.monotonic() - start) * 1000)
            return result
        adapter = _build_adapter(provider="anthropic", api_key=api_key)
    except Exception as exc:
        result.error = f"adapter_init_failed: {exc}"
        result.latency_ms = int((time.monotonic() - start) * 1000)
        return result

    # ── 2. Load tenant metadata + connectors + config (best effort) ─────
    try:
        metadata = await get_active_metadata(db, tenant_id)
    except Exception:
        logger.warning("agent_runner.metadata_load_failed", exc_info=True)
        metadata = None

    try:
        connectors = await get_active_connectors_for_tenant(db, tenant_id)
        connectors = connectors or []
    except Exception:
        logger.warning("agent_runner.connectors_load_failed", exc_info=True)
        connectors = []

    tenant_config = await _load_tenant_config(db, tenant_id)

    # ── 3. Build context (vernacular, DK, patterns, rules) ──────────────
    try:
        context = await _assemble_context(
            db=db,
            tenant_id=tenant_id,
            question=question,
            adapter=adapter,
            entity_resolver_model=model,
            tenant_config=tenant_config,
        )
    except Exception as exc:
        logger.warning("agent_runner.context_assembly_failed", exc_info=True)
        context = {
            "user_timezone": None,
            "tenant_vernacular": "",
            "domain_knowledge": [],
            "proven_patterns": [],
            "learned_rules": [],
            "fiscal_year_start_month": 1,
            "table_schemas": "",
        }
        logger.info("agent_runner.context_assembly fallback reason=%s", exc)

    # ── 4. Build tool definitions ───────────────────────────────────────
    try:
        _ = await build_all_tool_definitions(db, tenant_id)
        # The UnifiedAgent manages its own tool_definitions via its
        # tool_definitions property + _setup_context; we don't need to
        # pass the list through. We call build_all_tool_definitions()
        # above only so context_assembly failures surface here rather
        # than deep inside the agent's first tool call.
    except Exception:
        logger.warning("agent_runner.tool_defs_load_failed", exc_info=True)

    # ── 5. Construct the agent ──────────────────────────────────────────
    try:
        agent = UnifiedAgent(
            tenant_id=tenant_id,
            user_id=_BENCHMARK_ACTOR_ID,
            correlation_id=_BENCHMARK_CORRELATION_ID,
            metadata=metadata,
            policy=None,
            context_need="data",
        )
    except Exception as exc:
        result.error = f"agent_init_failed: {exc}"
        result.latency_ms = int((time.monotonic() - start) * 1000)
        return result

    # ── 6. Run with wall-clock timeout, collect the final response ──────
    agent_result: AgentResult | None = None
    try:

        async def _drive_agent():
            nonlocal agent_result
            async for event_type, payload in agent.run_streaming(
                task=question,
                context=context,
                db=db,
                adapter=adapter,
                model=model,
            ):
                if event_type == "response":
                    agent_result = payload

        await asyncio.wait_for(_drive_agent(), timeout=_TOTAL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        result.error = f"timeout: exceeded {_TOTAL_TIMEOUT_SECONDS:.0f}s wall clock"
        result.latency_ms = int((time.monotonic() - start) * 1000)
        # Still try to capture system_prompt size so we can see "how big
        # was the prompt when it blew up".
        try:
            result.context_chars = len(agent.system_prompt)
        except Exception:
            pass
        return result
    except Exception as exc:
        result.error = str(exc)
        result.latency_ms = int((time.monotonic() - start) * 1000)
        try:
            result.context_chars = len(agent.system_prompt)
        except Exception:
            pass
        return result

    # ── 7. Capture system prompt size AFTER setup so we see the full cost ─
    try:
        result.context_chars = len(agent.system_prompt)
    except Exception:
        result.context_chars = 0

    # ── 8. Map the AgentResult onto AgentRunResult ──────────────────────
    if agent_result is None:
        result.error = "agent_produced_no_response"
        result.latency_ms = int((time.monotonic() - start) * 1000)
        return result

    if not agent_result.success:
        result.error = agent_result.error or "agent_reported_failure"
        result.answer_text = str(agent_result.data or "")
        result.latency_ms = int((time.monotonic() - start) * 1000)
        return result

    tokens = agent_result.tokens_used
    result.input_tokens = int(getattr(tokens, "input_tokens", 0) or 0)
    result.output_tokens = int(getattr(tokens, "output_tokens", 0) or 0)
    result.cost_usd = _calculate_cost(
        model=model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    result.answer_text = str(agent_result.data or "")
    result.tool_calls = _tool_log_to_baseline_shape(agent_result.tool_calls_log)
    result.num_steps = _count_steps(agent_result.tool_calls_log)
    result.confidence_score = agent_result.confidence_score
    result.success = True
    result.latency_ms = int((time.monotonic() - start) * 1000)
    return result
