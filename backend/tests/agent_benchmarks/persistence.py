"""Persist vs-MCP benchmark results to the agent_benchmark_runs table.

Called by the CLI when --persist is passed, and by the nightly Celery
task. Writes one row per (case_id, side) pair so we can compute deltas
and detect regressions day-over-day.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

from app.models.agent_benchmark_run import AgentBenchmarkRun

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _safe_preview(text: str | None, limit: int = 500) -> str | None:
    if text is None:
        return None
    return text[:limit]


async def persist_case_result(
    *,
    db: "AsyncSession",
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    run_date: date,
    case_id: str,
    side: str,
    model: str,
    result: Any,  # BaselineResult or AgentRunResult
    answer_accuracy: float,
    tool_accuracy: float,
) -> AgentBenchmarkRun:
    """Persist a single case × side result to agent_benchmark_runs.

    Returns the inserted row (not yet committed — caller owns the transaction).
    """
    if side not in ("ours", "mcp"):
        raise ValueError(f"side must be 'ours' or 'mcp', got {side!r}")

    row = AgentBenchmarkRun(
        tenant_id=tenant_id,
        run_id=run_id,
        run_date=run_date,
        case_id=case_id,
        side=side,
        model=model,
        answer_accuracy=float(answer_accuracy),
        tool_accuracy=float(tool_accuracy),
        success=bool(getattr(result, "success", False)),
        error_message=getattr(result, "error", None),
        cost_usd=float(getattr(result, "cost_usd", 0.0) or 0.0),
        latency_ms=int(getattr(result, "latency_ms", 0) or 0),
        input_tokens=int(getattr(result, "input_tokens", 0) or 0),
        output_tokens=int(getattr(result, "output_tokens", 0) or 0),
        answer_preview=_safe_preview(getattr(result, "answer_text", None)),
        context_chars=getattr(result, "context_chars", None),
        num_steps=getattr(result, "num_steps", None),
        confidence_score=getattr(result, "confidence_score", None),
        tool_calls=getattr(result, "tool_calls", None),
    )
    db.add(row)
    return row
