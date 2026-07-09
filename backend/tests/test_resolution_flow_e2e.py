"""Phase 1 e2e: seed → plan → summary → group approve → close readiness.

The T2 regression backbone for the summary-first rework. NetSuite is never
touched (Phase 1 is DB-only by design)."""

from decimal import Decimal

from app.api.v1.reconciliation import (
    approve_resolution_group,
    get_close_readiness,
    get_resolution_summary,
    plan_resolutions,
)
from app.schemas.reconciliation import ResolutionGroupApprove
from tests.conftest import create_test_recon_result, create_test_recon_run, create_test_user


async def test_summary_first_flow_end_to_end(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    run = await create_test_recon_run(db, tenant_a.id, status="completed")

    # $9 fee on a $1000 order = sub-materiality → one-click group-approvable.
    fee = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="auto_classifications",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("9.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("991.00"),
        evidence={"charge_source_id": "ch_fee", "order_reference": "R1"},
    )
    timing = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="rules",
        match_type="fuzzy",
        variance_type="timing",
        variance_amount=Decimal("0"),
        stripe_amount=Decimal("50.00"),
        netsuite_amount=Decimal("50.00"),
        evidence={"charge_source_id": "ch_time", "order_reference": "R2"},
    )
    chargeback = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="chargeback",
        variance_amount=Decimal("42.00"),
        stripe_amount=Decimal("42.00"),
        netsuite_amount=Decimal("0"),
        evidence={"charge_source_id": "ch_cb", "order_reference": "R3"},
    )
    await db.flush()

    # 1. plan
    plan = await plan_resolutions(str(run.id), user=user, db=db)
    assert plan["planned_count"] == 3

    # 2. summary-first payload
    summary = await get_resolution_summary(str(run.id), user=user, db=db)
    assert summary.proposals_count == 3
    assert summary.explained_count == 2  # chargeback stays needs_human

    # 3. approve fees, acknowledge timing
    await approve_resolution_group(
        str(run.id),
        "fees:book_fee_line:deposit",
        ResolutionGroupApprove(notes="e2e"),
        user=user,
        db=db,
    )
    await approve_resolution_group(
        str(run.id),
        "timing:carry_forward:none",
        ResolutionGroupApprove(),
        user=user,
        db=db,
    )

    for r in (fee, timing, chargeback):
        await db.refresh(r)
    assert fee.status == "approved"
    assert timing.status == "carried_forward"
    assert chargeback.status == "pending"  # untouched — needs human

    # 4. close readiness reflects the flow: only the chargeback blocks
    readiness = await get_close_readiness("2026-04", user=user, db=db)
    assert readiness.carried_forward == 1
    assert readiness.open_exceptions == 1  # the pending chargeback
