"""Agent benchmark API — view vs-MCP benchmark run results and trends.

Powers the eventual frontend dashboard. For now, three read-only JSON
endpoints that can be polled or displayed by any client.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.agent_benchmark_run import AgentBenchmarkRun
from app.models.user import User

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CaseResultPair(BaseModel):
    case_id: str
    ours_accuracy: float | None
    mcp_accuracy: float | None
    delta_accuracy: float | None
    ours_cost: float | None
    mcp_cost: float | None
    ours_latency_ms: int | None
    mcp_latency_ms: int | None
    ours_success: bool | None
    mcp_success: bool | None
    ours_answer_preview: str | None
    mcp_answer_preview: str | None
    ours_error: str | None
    mcp_error: str | None


class BenchmarkRunSummary(BaseModel):
    run_id: str
    run_date: date
    total_cases: int
    ours_wins: int
    mcp_wins: int
    ties: int
    failures: int
    avg_delta_accuracy: float
    ours_avg_accuracy: float
    mcp_avg_accuracy: float
    ours_avg_cost: float
    mcp_avg_cost: float
    cases: list[CaseResultPair]


class DailyTrendPoint(BaseModel):
    run_date: date
    ours_avg_accuracy: float
    mcp_avg_accuracy: float
    delta_accuracy: float
    cases: int


class TrendResponse(BaseModel):
    tenant_id: str
    days: int
    points: list[DailyTrendPoint]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/latest", response_model=BenchmarkRunSummary | None)
async def get_latest_benchmark_run(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the most recent vs-MCP benchmark run for the current tenant.

    Returns null if no runs exist yet. Cases are grouped by case_id and
    paired ours-vs-mcp so the frontend can render a single row per case.
    """
    # Find the latest run_id for this tenant
    latest_run_stmt = (
        select(AgentBenchmarkRun.run_id, AgentBenchmarkRun.run_date)
        .where(AgentBenchmarkRun.tenant_id == user.tenant_id)
        .order_by(desc(AgentBenchmarkRun.run_date), desc(AgentBenchmarkRun.created_at))
        .limit(1)
    )
    latest = (await db.execute(latest_run_stmt)).first()
    if latest is None:
        return None

    run_id, run_date_val = latest

    # Fetch all rows from that run
    rows_stmt = (
        select(AgentBenchmarkRun)
        .where(
            AgentBenchmarkRun.tenant_id == user.tenant_id,
            AgentBenchmarkRun.run_id == run_id,
        )
        .order_by(AgentBenchmarkRun.case_id, AgentBenchmarkRun.side)
    )
    rows = (await db.execute(rows_stmt)).scalars().all()

    # Group by case_id
    cases_map: dict[str, dict] = {}
    for row in rows:
        c = cases_map.setdefault(
            row.case_id,
            {"ours": None, "mcp": None},
        )
        c[row.side] = row

    paired: list[CaseResultPair] = []
    ours_wins = 0
    mcp_wins = 0
    ties = 0
    failures = 0
    ours_accs: list[float] = []
    mcp_accs: list[float] = []
    ours_costs: list[float] = []
    mcp_costs: list[float] = []
    deltas: list[float] = []

    for case_id, sides in cases_map.items():
        ours = sides.get("ours")
        mcp = sides.get("mcp")
        delta = None
        if ours is not None and mcp is not None:
            delta = round(ours.answer_accuracy - mcp.answer_accuracy, 4)
            deltas.append(delta)
            if delta > 0.05:
                ours_wins += 1
            elif delta < -0.05:
                mcp_wins += 1
            else:
                ties += 1
        elif ours is None and mcp is None:
            failures += 1

        if ours is not None:
            ours_accs.append(ours.answer_accuracy)
            ours_costs.append(ours.cost_usd)
        if mcp is not None:
            mcp_accs.append(mcp.answer_accuracy)
            mcp_costs.append(mcp.cost_usd)

        paired.append(
            CaseResultPair(
                case_id=case_id,
                ours_accuracy=ours.answer_accuracy if ours else None,
                mcp_accuracy=mcp.answer_accuracy if mcp else None,
                delta_accuracy=delta,
                ours_cost=ours.cost_usd if ours else None,
                mcp_cost=mcp.cost_usd if mcp else None,
                ours_latency_ms=ours.latency_ms if ours else None,
                mcp_latency_ms=mcp.latency_ms if mcp else None,
                ours_success=ours.success if ours else None,
                mcp_success=mcp.success if mcp else None,
                ours_answer_preview=ours.answer_preview if ours else None,
                mcp_answer_preview=mcp.answer_preview if mcp else None,
                ours_error=ours.error_message if ours else None,
                mcp_error=mcp.error_message if mcp else None,
            )
        )

    def _avg(xs: list[float]) -> float:
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    return BenchmarkRunSummary(
        run_id=str(run_id),
        run_date=run_date_val,
        total_cases=len(cases_map),
        ours_wins=ours_wins,
        mcp_wins=mcp_wins,
        ties=ties,
        failures=failures,
        avg_delta_accuracy=_avg(deltas),
        ours_avg_accuracy=_avg(ours_accs),
        mcp_avg_accuracy=_avg(mcp_accs),
        ours_avg_cost=_avg(ours_costs),
        mcp_avg_cost=_avg(mcp_costs),
        cases=paired,
    )


@router.get("/trend", response_model=TrendResponse)
async def get_benchmark_trend(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(14, ge=1, le=90, description="Number of days of history to return"),
):
    """Return daily average accuracy trend over the last N days.

    One point per day with a benchmark run. Useful for a sparkline chart
    showing whether we're trending up or down vs MCP.
    """
    cutoff = date.today() - timedelta(days=days)

    stmt = (
        select(
            AgentBenchmarkRun.run_date,
            AgentBenchmarkRun.side,
            func.avg(AgentBenchmarkRun.answer_accuracy).label("avg_acc"),
            func.count(AgentBenchmarkRun.id).label("n"),
        )
        .where(
            AgentBenchmarkRun.tenant_id == user.tenant_id,
            AgentBenchmarkRun.run_date >= cutoff,
        )
        .group_by(AgentBenchmarkRun.run_date, AgentBenchmarkRun.side)
        .order_by(AgentBenchmarkRun.run_date)
    )

    rows = (await db.execute(stmt)).all()

    by_date: dict[date, dict[str, float]] = {}
    for run_date_val, side, avg_acc, n in rows:
        bucket = by_date.setdefault(run_date_val, {"ours": 0.0, "mcp": 0.0, "n": 0})
        bucket[side] = float(avg_acc or 0.0)
        bucket["n"] += int(n)

    points: list[DailyTrendPoint] = []
    for run_date_val in sorted(by_date.keys()):
        b = by_date[run_date_val]
        ours = b.get("ours", 0.0)
        mcp = b.get("mcp", 0.0)
        points.append(
            DailyTrendPoint(
                run_date=run_date_val,
                ours_avg_accuracy=round(ours, 4),
                mcp_avg_accuracy=round(mcp, 4),
                delta_accuracy=round(ours - mcp, 4),
                cases=int(b["n"]),
            )
        )

    return TrendResponse(
        tenant_id=str(user.tenant_id),
        days=days,
        points=points,
    )
