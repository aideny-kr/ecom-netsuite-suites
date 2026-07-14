"""plan_run orchestrator: supersede-then-insert, cross-run guard, audit event."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.reconciliation import ReconResolutionProposal
from app.services.reconciliation.group_actions import approve_group_core
from app.services.reconciliation.resolution_planner import plan_run
from tests.conftest import (
    create_test_payout_line,
    create_test_recon_result,
    create_test_recon_run,
    create_test_user,
    enable_feature_flag,
)


async def _result(db, tenant_id, run_id, **over):
    defaults = dict(
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("3.20"),
        stripe_amount=Decimal("100.00"),
        netsuite_amount=Decimal("96.80"),
        evidence={"charge_source_id": f"ch_{uuid.uuid4().hex[:8]}", "order_reference": "R123456789"},
    )
    defaults.update(over)
    return await create_test_recon_result(db, tenant_id, run_id, **defaults)


async def test_plan_run_writes_proposals_for_non_matches(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(db, tenant_a.id, run.id, variance_type="fees", bucket="auto_classifications")
    await _result(db, tenant_a.id, run.id, variance_type="timing", variance_amount=Decimal("0"), bucket="rules")
    # a clean match must NOT get a proposal
    await _result(
        db,
        tenant_a.id,
        run.id,
        variance_type=None,
        variance_amount=Decimal("0"),
        bucket="matches",
        status="auto_matched",
    )
    await db.flush()

    out = await plan_run(db, tenant_a.id, run.id)

    props = (
        (await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)))
        .scalars()
        .all()
    )
    assert out["planned_count"] == 2
    assert len(props) == 2
    assert {p.action for p in props} == {"book_fee_line", "carry_forward"}
    assert all(p.status == "proposed" and p.source == "planner" for p in props)
    assert all(p.charge_source_id for p in props)


async def test_plan_run_is_idempotent_via_supersede(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(db, tenant_a.id, run.id)
    await db.flush()
    await plan_run(db, tenant_a.id, run.id)
    out2 = await plan_run(db, tenant_a.id, run.id)  # re-plan must not violate the active-unique index

    props = (
        (await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)))
        .scalars()
        .all()
    )
    assert out2["superseded_count"] == 1
    assert sorted(p.status for p in props) == ["proposed", "superseded"]


async def test_plan_run_never_supersedes_decided_proposals(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(db, tenant_a.id, run.id)
    await db.flush()
    await plan_run(db, tenant_a.id, run.id)
    prop = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id))
    ).scalar_one()
    prop.status = "approved"
    await db.flush()

    out = await plan_run(db, tenant_a.id, run.id)
    await db.refresh(prop)
    assert prop.status == "approved"  # decided rows untouched
    assert out["planned_count"] == 0  # its result is not re-planned either


async def test_plan_run_cross_run_posted_guard(db, tenant_a):
    run1 = await create_test_recon_run(db, tenant_a.id, status="completed")
    r1 = await _result(db, tenant_a.id, run1.id, evidence={"charge_source_id": "ch_posted", "order_reference": "R1"})
    await db.flush()
    await plan_run(db, tenant_a.id, run1.id)
    p1 = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.result_id == r1.id))
    ).scalar_one()
    p1.status = "posted"
    await db.flush()

    run2 = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(db, tenant_a.id, run2.id, evidence={"charge_source_id": "ch_posted", "order_reference": "R1"})
    await db.flush()
    out = await plan_run(db, tenant_a.id, run2.id)
    assert out["skipped_guard_count"] == 1
    assert out["planned_count"] == 0


async def test_plan_run_cross_run_guard_covers_approved_status(db, tenant_a):
    """T2 gate finding: the guard must cover decided-or-in-flight statuses, not
    just 'posted' — a charge approved (but not yet posted) in run 1 must still
    guard run 2, otherwise the same charge gets two independent proposals
    racing toward NetSuite before run 1's posting even starts."""
    run1 = await create_test_recon_run(db, tenant_a.id, status="completed")
    r1 = await _result(db, tenant_a.id, run1.id, evidence={"charge_source_id": "ch_approved", "order_reference": "R1"})
    await db.flush()
    await plan_run(db, tenant_a.id, run1.id)
    p1 = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.result_id == r1.id))
    ).scalar_one()
    p1.status = "approved"  # decided, not yet posted
    await db.flush()

    run2 = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(db, tenant_a.id, run2.id, evidence={"charge_source_id": "ch_approved", "order_reference": "R1"})
    await db.flush()
    out = await plan_run(db, tenant_a.id, run2.id)
    assert out["skipped_guard_count"] == 1
    assert out["planned_count"] == 0


async def test_plan_run_replans_approved_carry_forward_after_lag_window(db, tenant_a):
    """T2 gate finding: an APPROVED carry_forward proposal must not
    permanently suppress its charge from all future planning via the
    cross-run decided_charge_ids guard. carry_forward is an acknowledged
    timing item, not a commitment toward NetSuite — the guard exists to
    prevent double-POSTING, and carry_forward never posts. A
    missing_in_netsuite charge carried forward as sync-lag in run 1 must
    still be re-surfaced (fresh create_and_apply_deposit) in a later run once
    the recency window passes and the deposit still hasn't arrived."""
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    user, _ = await create_test_user(db, tenant_a)
    charge_source_id = f"ch_{uuid.uuid4().hex[:8]}"

    recent_line = await create_test_payout_line(db, tenant_a.id, arrival_date=date.today() - timedelta(days=1))
    run1 = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(
        db,
        tenant_a.id,
        run1.id,
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("9.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=None,
        evidence={
            "charge_source_id": charge_source_id,
            "order_reference": "R1",
            "charge_payout_line_id": str(recent_line.id),
        },
    )
    await db.flush()
    await plan_run(db, tenant_a.id, run1.id)

    prop1 = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run1.id))
    ).scalar_one()
    assert prop1.action == "carry_forward"

    await approve_group_core(
        db,
        tenant_id=tenant_a.id,
        actor_id=user.id,
        run_id=str(run1.id),
        group_key=prop1.group_key,
        notes=None,
        included_above_materiality_ids=[],
        excluded_ids=[],
        currency=None,
    )
    await db.refresh(prop1)
    assert prop1.status == "approved"

    # Deposit STILL hasn't arrived: same charge, a later run, days_since_payout
    # now beyond the recency window.
    old_line = await create_test_payout_line(db, tenant_a.id, arrival_date=date.today() - timedelta(days=30))
    run2 = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(
        db,
        tenant_a.id,
        run2.id,
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("9.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=None,
        evidence={
            "charge_source_id": charge_source_id,
            "order_reference": "R1",
            "charge_payout_line_id": str(old_line.id),
        },
    )
    await db.flush()

    out = await plan_run(db, tenant_a.id, run2.id)

    assert out["skipped_guard_count"] == 0
    assert out["planned_count"] == 1
    prop2 = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run2.id))
    ).scalar_one()
    assert prop2.action == "create_and_apply_deposit"


async def test_plan_run_preserves_human_override_after_replan(db, tenant_a):
    """T2 gate finding: the supersede UPDATE must not flip source='human'
    override proposals — a re-plan must not discard a human decision. Mirrors
    override_resolution_proposal's semantics directly (supersede the planner
    row, insert a new active source='human' row for the same result) rather
    than calling the endpoint, so this test stays independent of the
    endpoint's own feature-flag gating."""
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    result = await _result(db, tenant_a.id, run.id)
    await db.flush()
    await plan_run(db, tenant_a.id, run.id)

    original = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.result_id == result.id))
    ).scalar_one()
    original.status = "superseded"
    human_override = ReconResolutionProposal(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        run_id=run.id,
        result_id=result.id,
        root_cause=original.root_cause,
        action="needs_human",
        booking_vehicle="none",
        group_key="fees:needs_human:none",
        source="human",
        narrative="Overridden by user.",
        proposed_amount=original.proposed_amount,
        currency=original.currency,
        above_materiality=original.above_materiality,
        status="proposed",
        charge_source_id=original.charge_source_id,
    )
    db.add(human_override)
    await db.flush()

    out = await plan_run(db, tenant_a.id, run.id)
    await db.refresh(human_override)
    assert human_override.status == "proposed"
    assert human_override.source == "human"

    active_props_for_result = (
        (
            await db.execute(
                select(ReconResolutionProposal).where(
                    ReconResolutionProposal.result_id == result.id,
                    ReconResolutionProposal.status.notin_(("superseded", "rejected")),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(active_props_for_result) == 1
    assert active_props_for_result[0].id == human_override.id
    assert out["planned_count"] == 0  # result excluded — the human proposal protected it


async def test_plan_run_does_not_resurrect_rejected_proposal(db, tenant_a):
    """T2 gate finding: a human rejection is a standing decision within THIS
    run — decided_result_ids must also exclude 'rejected' rows, or a re-plan
    silently reverses the rejection by inserting a fresh identical proposal
    for the same result. (A future run — new run_id, new results — still
    plans fresh; this guard is run-scoped.)"""
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    result = await _result(db, tenant_a.id, run.id)
    await db.flush()
    await plan_run(db, tenant_a.id, run.id)

    prop = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.result_id == result.id))
    ).scalar_one()
    prop.status = "rejected"
    await db.flush()

    out = await plan_run(db, tenant_a.id, run.id)
    await db.refresh(prop)
    assert prop.status == "rejected"

    proposed_props_for_result = (
        (
            await db.execute(
                select(ReconResolutionProposal).where(
                    ReconResolutionProposal.result_id == result.id,
                    ReconResolutionProposal.status == "proposed",
                )
            )
        )
        .scalars()
        .all()
    )
    assert proposed_props_for_result == []
    assert out["planned_count"] == 0


async def test_plan_run_emits_summary_audit_event(db, tenant_a):
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(db, tenant_a.id, run.id)
    await db.flush()
    await plan_run(db, tenant_a.id, run.id)
    evt = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "recon.resolution.planned",
                    AuditEvent.resource_id == str(run.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(evt) == 1
    assert evt[0].actor_type == "system"
    assert evt[0].payload["planned_count"] == 1


# ---------------------------------------------------------------------------
# Task 2: batched fee/recency enrichment (payout_lines/payouts lookup)
# ---------------------------------------------------------------------------


async def test_plan_run_enriches_fee_amount_and_emits_book_fee_line(db, tenant_a):
    """A payout_line carrying a fee close to the variance amount must flow
    through plan_run's batched lookup into plan_result's fee_amount arg,
    producing book_fee_line (rule 7b) end-to-end — not needs_human."""
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    payout_line = await create_test_payout_line(db, tenant_a.id, fee=Decimal("3.20"))
    await _result(
        db,
        tenant_a.id,
        run.id,
        variance_type="amount_mismatch",
        variance_amount=Decimal("3.00"),  # within FEE_EXPLAIN_TOLERANCE (0.50) of fee 3.20
        stripe_amount=Decimal("100.00"),
        netsuite_amount=Decimal("97.00"),
        evidence={
            "charge_source_id": f"ch_{uuid.uuid4().hex[:8]}",
            "charge_payout_line_id": str(payout_line.id),
        },
    )
    await db.flush()

    out = await plan_run(db, tenant_a.id, run.id)

    prop = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id))
    ).scalar_one()
    assert prop.action == "book_fee_line"
    assert prop.root_cause == "amount_mismatch"
    assert out["by_action"]["book_fee_line"] == 1


async def test_plan_run_enriches_recent_arrival_date_and_emits_carry_forward(db, tenant_a):
    """A payout arriving within RECENT_PAYOUT_LAG_DAYS must flow through
    plan_run's batched lookup into plan_result's days_since_payout arg,
    producing carry_forward for a missing_in_netsuite row instead of the
    default create_and_apply_deposit/needs_human path."""
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    payout_line = await create_test_payout_line(db, tenant_a.id, arrival_date=date.today() - timedelta(days=1))
    await _result(
        db,
        tenant_a.id,
        run.id,
        variance_type="missing_in_netsuite",
        variance_amount=Decimal("100.00"),
        stripe_amount=Decimal("100.00"),
        netsuite_amount=None,
        evidence={
            "charge_source_id": f"ch_{uuid.uuid4().hex[:8]}",
            "order_reference": "R123456789",
            "charge_payout_line_id": str(payout_line.id),
        },
    )
    await db.flush()

    out = await plan_run(db, tenant_a.id, run.id)

    prop = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id))
    ).scalar_one()
    assert prop.action == "carry_forward"
    assert out["by_action"]["carry_forward"] == 1


async def test_plan_run_missing_payout_line_falls_back_without_crash(db, tenant_a):
    """A charge_payout_line_id that doesn't resolve to a real payout_line
    (malformed UUID string, or a UUID for a row that doesn't exist) must not
    crash plan_run — fee_amount/days_since_payout both stay None and the
    result falls back to plan_result's un-enriched behavior."""
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(
        db,
        tenant_a.id,
        run.id,
        variance_type="amount_mismatch",
        variance_amount=Decimal("500.00"),  # above materiality, no fee evidence available
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("500.00"),
        evidence={
            "charge_source_id": f"ch_{uuid.uuid4().hex[:8]}",
            "charge_payout_line_id": "not-a-uuid",
        },
    )
    await _result(
        db,
        tenant_a.id,
        run.id,
        variance_type="amount_mismatch",
        variance_amount=Decimal("500.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("500.00"),
        evidence={
            "charge_source_id": f"ch_{uuid.uuid4().hex[:8]}",
            "charge_payout_line_id": str(uuid.uuid4()),  # well-formed but nonexistent
        },
    )
    await db.flush()

    out = await plan_run(db, tenant_a.id, run.id)

    props = (
        (await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)))
        .scalars()
        .all()
    )
    assert len(props) == 2
    assert all(p.action == "needs_human" for p in props)
    assert out["by_action"]["needs_human"] == 2
