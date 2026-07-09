"""Planner runs automatically post-run and is retryable via endpoint."""

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.api.v1.reconciliation import plan_resolutions
from app.models.reconciliation import ReconciliationRun, ReconResolutionProposal
from app.schemas.order_reconciliation import ChargeRecord, OrderMatchCandidate
from app.services.reconciliation.order_recon_job import OrderReconJob
from tests.conftest import (
    create_test_recon_result,
    create_test_recon_run,
    create_test_user,
    enable_feature_flag,
)


async def test_plan_resolutions_endpoint_plans_a_completed_run(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("3.20"),
        stripe_amount=Decimal("100.00"),
        netsuite_amount=Decimal("96.80"),
        evidence={"charge_source_id": "ch_1", "order_reference": "R1"},
    )
    await db.flush()

    out = await plan_resolutions(str(run.id), user=user, db=db)

    assert out["planned_count"] == 1
    props = (
        (await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)))
        .scalars()
        .all()
    )
    assert len(props) == 1


async def test_plan_resolutions_404_on_foreign_run(db, tenant_a, tenant_b):
    user, _ = await create_test_user(db, tenant_a)
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    run_b = await create_test_recon_run(db, tenant_b.id, status="completed")
    await db.flush()
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await plan_resolutions(str(run_b.id), user=user, db=db)
    assert exc.value.status_code == 404


async def test_plan_resolutions_rejected_on_closed_run(db, tenant_a):
    """Close = hard freeze: re-planning must not supersede/re-derive proposals
    once the run is closed — mirrors the guard on approve/reject/override."""
    import pytest
    from fastapi import HTTPException

    user, _ = await create_test_user(db, tenant_a)
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    run = await create_test_recon_run(db, tenant_a.id, status="closed")
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("3.20"),
        stripe_amount=Decimal("100.00"),
        netsuite_amount=Decimal("96.80"),
        evidence={"charge_source_id": "ch_1", "order_reference": "R1"},
    )
    await db.flush()

    with pytest.raises(HTTPException) as exc:
        await plan_resolutions(str(run.id), user=user, db=db)
    assert exc.value.status_code == 400
    assert "closed" in exc.value.detail.lower()

    props = (
        (await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)))
        .scalars()
        .all()
    )
    assert props == []


async def test_plan_resolutions_maps_integrity_error_to_409(db, tenant_a):
    """T2 gate finding (round 4): plan_run races itself under concurrent
    calls for the same run — the second caller can still hit the partial
    unique index and raise IntegrityError even beneath plan_run's own
    advisory-lock serialization (e.g. a caller on an older code path, or a
    lock-acquisition edge case). The endpoint must roll back the poisoned
    transaction and surface a clean 409, not a raw 500."""
    import pytest
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    user, _ = await create_test_user(db, tenant_a)
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await db.flush()

    db.rollback = AsyncMock()
    with (
        patch(
            "app.api.v1.reconciliation.plan_run",
            side_effect=IntegrityError("", "", Exception()),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await plan_resolutions(str(run.id), user=user, db=db)

    assert exc.value.status_code == 409
    db.rollback.assert_awaited_once()


async def test_plan_run_hook_reestablishes_tenant_context_before_planning(db, tenant_a):
    """The finalize commit clears the transaction-scoped SET LOCAL
    app.current_tenant_id; the hook must re-establish it (same helper used by
    recon_envelope_dry_run.py) BEFORE calling plan_run, or plan_run's INSERT
    into the FORCE-RLS'd recon_resolution_proposals table silently writes
    nothing on a non-BYPASSRLS role. Spies on set_tenant_context and plan_run
    where the hook resolves them (both are local imports re-resolved at call
    time) and asserts ordering + the tenant_id argument."""
    charge = ChargeRecord(
        id="pl-1",
        source_id="ch_hook_2",
        payout_line_id="pl-1",
        amount=Decimal("50.00"),
        fee=Decimal("1.50"),
        net=Decimal("48.50"),
        currency="USD",
        charge_date=date(2026, 3, 15),
    )
    unmatched_candidate = OrderMatchCandidate(
        charge=charge,
        deposit=None,
        match_type="unmatched",
        confidence=Decimal("0"),
        variance_amount=Decimal("50.00"),
        variance_type="missing",
    )

    job = OrderReconJob(db=db, tenant_id=str(tenant_a.id))
    call_order: list[tuple[str, str]] = []

    async def fake_set_tenant_context(session, tenant_id):
        call_order.append(("set_tenant_context", tenant_id))

    async def fake_plan_run(session, tenant_id, run_id):
        call_order.append(("plan_run", tenant_id))
        return {"planned_count": 0}

    with (
        patch.object(job, "_fetch_charges", return_value=[charge]),
        patch.object(job, "_fetch_deposits", return_value=[]),
        patch.object(job.engine, "match", return_value=[unmatched_candidate]),
        patch("app.services.reconciliation.resolution_planner.plan_run", side_effect=fake_plan_run),
        patch("app.core.database.set_tenant_context", side_effect=fake_set_tenant_context),
    ):
        await job.run(
            date_from=date(2026, 3, 10),
            date_to=date(2026, 3, 20),
        )

    assert call_order == [
        ("set_tenant_context", job.tenant_id),
        ("plan_run", job.tenant_id),
    ]


async def test_plan_run_exception_leaves_run_completed_with_no_proposals(db, tenant_a):
    """OrderReconJob.run()'s post-finalize hook must never fail the run.

    Drives the full job.run() against the real (savepoint-rolled-back) test DB
    — mirroring TestRunProducesSummary in test_order_recon_job.py, which
    patches _fetch_charges/_fetch_deposits/engine.match on a mocked db. Here we
    use the real `db`/`tenant_a` fixtures instead, so the finalize commit and
    the hook's own rollback are exercised against a real session/transaction,
    and we can assert directly on persisted rows (run.status, proposal count).
    plan_run is patched where the hook imports it from
    (`resolution_planner.plan_run`), since the hook does a local
    `from ... import plan_run` that re-resolves the name at call time.
    """
    charge = ChargeRecord(
        id="pl-1",
        source_id="ch_hook_1",
        payout_line_id="pl-1",
        amount=Decimal("50.00"),
        fee=Decimal("1.50"),
        net=Decimal("48.50"),
        currency="USD",
        charge_date=date(2026, 3, 15),
    )
    unmatched_candidate = OrderMatchCandidate(
        charge=charge,
        deposit=None,
        match_type="unmatched",
        confidence=Decimal("0"),
        variance_amount=Decimal("50.00"),
        variance_type="missing",
    )

    job = OrderReconJob(db=db, tenant_id=str(tenant_a.id))

    with (
        patch.object(job, "_fetch_charges", return_value=[charge]),
        patch.object(job, "_fetch_deposits", return_value=[]),
        patch.object(job.engine, "match", return_value=[unmatched_candidate]),
        patch(
            "app.services.reconciliation.resolution_planner.plan_run",
            side_effect=RuntimeError("boom"),
        ),
    ):
        summary = await job.run(
            date_from=date(2026, 3, 10),
            date_to=date(2026, 3, 20),
        )

    assert summary.status == "completed"

    run = (
        await db.execute(select(ReconciliationRun).where(ReconciliationRun.id == uuid.UUID(summary.run_id)))
    ).scalar_one()
    assert run.status == "completed"

    props = (
        (
            await db.execute(
                select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == uuid.UUID(summary.run_id))
            )
        )
        .scalars()
        .all()
    )
    assert props == []
