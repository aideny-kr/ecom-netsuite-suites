"""Reconciliation API endpoints.

All endpoints gated by require_feature("reconciliation").
Mutation endpoints gated by require_permission("recon.run").
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, insert, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_feature, require_permission
from app.core.redis_lock import acquire_lock, release_lock
from app.models.audit import AuditEvent
from app.models.canonical import NetsuitePosting, Payout, PayoutLine
from app.models.connection import Connection
from app.models.pipeline import CursorState
from app.models.reconciliation import ReconciliationResult, ReconciliationRun, ReconResolutionProposal
from app.models.user import User
from app.schemas.reconciliation import (
    ReconBucketApprove,
    ReconBucketApproveResult,
    ReconBucketCount,
    ReconBucketSummary,
    ReconCloseReadiness,
    ReconResultApprove,
    ReconResultResponse,
    ReconRunCreate,
    ReconRunResponse,
    ReconRunSummary,
    ResolutionGroupSummary,
    ResolutionProposalResponse,
    ResolutionSummaryResponse,
)
from app.services import audit_service
from app.services.reconciliation.close_scope import (
    closeable_runs_conditions,
    left_for_review_conditions,
)
from app.services.reconciliation.evidence_service import EvidencePackGenerator
from app.services.reconciliation.four_bucket_classifier import (
    ALL_BUCKETS,
    BULK_APPROVABLE_BUCKETS,
    TERMINAL_RESULT_STATUSES,
    bucket_conditions,
)
from app.services.reconciliation.pipeline import ReconPipeline
from app.services.reconciliation.recon_job import ReconJobRunner
from app.services.reconciliation.resolution_planner import plan_run

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
            ReconciliationResult.run_id == _parse_uuid(run_id),
        )
        # R2: `confidence` is now the advisory amount+temporal composite (matched rows
        # ~0.6–1.0), NOT the engine match-tier ladder. asc() intentionally surfaces
        # lower-advisory-confidence first (e.g. temporally-implausible "exact" matches).
        # Do NOT re-couple this to ladder values.
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

    # 404 on a missing/foreign run (a bogus run would otherwise return 200 with
    # all-zero counts, mirroring approve_bucket's run-existence guard).
    run = (
        await db.execute(
            select(ReconciliationRun).where(
                ReconciliationRun.id == run_uuid,
                ReconciliationRun.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

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

    # Close-readiness counts intentionally do NOT live here: the close is
    # period-scoped (POST /close/{period} touches EVERY in-scope run), so a
    # per-run readiness would gate a mutation it never inspected. Use
    # GET /close-readiness/{period} (the single authoritative source, R3-A).
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
        ReconciliationResult.id == _parse_uuid(result_id),
        ReconciliationResult.tenant_id == user.tenant_id,
    )
    result = await db.execute(stmt)
    recon_result = result.scalar_one_or_none()

    if not recon_result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Result not found")

    # Close = hard freeze. close_period leaves auto_matched+needs_review lines unlocked
    # for human review; without this guard such a line could still be single-approved
    # post-close, flipping it to 'approved' inside a closed period and never re-locked.
    run = (
        await db.execute(select(ReconciliationRun).where(ReconciliationRun.id == recon_result.run_id))
    ).scalar_one_or_none()
    if run is not None and run.status in ("closed", "locked"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Period is closed; cannot approve.")

    if recon_result.status == "approved":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Already approved")

    if recon_result.status == "locked":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Result is locked (period closed)",
        )

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
        payload={"notes": request.notes},
    )
    await db.commit()
    await db.refresh(recon_result)

    return ReconResultResponse.model_validate(recon_result)


# ---------------------------------------------------------------------------
# Bulk-approve a whole bucket (set-based, per-line audit, no auto-post)
# ---------------------------------------------------------------------------
_SKIP_STATUSES = TERMINAL_RESULT_STATUSES


@router.post("/runs/{run_id}/approve-bucket", response_model=ReconBucketApproveResult)
async def approve_bucket(
    run_id: str,
    request: ReconBucketApprove,
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Set-based bulk approve of every approvable line in a bucket.

    One server-side ``UPDATE ... RETURNING`` flips status, plus one immutable
    per-line audit row per approved line and one summary audit event — all
    sharing a batch ``correlation_id``. Skips already-approved/rejected/locked
    lines. Rejects ``needs_review``. This is a DB status flip + audit only; it
    never posts to NetSuite (no auto-post).
    """
    if request.bucket not in BULK_APPROVABLE_BUCKETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bucket is not bulk-approvable",
        )
    run_uuid = _parse_uuid(run_id)

    # 404 on a missing/foreign run (a non-existent run would otherwise silently
    # match 0 rows and return 200 with approved_count=0).
    run = (
        await db.execute(
            select(ReconciliationRun).where(
                ReconciliationRun.id == run_uuid,
                ReconciliationRun.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    # Reject a closed/locked period: close_period sets run.status="closed" but only
    # locks 'approved' rows and non-needs_review 'auto_matched' rows, leaving
    # 'suggested'/'pending' (and unreviewed needs_review) rows un-locked and thus
    # still bulk-approvable. Guard the run itself.
    if run.status in ("closed", "locked"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Period is closed; cannot approve.",
        )

    now = datetime.now(timezone.utc)
    correlation_id = str(uuid.uuid4())

    base_filter = (
        ReconciliationResult.run_id == run_uuid,
        ReconciliationResult.tenant_id == user.tenant_id,
        bucket_conditions(request.bucket),
    )

    # set-based update of only the approvable rows; RETURNING the ids we touched
    upd = (
        update(ReconciliationResult)
        .where(*base_filter, ReconciliationResult.status.notin_(_SKIP_STATUSES))
        .values(status="approved", approved_by=user.id, approved_at=now)
        .returning(ReconciliationResult.id)
    )
    approved_ids = (await db.execute(upd)).scalars().all()

    # accurate skipped_count, atomically (same txn, post-update): the freshly
    # approved rows now also match _SKIP_STATUSES, so the count of skip-status rows
    # in the bucket equals pre-existing skips PLUS the rows we just approved.
    # Subtract len(approved_ids) arithmetically rather than excluding their ids via
    # NOT IN — cheaper, and the bucket is run-scoped so the ids are this batch's.
    skip_status_count = (
        await db.execute(
            select(func.count(ReconciliationResult.id)).where(
                *base_filter, ReconciliationResult.status.in_(_SKIP_STATUSES)
            )
        )
    ).scalar_one()
    skipped_count = skip_status_count - len(approved_ids)

    # one immutable per-line audit row per approved result (multi-row insert)
    if approved_ids:
        await db.execute(
            insert(AuditEvent),
            [
                {
                    "tenant_id": user.tenant_id,
                    "actor_id": user.id,
                    "actor_type": "user",
                    "category": "reconciliation",
                    "action": "recon.approve",
                    "resource_type": "reconciliation_result",
                    "resource_id": str(rid),
                    "correlation_id": correlation_id,
                    "status": "success",
                }
                for rid in approved_ids
            ],
        )

    # one summary event for the human bulk action
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="reconciliation",
        action="recon.bulk_approve",
        actor_id=user.id,
        resource_type="reconciliation_run",
        resource_id=run_id,
        correlation_id=correlation_id,
        payload={
            "bucket": request.bucket,
            "approved_count": len(approved_ids),
            "notes": request.notes,
        },
    )

    await db.commit()

    return ReconBucketApproveResult(
        run_id=run_id,
        bucket=request.bucket,
        approved_count=len(approved_ids),
        skipped_count=skipped_count,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# (Re-)plan resolution proposals for a run — retry surface for the post-run hook
# ---------------------------------------------------------------------------
@router.post("/runs/{run_id}/plan-resolutions")
async def plan_resolutions(
    run_id: str,
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """(Re-)plan resolution proposals for a run. Idempotent: undecided
    proposals are superseded and re-derived; decided ones are untouched.
    DB-only — never posts to NetSuite."""
    run_uuid = _parse_uuid(run_id)
    try:
        return await plan_run(db, user.tenant_id, run_uuid)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")


# ---------------------------------------------------------------------------
# Resolution summary + per-group proposal listing (summary-first rework)
# ---------------------------------------------------------------------------
def _parse_group_key(group_key: str) -> tuple[str, str, str]:
    parts = group_key.split(":")
    if len(parts) != 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="group_key must be root_cause:action:booking_vehicle",
        )
    return parts[0], parts[1], parts[2]


async def _get_run_or_404(db: AsyncSession, tenant_id, run_id_str: str) -> ReconciliationRun:
    run_uuid = _parse_uuid(run_id_str)
    run = (
        await db.execute(
            select(ReconciliationRun).where(
                ReconciliationRun.id == run_uuid,
                ReconciliationRun.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


@router.get("/runs/{run_id}/resolution-summary", response_model=ResolutionSummaryResponse)
async def get_resolution_summary(
    run_id: str,
    user: Annotated[User, Depends(require_feature("reconciliation"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Summary-first payload: rates + variance-by-root-cause + computed groups.

    Groups are computed here by GROUP BY on real columns (never parsed out of
    group_key). Only ACTIVE, undecided-or-approved proposal states are shown;
    superseded/rejected history is excluded.
    """
    run = await _get_run_or_404(db, user.tenant_id, run_id)
    run_uuid = run.id

    total_results = (
        await db.execute(
            select(func.count(ReconciliationResult.id)).where(
                ReconciliationResult.run_id == run_uuid,
                ReconciliationResult.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one()
    matches_count = run.matches_count

    P = ReconResolutionProposal
    live = (P.run_id == run_uuid, P.tenant_id == user.tenant_id, P.status.notin_(("superseded", "rejected")))

    group_rows = (
        await db.execute(
            select(
                P.root_cause,
                P.action,
                P.booking_vehicle,
                P.group_key,
                func.count(P.id).label("count"),
                func.count(P.id).filter(P.status == "proposed").label("proposed_count"),
                func.count(P.id).filter(P.status == "approved").label("approved_count"),
                func.coalesce(func.sum(P.proposed_amount), 0).label("total_amount"),
                func.count(P.id).filter(P.above_materiality.is_(True)).label("above_materiality_count"),
            )
            .where(*live)
            .group_by(P.root_cause, P.action, P.booking_vehicle, P.group_key)
            .order_by(func.coalesce(func.sum(P.proposed_amount), 0).desc())
        )
    ).all()

    groups = [
        ResolutionGroupSummary(
            group_key=r.group_key,
            root_cause=r.root_cause,
            action=r.action,
            booking_vehicle=r.booking_vehicle,
            count=r.count,
            proposed_count=r.proposed_count,
            approved_count=r.approved_count,
            total_amount=r.total_amount,
            above_materiality_count=r.above_materiality_count,
        )
        for r in group_rows
    ]
    proposals_count = sum(g.count for g in groups)
    explained_count = sum(g.count for g in groups if g.action != "needs_human")
    variance_by_root_cause: dict[str, Decimal] = {}
    for g in groups:
        variance_by_root_cause[g.root_cause] = variance_by_root_cause.get(g.root_cause, Decimal("0")) + g.total_amount

    def _pct(numerator: int, denominator: int) -> Decimal:
        if denominator == 0:
            return Decimal("0")
        return (Decimal(numerator) / Decimal(denominator) * 100).quantize(Decimal("0.1"))

    # guard-skip visibility: results with no live proposal, no match, and a
    # posted proposal elsewhere are reported by plan_run's audit; recompute the
    # cheap upper bound here for the header (results minus matches minus live).
    guard_skipped_count = max(0, total_results - matches_count - proposals_count)

    return ResolutionSummaryResponse(
        run_id=run_id,
        total_results=total_results,
        matches_count=matches_count,
        match_rate=_pct(matches_count, total_results),
        proposals_count=proposals_count,
        explained_count=explained_count,
        explained_rate=_pct(explained_count, proposals_count),
        guard_skipped_count=guard_skipped_count,
        variance_by_root_cause=variance_by_root_cause,
        groups=groups,
    )


@router.get(
    "/runs/{run_id}/resolution-groups/{group_key}/proposals",
    response_model=list[ResolutionProposalResponse],
)
async def list_group_proposals(
    run_id: str,
    group_key: str,
    user: Annotated[User, Depends(require_feature("reconciliation"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 100,
    offset: int = 0,
):
    await _get_run_or_404(db, user.tenant_id, run_id)
    root_cause, action, vehicle = _parse_group_key(group_key)
    P = ReconResolutionProposal
    rows = (
        (
            await db.execute(
                select(P)
                .where(
                    P.run_id == _parse_uuid(run_id),
                    P.tenant_id == user.tenant_id,
                    P.root_cause == root_cause,
                    P.action == action,
                    P.booking_vehicle == vehicle,
                    P.status.notin_(("superseded", "rejected")),
                )
                .order_by(P.proposed_amount.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return [ResolutionProposalResponse.model_validate(p) for p in rows]


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

    stmt = select(ReconciliationResult).where(
        ReconciliationResult.run_id == uuid.UUID(run_id),
        ReconciliationResult.tenant_id == user.tenant_id,
    )
    result = await db.execute(stmt)
    results = result.scalars().all()

    results_dicts = [
        {
            "id": str(r.id),
            "match_type": r.match_type,
            "confidence": r.confidence,
            "status": r.status,
            "bucket": r.bucket,
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
# Close readiness (period-scoped) + close period
# ---------------------------------------------------------------------------
@router.get("/close-readiness/{period}", response_model=ReconCloseReadiness)
async def get_close_readiness(
    period: str,
    user: Annotated[User, Depends(require_feature("reconciliation"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Period-scoped readiness counts for the FE CloseChecklist (R3-A).

    ``POST /close/{period}`` closes EVERY in-scope run in the month, so the
    gate aggregates over the results of ALL of them — the exact same run
    selection (``close_scope.closeable_runs_conditions``) the close will use.
    Zero in-scope runs → all zeros (a read, not close_period's 404). Every
    count keys on the authoritative status/bucket only, never the advisory
    confidence composite.
    """
    try:
        run_conditions = closeable_runs_conditions(user.tenant_id, period)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Period must be YYYY-MM format",
        )

    in_scope_run_ids = select(ReconciliationRun.id).where(*run_conditions)

    # ONE statement, single snapshot: count FILTER (WHERE ...) aggregates over
    # the results of every in-scope run, plus the in-scope run ids as an
    # uncorrelated array_agg scalar subquery — the gate's counts and run set
    # can never disagree with each other. runs_in_scope is derived from that
    # same array (R4-A: the FE needs the IDS, not just a count — with zero
    # in-scope runs every count is vacuously zero and a count-only gate fails
    # OPEN; the FE checks the selected run is a member).
    # Results are tenant-scoped directly too (defense in depth alongside the
    # tenant-scoped run selection: a cross-tenant row seeded on an in-scope run
    # must not count).
    row = (
        await db.execute(
            select(
                select(func.array_agg(ReconciliationRun.id))
                .where(*run_conditions)
                .scalar_subquery()
                .label("in_scope_run_ids"),
                # Open exceptions: a pending row on a MATCHED line. Pending+
                # unmatched rows are expected exceptions already surfaced in
                # the needs_review bucket.
                func.count()
                .filter(
                    and_(
                        ReconciliationResult.status == "pending",
                        ReconciliationResult.match_type != "unmatched",
                    )
                )
                .label("open_exceptions"),
                func.count().filter(ReconciliationResult.status == "suggested").label("suggested"),
                # Rows close deliberately leaves UNLOCKED (HITL) — the shared
                # predicate close_period() skips by.
                func.count().filter(and_(*left_for_review_conditions())).label("left_for_review"),
            )
            .select_from(ReconciliationResult)
            .where(
                ReconciliationResult.tenant_id == user.tenant_id,
                ReconciliationResult.run_id.in_(in_scope_run_ids),
            )
        )
    ).one()

    # array_agg over zero rows is NULL; sorted for determinism (the FE only
    # does a membership check).
    scope_ids = sorted(str(rid) for rid in (row.in_scope_run_ids or []))

    return ReconCloseReadiness(
        period=period,
        runs_in_scope=len(scope_ids),
        in_scope_run_ids=scope_ids,
        open_exceptions=row.open_exceptions,
        suggested=row.suggested,
        left_for_review=row.left_for_review,
    )


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
        # Shared with GET /close-readiness/{period} via close_scope — the FE
        # gate and this mutation must select the SAME runs (R3-A).
        run_conditions = closeable_runs_conditions(user.tenant_id, period)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Period must be YYYY-MM format",
        )

    stmt = select(ReconciliationRun).where(*run_conditions)
    result = await db.execute(stmt)
    runs = result.scalars().all()

    if not runs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No completed reconciliation runs found for {period}",
        )

    run_ids = [run.id for run in runs]

    # Lock a result iff it was approved (a human reviewed it — even a
    # needs_review line that was single-approved), OR it is an auto_matched
    # line that is NOT left for review. A confident match with a MATERIAL
    # variance (status='auto_matched', bucket='needs_review') is left
    # unlocked so the discrepancy is not silently buried on close (HITL).
    # The left-for-review predicate is shared with the readiness endpoint
    # via close_scope (single source of truth).
    lock_predicate = or_(
        ReconciliationResult.status == "approved",
        and_(
            ReconciliationResult.status == "auto_matched",
            not_(and_(*left_for_review_conditions())),
        ),
    )

    # ONE set-based UPDATE over every in-scope run (R4-A): the previous
    # per-row ORM flip loaded + flushed each result individually, which at
    # production scale (62.5k-row runs) risks the Supabase 2-min statement
    # timeout. rowcount = rows locked (pattern: approve_bucket's server-side
    # UPDATE). synchronize_session=False is safe: this request reads no
    # result ORM objects after the flip.
    locked_count = (
        await db.execute(
            update(ReconciliationResult)
            .where(
                ReconciliationResult.run_id.in_(run_ids),
                ReconciliationResult.tenant_id == user.tenant_id,
                lock_predicate,
            )
            .values(status="locked")
            .execution_options(synchronize_session=False)
        )
    ).rowcount

    # Count the auto_matched + needs_review lines deliberately left unlocked,
    # so the close response + audit trail make the skipped items visible.
    # Same shared predicate the readiness endpoint counts by (close_scope).
    left_for_review_count = (
        await db.execute(
            select(func.count())
            .select_from(ReconciliationResult)
            .where(
                ReconciliationResult.run_id.in_(run_ids),
                ReconciliationResult.tenant_id == user.tenant_id,
                *left_for_review_conditions(),
            )
        )
    ).scalar_one()

    for run in runs:
        run.status = "closed"

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="reconciliation",
        action="recon.close_period",
        actor_id=user.id,
        resource_type="reconciliation_period",
        resource_id=period,
        payload={"results_left_for_review": left_for_review_count},
    )
    await db.commit()

    return {
        "period": period,
        "runs_closed": len(runs),
        "results_locked": locked_count,
        "results_left_for_review": left_for_review_count,
        "message": (f"Period {period} closed. {locked_count} results locked, {left_for_review_count} left for review."),
    }
