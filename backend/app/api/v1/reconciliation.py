"""Reconciliation API endpoints.

All endpoints gated by require_feature("reconciliation").
Mutation endpoints gated by require_permission("recon.run").
"""

from __future__ import annotations

import asyncio
import calendar
import json
import uuid
from datetime import date as date_type
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_feature, require_permission
from app.core.redis_lock import acquire_lock, release_lock
from app.models.canonical import NetsuitePosting, Payout, PayoutLine
from app.models.connection import Connection
from app.models.pipeline import CursorState
from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.models.user import User
from app.schemas.reconciliation import (
    ReconBucketCount,
    ReconBucketSummary,
    ReconResultApprove,
    ReconResultResponse,
    ReconRunCreate,
    ReconRunResponse,
    ReconRunSummary,
)
from app.services import audit_service
from app.services.reconciliation.evidence_service import EvidencePackGenerator
from app.services.reconciliation.four_bucket_classifier import (
    ALL_BUCKETS,
    bucket_conditions,
)
from app.services.reconciliation.pipeline import ReconPipeline
from app.services.reconciliation.recon_job import ReconJobRunner

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


def _parse_uuid(value: str) -> uuid.UUID:
    """Parse a path UUID, returning 404 (not 500) on a malformed id."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


# ---------------------------------------------------------------------------
# Data freshness status (finance user accessible)
# ---------------------------------------------------------------------------
@router.get("/data-status")
async def get_data_status(
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return connector status and data freshness for reconciliation.

    Accessible by finance users (recon.run) — no admin permission required.
    """
    from sqlalchemy import func

    # Stripe status
    stripe_conn = (
        await db.execute(
            select(Connection).where(
                Connection.tenant_id == user.tenant_id,
                Connection.provider == "stripe",
            )
        )
    ).scalar_one_or_none()

    stripe_info: dict = {"connected": False, "status": "not_configured"}
    if stripe_conn:
        # Last sync time
        cursor = (
            await db.execute(
                select(CursorState.last_synced_at).where(
                    CursorState.connection_id == stripe_conn.id,
                    CursorState.object_type == "stripe_payouts",
                )
            )
        ).scalar_one_or_none()

        payout_count = (
            await db.execute(select(func.count(Payout.id)).where(Payout.tenant_id == user.tenant_id))
        ).scalar_one()

        payout_line_count = (
            await db.execute(select(func.count(PayoutLine.id)).where(PayoutLine.tenant_id == user.tenant_id))
        ).scalar_one()

        stripe_status = "healthy" if stripe_conn.status in ("active", "healthy") else stripe_conn.status
        stripe_info = {
            "connected": True,
            "status": stripe_status,
            "last_sync": cursor.isoformat() if cursor else None,
            "payout_count": payout_count,
            "payout_line_count": payout_line_count,
            "error": stripe_conn.error_reason,
        }

    # NetSuite deposit status
    ns_conn = (
        await db.execute(
            select(Connection).where(
                Connection.tenant_id == user.tenant_id,
                Connection.provider == "netsuite",
                Connection.status.in_(["active", "healthy"]),
            )
        )
    ).scalar_one_or_none()

    netsuite_info: dict = {"connected": False, "status": "not_configured"}
    if ns_conn:
        ns_cursor = (
            await db.execute(
                select(CursorState.last_synced_at).where(
                    CursorState.connection_id == ns_conn.id,
                    CursorState.object_type == "netsuite_deposits",
                )
            )
        ).scalar_one_or_none()

        from sqlalchemy import func as sqla_func

        deposit_count = (
            await db.execute(
                select(sqla_func.count(NetsuitePosting.id)).where(NetsuitePosting.tenant_id == user.tenant_id)
            )
        ).scalar_one()

        netsuite_info = {
            "connected": True,
            "status": "active",
            "last_sync": ns_cursor.isoformat() if ns_cursor else None,
            "deposit_count": deposit_count,
        }

    return {"stripe": stripe_info, "netsuite": netsuite_info}


# ---------------------------------------------------------------------------
# Sync trigger from Reconciliation page (finance user accessible)
# ---------------------------------------------------------------------------
@router.post("/sync")
async def trigger_recon_sync(
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Trigger Stripe + NetSuite sync for reconciliation data.

    Rate limited: max 1 sync per tenant per 5 minutes.
    Accessible by finance users (recon.run).
    """
    lock_key = f"recon_sync:{user.tenant_id}"
    if not acquire_lock(lock_key, timeout=300):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="A sync is already running or was triggered recently. Please wait 5 minutes.",
        )

    jobs_dispatched = []

    try:
        # Stripe sync
        stripe_conn = (
            await db.execute(
                select(Connection).where(
                    Connection.tenant_id == user.tenant_id,
                    Connection.provider == "stripe",
                    Connection.status.in_(["active", "healthy"]),
                )
            )
        ).scalar_one_or_none()

        if stripe_conn:
            from app.workers.celery_app import celery_app

            result = celery_app.send_task(
                "tasks.stripe_sync",
                kwargs={
                    "tenant_id": str(user.tenant_id),
                    "connection_id": str(stripe_conn.id),
                },
                queue="sync",
            )
            jobs_dispatched.append({"provider": "stripe", "job_id": result.id})

        # NetSuite deposit sync — dispatch via Celery (inline times out on Supabase)
        from app.services.ingestion.netsuite_deposit_sync import get_netsuite_rest_connection

        ns_conn = await get_netsuite_rest_connection(db, str(user.tenant_id))
        if ns_conn:
            ns_task = celery_app.send_task(
                "tasks.netsuite_deposit_sync",
                kwargs={"tenant_id": str(user.tenant_id)},
                queue="sync",
            )
            jobs_dispatched.append({"provider": "netsuite_deposits", "job_id": ns_task.id})

        await audit_service.log_event(
            db=db,
            tenant_id=user.tenant_id,
            category="reconciliation",
            action="recon.sync_trigger",
            actor_id=user.id,
            resource_type="sync",
            resource_id="manual",
            payload={"jobs": jobs_dispatched},
        )
        await db.commit()

    except Exception:
        release_lock(lock_key)
        raise

    return {
        "status": "syncing",
        "jobs": jobs_dispatched,
        "message": "Data sync triggered. Stripe syncs in background, NetSuite deposits synced inline.",
    }


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
    if request.match_level == "order":
        from app.services.reconciliation.order_recon_job import OrderReconJob

        runner = OrderReconJob(db=db, tenant_id=str(user.tenant_id))
    else:
        runner = ReconJobRunner(db=db, tenant_id=str(user.tenant_id))

    try:
        run_kwargs: dict = {
            "date_from": request.date_from,
            "date_to": request.date_to,
            "subsidiary_id": request.subsidiary_id,
        }
        if request.match_level == "payout":
            run_kwargs["payout_ids"] = request.payout_ids
        summary = await runner.run(**run_kwargs)
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
# Run reconciliation with SSE progress (pipeline)
# ---------------------------------------------------------------------------
@router.post("/runs/stream", status_code=status.HTTP_200_OK)
async def create_run_stream(
    request: ReconRunCreate,
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Run reconciliation pipeline with real-time SSE progress events.

    Emits events:
      - recon_progress: stage updates with progress percentage
      - recon_complete: final summary
      - recon_error: if pipeline fails
    """
    _SENTINEL = object()

    async def stream_generator():
        queue: asyncio.Queue = asyncio.Queue()

        async def _producer():
            try:
                pipeline = ReconPipeline(
                    db=db,
                    tenant_id=str(user.tenant_id),
                    queue=queue,
                )
                result = await pipeline.run(
                    date_from=request.date_from,
                    date_to=request.date_to,
                    subsidiary_id=request.subsidiary_id,
                    payout_ids=request.payout_ids,
                    match_level=request.match_level,
                )

                # Audit log on success
                run_id = result.get("run_id")
                if run_id:
                    await audit_service.log_event(
                        db=db,
                        tenant_id=user.tenant_id,
                        category="reconciliation",
                        action="recon.pipeline_run",
                        actor_id=user.id,
                        resource_type="reconciliation_run",
                        resource_id=run_id,
                    )
                    await db.commit()

            except Exception as e:
                await queue.put(
                    {
                        "type": "recon_error",
                        "error": f"Pipeline failed: {str(e)}",
                    }
                )
            finally:
                await queue.put(_SENTINEL)

        producer_task = asyncio.create_task(_producer())

        # Padding for Cloudflare / nginx buffering
        yield f": {' ' * 2048}\n\n"

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue

                if chunk is _SENTINEL:
                    break

                yield f"data: {json.dumps(chunk, default=str)}\n\n"
        finally:
            producer_task.cancel()

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Get run results
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/results", response_model=list[ReconResultResponse])
async def get_run_results(
    run_id: str,
    user: Annotated[User, Depends(require_feature("reconciliation"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: str | None = None,
    bucket: str | None = None,
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

    if bucket is not None:
        if bucket not in ALL_BUCKETS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid bucket")
        stmt = stmt.where(bucket_conditions(bucket))

    result = await db.execute(stmt)
    results = result.scalars().all()
    return [ReconResultResponse.model_validate(r) for r in results]


# ---------------------------------------------------------------------------
# Four-bucket summary (authoritative per-bucket counts + variance over the run)
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/buckets", response_model=ReconBucketSummary)
async def get_run_bucket_summary(
    run_id: str,
    user: Annotated[User, Depends(require_feature("reconciliation"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Authoritative per-bucket counts + total variance over the FULL run.

    The FE only fetches a page of results, so it cannot count buckets itself;
    these counts are computed server-side via the SQL twin of the classifier.
    """
    run_uuid = _parse_uuid(run_id)
    counts: dict[str, ReconBucketCount] = {}
    for bucket in ALL_BUCKETS:
        row = (
            await db.execute(
                select(
                    func.count(ReconciliationResult.id),
                    func.coalesce(func.sum(func.abs(ReconciliationResult.variance_amount)), 0),
                ).where(
                    ReconciliationResult.run_id == run_uuid,
                    ReconciliationResult.tenant_id == user.tenant_id,
                    bucket_conditions(bucket),
                )
            )
        ).one()
        counts[bucket] = ReconBucketCount(count=row[0], total_variance=row[1])
    return ReconBucketSummary(run_id=run_id, **counts)


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
