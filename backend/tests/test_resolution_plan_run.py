"""plan_run orchestrator: supersede-then-insert, cross-run guard, audit event."""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.reconciliation import ReconResolutionProposal
from app.services.reconciliation.resolution_planner import plan_run
from tests.conftest import create_test_recon_result, create_test_recon_run


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
