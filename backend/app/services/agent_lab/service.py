"""Service layer for agent-lab runs. Mixes async (API endpoint) and sync
(Celery worker) helpers.

Async functions (start_run, get_run_snapshot, list_runs, list_patterns)
are called from FastAPI endpoints.

Sync functions (finalize_run_sync, sum_benchmark_cost_for_run, cancel_run)
are called from the Celery worker's try/finally block and the cancel endpoint.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import redis
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.agent_benchmark_run import AgentBenchmarkRun
from app.models.agent_lab_run import AgentLabRun
from app.models.tenant_query_pattern import TenantQueryPattern
from app.models.user import User

# Top-level import with fallback — Task 5 replaces the stub with the real task.
try:
    from app.workers.tasks.agent_lab_runner import agent_lab_run_task
except ImportError:
    agent_lab_run_task = None  # stub until Task 5 lands


class ConcurrentRunError(Exception):
    """Raised when a same-kind run is already in progress for the tenant."""


_BENCHMARK_TOTAL_CASES = 18
_EXPERIMENT_MAX_CASES = 60  # matches QUERY_IMPROVEMENT_MAX_EXPERIMENTS default


# ----------------------------------------------------------------------
# Async — called from FastAPI endpoints
# ----------------------------------------------------------------------


async def start_run(
    db: AsyncSession,
    *,
    user: User,
    tenant_id: uuid.UUID,
    kind: Literal["benchmark", "experiment"],
    mode: Literal["all", "single"],
    case_id: str | None = None,
) -> AgentLabRun:
    """Insert AgentLabRun row, dispatch Celery task, return row.

    Raises ConcurrentRunError on IntegrityError from the partial unique
    index (caller returns 409).

    Caller is responsible for audit logging — this service layer intentionally
    does not log (matches the pattern elsewhere; audit lives at the API layer).

    Known trade-off: if Celery dispatch (apply_async) fails after DB commit,
    the row remains with status='running' with no worker attached. The partial
    unique index will then block the next run until the row is manually updated.
    This is an Outbox-pattern gap acceptable for v1 (super-admin dev tool).
    v1.1 may introduce a sweeper task that marks orphan rows as 'failed'.
    """
    total_cases = _total_cases_for(kind, mode)

    run = AgentLabRun(
        tenant_id=tenant_id,
        triggered_by_user_id=user.id,
        kind=kind,
        mode=mode,
        case_id=case_id,
        status="running",
        total_cases=total_cases,
    )
    db.add(run)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConcurrentRunError(
            f"a {kind} run is already in progress for this tenant"
        ) from exc

    await db.refresh(run)
    await db.commit()

    if agent_lab_run_task is None:
        raise RuntimeError(
            "agent_lab_run_task not yet implemented — Task 5 must be merged first"
        )

    agent_lab_run_task.apply_async(
        kwargs={
            "run_id": str(run.id),
            "config": {
                "kind": kind,
                "mode": mode,
                "case_id": case_id,
                "tenant_id": str(tenant_id),
            },
        }
    )
    return run


async def get_run_snapshot(db: AsyncSession, run_id: uuid.UUID) -> dict | None:
    """Return run row + case-level results.

    Cases come from agent_benchmark_runs (benchmark) or experiment_log (experiment).
    Returns None if run_id is not found.
    """
    run = await db.get(AgentLabRun, run_id)
    if run is None:
        return None

    cases: list[dict] = []
    if run.kind == "benchmark":
        rows = (
            await db.execute(
                select(AgentBenchmarkRun)
                .where(AgentBenchmarkRun.run_id == run_id)
                .order_by(AgentBenchmarkRun.case_id, AgentBenchmarkRun.side)
            )
        ).scalars().all()
        # Pair ours/mcp by case_id
        by_case: dict[str, dict] = {}
        for row in rows:
            bucket = by_case.setdefault(row.case_id, {"ours": None, "mcp": None})
            bucket[row.side] = _benchmark_row_to_dict(row)
        for case_id, sides in by_case.items():
            cases.append({"case_id": case_id, **sides})
    else:  # experiment
        from app.models.experiment_log import ExperimentLog

        rows = (
            await db.execute(
                select(ExperimentLog)
                .where(ExperimentLog.metadata_json["run_id"].astext == str(run_id))
                .order_by(ExperimentLog.created_at)
            )
        ).scalars().all()
        cases = [_experiment_row_to_dict(row) for row in rows]

    return {"run": _run_to_dict(run), "cases": cases}


async def list_runs(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str | None = None,
    days: int = 14,
) -> list[AgentLabRun]:
    """Recent runs for tenant, filtered by kind and time window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(AgentLabRun)
        .where(
            AgentLabRun.tenant_id == tenant_id,
            AgentLabRun.started_at >= cutoff,
        )
        .order_by(desc(AgentLabRun.started_at))
        .limit(50)
    )
    if kind:
        stmt = stmt.where(AgentLabRun.kind == kind)
    return (await db.execute(stmt)).scalars().all()


async def list_patterns(
    db: AsyncSession, tenant_id: uuid.UUID
) -> list[TenantQueryPattern]:
    """Patterns for tenant, sorted last_used_at DESC NULLS LAST."""
    stmt = (
        select(TenantQueryPattern)
        .where(TenantQueryPattern.tenant_id == tenant_id)
        .order_by(TenantQueryPattern.last_used_at.desc().nulls_last())
    )
    return (await db.execute(stmt)).scalars().all()


# ----------------------------------------------------------------------
# Sync — called from Celery worker + cancel endpoint
# ----------------------------------------------------------------------


def finalize_run_sync(
    db: Session,
    run_id: uuid.UUID,
    *,
    status: Literal["completed", "cancelled", "failed"],
    cost_usd_actual: float,
    error_message: str | None = None,
) -> None:
    """Guaranteed to run in the Celery wrapper's finally block.

    Updates status, finished_at, cost_usd_actual, and optionally error_message.
    Row never stays status='running' after the worker finishes.
    """
    update_values: dict = {
        "status": status,
        "finished_at": datetime.now(timezone.utc),
        "cost_usd_actual": cost_usd_actual,
    }
    if error_message is not None:
        update_values["error_message"] = error_message

    db.query(AgentLabRun).filter_by(id=run_id).update(update_values)
    db.commit()


def sum_benchmark_cost_for_run(db: Session, run_id: uuid.UUID) -> float:
    """Sum cost_usd across agent_benchmark_runs rows for this run_id."""
    result = (
        db.query(func.coalesce(func.sum(AgentBenchmarkRun.cost_usd), 0.0))
        .filter(AgentBenchmarkRun.run_id == run_id)
        .scalar()
    )
    return float(result or 0.0)


def cancel_run(redis_client: redis.Redis, run_id: uuid.UUID) -> bool:
    """Set cancel flag in Redis with 5-min TTL.

    Sync because Redis SET is fast enough not to warrant async in the endpoint
    handler.
    """
    redis_client.set(f"agent_lab_run:{run_id}:cancel", "1", ex=300)
    return True


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------


def _total_cases_for(kind: str, mode: str) -> int:
    if mode == "single":
        return 1
    return _BENCHMARK_TOTAL_CASES if kind == "benchmark" else _EXPERIMENT_MAX_CASES


def _run_to_dict(run: AgentLabRun) -> dict:
    return {
        "run_id": str(run.id),
        "kind": run.kind,
        "mode": run.mode,
        "status": run.status,
        "total_cases": run.total_cases,
        "cases_completed": run.cases_completed,
        "cost_usd_actual": run.cost_usd_actual,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error_message": run.error_message,
    }


def _benchmark_row_to_dict(row: AgentBenchmarkRun) -> dict:
    return {
        "case_id": row.case_id,
        "side": row.side,
        "accuracy": row.answer_accuracy,
        "cost_usd": row.cost_usd,
        "latency_ms": row.latency_ms,
        "success": row.success,
        "answer_preview": row.answer_preview,
        "error_message": row.error_message,
    }


def _experiment_row_to_dict(row) -> dict:
    return {
        "case_id": row.test_query[:60] if row.test_query else None,
        "dialect": row.dialect,
        "decision": row.decision,
        "experiment_score": float(row.experiment_score) if row.experiment_score else 0.0,
        "generated_sql": row.generated_sql,
        "executed_successfully": row.executed_successfully,
        "error_message": row.error_message,
        "cost_usd": float(row.cost_usd) if row.cost_usd else 0.0,
    }
