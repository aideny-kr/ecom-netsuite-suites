"""Reconciliation API endpoints.

All endpoints gated by require_feature("reconciliation").
Mutation endpoints gated by require_permission("recon.run").
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date as date_type
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_feature, require_permission
from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.models.user import User
from app.schemas.reconciliation import (
    ReconResultApprove,
    ReconResultResponse,
    ReconRunCreate,
    ReconRunResponse,
    ReconRunSummary,
)
from app.services import audit_service
from app.services.reconciliation.evidence_service import EvidencePackGenerator
from app.services.reconciliation.recon_job import ReconJobRunner

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


# ---------------------------------------------------------------------------
# List runs
# ---------------------------------------------------------------------------
@router.get("/runs", response_model=list[ReconRunResponse])
async def list_runs(
    user: Annotated[User, Depends(require_feature("reconciliation"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 20,
    offset: int = 0,
):
    stmt = (
        select(ReconciliationRun)
        .where(ReconciliationRun.tenant_id == user.tenant_id)
        .order_by(ReconciliationRun.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    runs = result.scalars().all()
    return [ReconRunResponse.model_validate(r) for r in runs]


# ---------------------------------------------------------------------------
# Get single run
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}", response_model=ReconRunResponse)
async def get_run(
    run_id: str,
    user: Annotated[User, Depends(require_feature("reconciliation"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    stmt = select(ReconciliationRun).where(
        ReconciliationRun.id == uuid.UUID(run_id),
        ReconciliationRun.tenant_id == user.tenant_id,
    )
    result = await db.execute(stmt)
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return ReconRunResponse.model_validate(run)


# ---------------------------------------------------------------------------
# Trigger new run
# ---------------------------------------------------------------------------
@router.post("/runs", response_model=ReconRunSummary, status_code=status.HTTP_201_CREATED)
async def create_run(
    request: ReconRunCreate,
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    runner = ReconJobRunner(db=db, tenant_id=str(user.tenant_id))

    try:
        summary = await runner.run(
            date_from=request.date_from,
            date_to=request.date_to,
            subsidiary_id=request.subsidiary_id,
            payout_ids=request.payout_ids,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Reconciliation failed: {e}",
        )

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="reconciliation",
        action="recon.run",
        actor_id=user.id,
        resource_type="reconciliation_run",
        resource_id=summary.run_id,
    )
    await db.commit()

    return summary


# ---------------------------------------------------------------------------
# Get run results
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/results", response_model=list[ReconResultResponse])
async def get_run_results(
    run_id: str,
    user: Annotated[User, Depends(require_feature("reconciliation"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    stmt = (
        select(ReconciliationResult)
        .where(
            ReconciliationResult.tenant_id == user.tenant_id,
            ReconciliationResult.run_id == uuid.UUID(run_id),
        )
        .order_by(ReconciliationResult.confidence.asc())
        .limit(limit)
        .offset(offset)
    )

    if status_filter:
        stmt = stmt.where(ReconciliationResult.status == status_filter)

    result = await db.execute(stmt)
    results = result.scalars().all()
    return [ReconResultResponse.model_validate(r) for r in results]


# ---------------------------------------------------------------------------
# Approve match
# ---------------------------------------------------------------------------
@router.patch("/results/{result_id}/approve", response_model=ReconResultResponse)
async def approve_result(
    result_id: str,
    request: ReconResultApprove,
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    stmt = select(ReconciliationResult).where(
        ReconciliationResult.id == uuid.UUID(result_id),
        ReconciliationResult.tenant_id == user.tenant_id,
    )
    result = await db.execute(stmt)
    recon_result = result.scalar_one_or_none()

    if not recon_result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Result not found")

    if recon_result.status == "approved":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Already approved")

    recon_result.status = "approved"
    recon_result.approved_by = user.id
    recon_result.approved_at = datetime.now(timezone.utc)

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="reconciliation",
        action="recon.approve",
        actor_id=user.id,
        resource_type="reconciliation_result",
        resource_id=result_id,
    )
    await db.commit()
    await db.refresh(recon_result)

    return ReconResultResponse.model_validate(recon_result)


# ---------------------------------------------------------------------------
# Download evidence pack
# ---------------------------------------------------------------------------
@router.get("/evidence/{run_id}")
async def download_evidence(
    run_id: str,
    user: Annotated[User, Depends(require_feature("reconciliation"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    stmt = select(ReconciliationRun).where(
        ReconciliationRun.id == uuid.UUID(run_id),
        ReconciliationRun.tenant_id == user.tenant_id,
    )
    run_result = await db.execute(stmt)
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    stmt = select(ReconciliationResult).where(ReconciliationResult.run_id == uuid.UUID(run_id))
    result = await db.execute(stmt)
    results = result.scalars().all()

    results_dicts = [
        {
            "id": str(r.id),
            "match_type": r.match_type,
            "confidence": r.confidence,
            "status": r.status,
            "stripe_amount": r.stripe_amount,
            "netsuite_amount": r.netsuite_amount,
            "variance_amount": r.variance_amount,
            "variance_type": r.variance_type,
            "variance_explanation": r.variance_explanation,
            "currency": r.currency,
            "match_rule": r.match_rule,
            "evidence": r.evidence,
        }
        for r in results
    ]

    generator = EvidencePackGenerator()
    excel_bytes = generator.generate_excel(
        results=results_dicts,
        run_id=run_id,
        date_from=run.date_from,
        date_to=run.date_to,
    )

    filename = f"recon-evidence-{run.date_from.isoformat()}-{run.date_to.isoformat()}.xlsx"

    return StreamingResponse(
        excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Close period
# ---------------------------------------------------------------------------
@router.post("/close/{period}", status_code=status.HTTP_200_OK)
async def close_period(
    period: str,
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Lock all approved results for a given period (e.g., '2026-03').

    Prevents further modifications to matched transactions.
    """
    try:
        year_str, month_str = period.split("-")
        year, month = int(year_str), int(month_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Period must be YYYY-MM format",
        )

    first_day = date_type(year, month, 1)
    last_day = date_type(year, month, calendar.monthrange(year, month)[1])

    stmt = select(ReconciliationRun).where(
        ReconciliationRun.tenant_id == user.tenant_id,
        ReconciliationRun.date_from >= first_day,
        ReconciliationRun.date_to <= last_day,
        ReconciliationRun.status == "completed",
    )
    result = await db.execute(stmt)
    runs = result.scalars().all()

    if not runs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No completed reconciliation runs found for {period}",
        )

    locked_count = 0
    for run in runs:
        stmt = select(ReconciliationResult).where(
            ReconciliationResult.run_id == run.id,
            ReconciliationResult.status.in_(["approved", "auto_matched"]),
        )
        result = await db.execute(stmt)
        period_results = result.scalars().all()

        for r in period_results:
            r.status = "locked"
            locked_count += 1

        run.status = "closed"

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="reconciliation",
        action="recon.close_period",
        actor_id=user.id,
        resource_type="reconciliation_period",
        resource_id=period,
    )
    await db.commit()

    return {
        "period": period,
        "runs_closed": len(runs),
        "results_locked": locked_count,
        "message": f"Period {period} closed. {locked_count} results locked.",
    }
