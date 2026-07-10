"""Shared group-approve core — extracted from the REST endpoint so the chat
`recon.approve_group` tool (Task 7) can reuse the exact same behavior instead
of re-implementing the group-parse/eligibility/audit invariants.

``approve_group_core`` is the ENTIRE body of the resolution-group approve
endpoint, parameterized on plain ``tenant_id``/``actor_id`` (no FastAPI
``User``/``Depends`` object) so it is callable directly from a Celery task,
a chat tool, or a test — not just via HTTP. The REST endpoint
(`app.api.v1.reconciliation.approve_resolution_group`) is now a thin wrapper
that unpacks ``user.tenant_id``/``user.id`` and delegates here. Behavior is
byte-identical to the pre-extraction endpoint: same UI-flag gate, same
run-open guard, same eligibility predicate, same result-flip subquery, same
per-line + bulk audit events.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import exists, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.models.reconciliation import ReconciliationResult, ReconciliationRun, ReconResolutionProposal
from app.schemas.reconciliation import ResolutionGroupApproveResult
from app.services import audit_service
from app.services.reconciliation.four_bucket_classifier import CLOSED_RUN_STATUSES, TERMINAL_RESULT_STATUSES


def _parse_uuid(value: str) -> uuid.UUID:
    """Parse a path/body UUID, raising 404 (not 500) on a malformed id."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _parse_group_key(group_key: str) -> tuple[str, str, str]:
    parts = group_key.split(":")
    if len(parts) != 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="group_key must be root_cause:action:booking_vehicle",
        )
    return parts[0], parts[1], parts[2]


def _ensure_run_open(run: ReconciliationRun | None) -> None:
    """Close = hard freeze. Raise if the run is closed/locked; a ``None`` run
    is a no-op (the caller either already 404'd on a missing run, or derived
    the run from a tenant-scoped child row and treats a missing run as not
    blocking)."""
    if run is not None and run.status in CLOSED_RUN_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Period is closed; cannot approve.")


async def _ensure_resolution_ui_enabled(db: AsyncSession, tenant_id) -> None:
    """Flag-gate the resolution-plan mutation endpoints behind
    recon_resolution_ui (default OFF) — the redesigned surface, independent of
    the base ``reconciliation`` feature. A body-level check rather than a
    second ``Depends``: these endpoints/tools are exercised directly as plain
    function calls throughout the test suite (bypassing FastAPI's dependency
    injection), so a Depends-only gate would never actually run under a
    direct call."""
    from app.services.feature_flag_service import is_enabled

    if not await is_enabled(db, tenant_id, "recon_resolution_ui"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Feature 'recon_resolution_ui' is not enabled for your account.",
        )


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


async def approve_group_core(
    db: AsyncSession,
    *,
    tenant_id,
    actor_id,
    run_id: str,
    group_key: str,
    notes: str | None,
    included_above_materiality_ids: list[str],
    excluded_ids: list[str],
    currency: str | None,
) -> ResolutionGroupApproveResult:
    """Set-based approve of a resolution group. DB-only (no posting).

    Above-materiality proposals approve ONLY when explicitly ticked
    (included_above_materiality_ids). carry_forward groups flip results to
    'carried_forward'; every other approvable action flips to 'approved'.
    needs_human groups are never group-approvable.
    """
    await _ensure_resolution_ui_enabled(db, tenant_id)
    root_cause, action, vehicle = _parse_group_key(group_key)
    if action == "needs_human":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="needs_human groups must be resolved individually",
        )
    run = await _get_run_or_404(db, tenant_id, run_id)
    _ensure_run_open(run)

    now = datetime.now(timezone.utc)
    correlation_id = str(uuid.uuid4())
    P = ReconResolutionProposal
    included = {_parse_uuid(i) for i in included_above_materiality_ids}
    excluded = {_parse_uuid(i) for i in excluded_ids}

    group_filter = (
        P.run_id == run.id,
        P.tenant_id == tenant_id,
        P.root_cause == root_cause,
        P.action == action,
        P.booking_vehicle == vehicle,
    )
    # A group_key alone can now span more than one currency (multi-currency runs
    # render one card per currency) — scope to just this currency when the
    # caller sends one; omitted matches every currency (back-compat).
    if currency:
        group_filter = (*group_filter, P.currency == currency)
    eligibility = (
        or_(P.above_materiality.is_(False), P.id.in_(included)) if included else P.above_materiality.is_(False)
    )
    # Build the where-clause list conditionally rather than pass
    # `P.id.notin_(set())` unconditionally — an empty NOT IN renders as a
    # vacuous/NULL-poisoned predicate on some dialects.
    exclusion_clause = [P.id.notin_(excluded)] if excluded else []
    # A result can go terminal independently of this proposal (e.g. locked via
    # the classic per-result approve path) while its proposal row is still
    # 'proposed'. Skip those instead of flipping the proposal — otherwise the
    # proposal is marked approved + per-line audited for a result this
    # group-approve never actually touched.
    not_terminal_result = ~exists().where(
        ReconciliationResult.id == P.result_id,
        ReconciliationResult.tenant_id == tenant_id,
        ReconciliationResult.status.in_(TERMINAL_RESULT_STATUSES),
    )

    upd = (
        update(P)
        .where(
            *group_filter,
            P.status == "proposed",
            eligibility,
            *exclusion_clause,
            not_terminal_result,
        )
        .values(status="approved", decided_by=actor_id, decided_at=now, correlation_id=correlation_id)
        .returning(P.id)
    )
    approved_ids = (await db.execute(upd)).scalars().all()

    total_in_group = (
        await db.execute(select(func.count(P.id)).where(*group_filter, P.status.notin_(("superseded", "rejected"))))
    ).scalar_one()
    skipped_count = total_in_group - len(approved_ids)

    # Flip the underlying results (never terminal rows). Scoped via a correlated
    # subquery on this batch's correlation_id rather than a Python id list — at
    # Framework scale (>32,767 approved rows) an `IN (<python list>)` blows the
    # asyncpg bind-parameter limit; the freshly-approved proposals already carry
    # this batch's unique correlation_id, so the subquery re-derives the same set.
    if approved_ids:
        result_status = "carried_forward" if action == "carry_forward" else "approved"
        values = {"status": result_status}
        if result_status == "approved":
            values.update(approved_by=actor_id, approved_at=now)
        approved_result_ids = select(P.result_id).where(
            P.tenant_id == tenant_id,
            P.correlation_id == correlation_id,
            P.status == "approved",
        )
        await db.execute(
            update(ReconciliationResult)
            .where(
                ReconciliationResult.id.in_(approved_result_ids),
                ReconciliationResult.tenant_id == tenant_id,
                ReconciliationResult.status.notin_(TERMINAL_RESULT_STATUSES),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )

    if approved_ids:
        await db.execute(
            insert(AuditEvent),
            [
                {
                    "tenant_id": tenant_id,
                    "actor_id": actor_id,
                    "actor_type": "user",
                    "category": "reconciliation",
                    "action": "recon.resolution.approve",
                    "resource_type": "recon_resolution_proposal",
                    "resource_id": str(pid),
                    "correlation_id": correlation_id,
                    "status": "success",
                }
                for pid in approved_ids
            ],
        )
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="reconciliation",
        action="recon.resolution.bulk_approve",
        actor_id=actor_id,
        resource_type="reconciliation_run",
        resource_id=run_id,
        correlation_id=correlation_id,
        payload={"group_key": group_key, "approved_count": len(approved_ids), "notes": notes},
    )
    await db.commit()
    return ResolutionGroupApproveResult(
        run_id=run_id,
        group_key=group_key,
        approved_count=len(approved_ids),
        skipped_count=skipped_count,
        correlation_id=correlation_id,
    )
