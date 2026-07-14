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
    create_test_payout,
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


async def test_plan_run_replans_approved_recency_hold_after_lag_window(db, tenant_a):
    """T2 gate finding, narrowed by Option A (recency holds): an APPROVED
    rule-7 recency-hold carry_forward (root_cause='missing_in_netsuite') must
    not permanently suppress its charge from all future planning via the
    cross-run decided_charge_ids guard. A recency hold is an acknowledged
    're-check next run' snooze, not a commitment toward NetSuite — the guard
    exists to prevent double-POSTING, and carry_forward never posts. A
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

    # Fix A: the run-1 approved recency hold is now superseded — exactly one
    # live recency-hold thread for this charge (the fresh run-2 proposal).
    await db.refresh(prop1)
    assert prop1.status == "superseded"
    assert out["recency_holds_superseded_count"] == 1


async def test_plan_run_supersedes_cross_run_proposed_recency_hold(db, tenant_a):
    """Fix A, narrowed by Option A: a rule-7 recency hold is a per-run,
    re-evaluable acknowledgment. A prior-run recency hold that was never
    approved (still 'proposed') must also be superseded when a later run
    re-plans the same charge — the supersede is keyed on
    action=carry_forward + root_cause IN RECENCY_HOLD_ROOT_CAUSES + charge,
    not on the prior proposal's decision status."""
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
    assert prop1.status == "proposed"  # never approved

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

    await db.refresh(prop1)
    assert prop1.status == "superseded"
    assert out["recency_holds_superseded_count"] == 1


async def test_plan_run_approved_timing_carry_forward_is_standing_decision(db, tenant_a):
    """Option A (recency holds): a TIMING carry_forward (root_cause='timing',
    rule 9) is an ordinary standing decision, unlike a rule-7 recency hold —
    once approved it DOES feed the cross-run decided_charge_ids guard (like
    any other approved action) and is NEVER system-superseded. Before this
    fix the guard's blanket `action != carry_forward` exclusion covered every
    carry_forward variant, so a timing carry_forward would incorrectly get
    re-planned (and then swept up by the cross-run supersede) exactly like a
    recency hold — this is the RED case that motivated narrowing the
    exemption to root_cause IN RECENCY_HOLD_ROOT_CAUSES."""
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    user, _ = await create_test_user(db, tenant_a)
    charge_source_id = f"ch_{uuid.uuid4().hex[:8]}"

    run1 = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(
        db,
        tenant_a.id,
        run1.id,
        variance_type="timing",
        variance_amount=Decimal("5.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("995.00"),
        evidence={"charge_source_id": charge_source_id, "order_reference": "R1"},
    )
    await db.flush()
    await plan_run(db, tenant_a.id, run1.id)

    prop1 = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run1.id))
    ).scalar_one()
    assert prop1.action == "carry_forward"
    assert prop1.root_cause == "timing"

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

    # Same charge shows the same timing variance again in a later run.
    run2 = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(
        db,
        tenant_a.id,
        run2.id,
        variance_type="timing",
        variance_amount=Decimal("5.00"),
        stripe_amount=Decimal("1000.00"),
        netsuite_amount=Decimal("995.00"),
        evidence={"charge_source_id": charge_source_id, "order_reference": "R1"},
    )
    await db.flush()

    out = await plan_run(db, tenant_a.id, run2.id)

    # Standing decision: the guard suppresses re-planning, exactly like any
    # other approved action — no fresh proposal, no supersede.
    assert out["skipped_guard_count"] == 1
    assert out["planned_count"] == 0
    assert out["recency_holds_superseded_count"] == 0

    await db.refresh(prop1)
    assert prop1.status == "approved"  # never system-superseded


async def test_plan_run_carry_forward_supersede_does_not_touch_other_actions(db, tenant_a):
    """Fix A: the cross-run carry_forward supersede is scoped to
    action='carry_forward' only — an approved book_fee_line for the same
    charge is untouched (and continues to suppress re-planning entirely via
    the pre-existing decided_charge_ids guard, since book_fee_line was never
    exempted from it)."""
    charge_source_id = f"ch_{uuid.uuid4().hex[:8]}"
    run1 = await create_test_recon_run(db, tenant_a.id, status="completed")
    r1 = await _result(
        db,
        tenant_a.id,
        run1.id,
        variance_type="fees",
        evidence={"charge_source_id": charge_source_id, "order_reference": "R1"},
    )
    await db.flush()
    await plan_run(db, tenant_a.id, run1.id)
    prop1 = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.result_id == r1.id))
    ).scalar_one()
    assert prop1.action == "book_fee_line"
    prop1.status = "approved"
    await db.flush()

    run2 = await create_test_recon_run(db, tenant_a.id, status="completed")
    await _result(
        db,
        tenant_a.id,
        run2.id,
        variance_type="fees",
        evidence={"charge_source_id": charge_source_id, "order_reference": "R1"},
    )
    await db.flush()

    out = await plan_run(db, tenant_a.id, run2.id)

    await db.refresh(prop1)
    assert prop1.status == "approved"  # untouched — guarded from re-planning entirely
    assert out["skipped_guard_count"] == 1
    assert out["planned_count"] == 0
    assert out["recency_holds_superseded_count"] == 0


async def test_plan_run_carry_forward_supersede_skips_closed_run(db, tenant_a):
    """Gate r3 Fix 1: the cross-run carry_forward supersede must not touch a
    proposal whose owning run is closed/locked — closed-period
    acknowledgments are immutable audit history. The new run-2 proposal still
    supersedes run-1's *logically* (it becomes the only live row going
    forward); run-1's approved carry_forward row itself must stay
    'approved'."""
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
    prop1.status = "approved"
    run1.status = "closed"
    await db.flush()

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

    await db.refresh(prop1)
    assert prop1.status == "approved"  # frozen history — closed run's proposal untouched
    prop2 = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run2.id))
    ).scalar_one()
    assert prop2.action == "create_and_apply_deposit"
    assert out["recency_holds_superseded_count"] == 0


async def test_plan_run_carry_forward_supersede_preserves_human_override(db, tenant_a):
    """Gate r3 Fix 2: never supersede a human override — it is itself the
    human's decision, mirroring the same-run supersede's source != 'human'
    guard (this is the cross-run counterpart of
    test_plan_run_preserves_human_override_after_replan). NOTE the
    interaction: the human's carry_forward proposal survives as 'proposed'
    while run2 still inserts a fresh proposal for its own (new) result row —
    that's two live rows for the charge across runs, which is acceptable and
    inherent here: a human decision outranks the one-live-thread invariant."""
    charge_source_id = f"ch_{uuid.uuid4().hex[:8]}"
    recent_line = await create_test_payout_line(db, tenant_a.id, arrival_date=date.today() - timedelta(days=1))
    run1 = await create_test_recon_run(db, tenant_a.id, status="completed")
    result1 = await _result(
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
    # Mirrors override_resolution_proposal: supersede the planner row, insert
    # a new active source='human' row for the same result.
    prop1.status = "superseded"
    human_prop = ReconResolutionProposal(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        run_id=run1.id,
        result_id=result1.id,
        root_cause=prop1.root_cause,
        action="carry_forward",
        booking_vehicle="none",
        group_key="missing_in_netsuite:carry_forward:none",
        source="human",
        narrative="Overridden by user.",
        proposed_amount=prop1.proposed_amount,
        currency=prop1.currency,
        above_materiality=prop1.above_materiality,
        status="proposed",
        charge_source_id=charge_source_id,
    )
    db.add(human_prop)
    await db.flush()

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

    await db.refresh(human_prop)
    assert human_prop.status == "proposed"  # human decision untouched
    assert human_prop.source == "human"
    prop2 = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run2.id))
    ).scalar_one()
    assert prop2.action == "create_and_apply_deposit"
    assert out["recency_holds_superseded_count"] == 0


async def test_plan_run_carry_forward_supersede_emits_per_proposal_audit(db, tenant_a):
    """Gate r3 Fix 3, narrowed by Option A: each cross-run recency-hold
    supersede emits its own AuditEvent (mirrors the per-line audit pattern in
    group_actions.approve_group_core) — reversing an acknowledged decision
    needs its own audit trail, not just the aggregate
    recency_holds_superseded_count in the summary. The event's
    correlation_id links to this plan's own 'recon.resolution.planned'
    event."""
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
    assert out["recency_holds_superseded_count"] == 1

    plan_event = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "recon.resolution.planned",
                AuditEvent.resource_id == str(run2.id),
            )
        )
    ).scalar_one()

    supersede_events = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "recon.resolution.recency_hold_superseded",
                    AuditEvent.resource_id == str(prop1.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(supersede_events) == 1
    evt = supersede_events[0]
    assert evt.actor_type == "system"
    assert evt.actor_id is None
    assert evt.category == "reconciliation"
    assert evt.resource_type == "recon_resolution_proposal"
    assert evt.correlation_id == plan_event.correlation_id
    assert evt.payload == {"superseding_run_id": str(run2.id), "prior_run_id": str(run1.id)}


async def test_plan_run_supersedes_agent_sourced_recency_hold(db, tenant_a):
    """Final wave Fix 2 (pin-by-design): an agent-authored recency hold
    (source='agent', action='carry_forward', root_cause in
    RECENCY_HOLD_ROOT_CAUSES — as resolution_agent.apply_agent_proposal
    inserts after investigating a needs_human abstention) shares the same
    cross-run snooze lifecycle as a planner-authored one; it is NOT a
    standing decision like a human override (mirrors
    test_plan_run_carry_forward_supersede_preserves_human_override with the
    opposite expectation — only source='human' is exempt from the supersede).
    This test currently PASSES with no code change: the cross-run supersede
    query only excludes source='human', so source='agent' was already
    included. It pins that intended behavior so a future change can't
    silently narrow the exemption to cover agent rows too."""
    charge_source_id = f"ch_{uuid.uuid4().hex[:8]}"
    recent_line = await create_test_payout_line(db, tenant_a.id, arrival_date=date.today() - timedelta(days=1))
    run1 = await create_test_recon_run(db, tenant_a.id, status="completed")
    result1 = await _result(
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
    # Mirrors apply_agent_proposal: supersede the planner row, insert a new
    # active source='agent' row for the same result — the agent investigated
    # a needs_human abstention and judged the charge still in transit.
    prop1.status = "superseded"
    agent_prop = ReconResolutionProposal(
        id=uuid.uuid4(),
        tenant_id=tenant_a.id,
        run_id=run1.id,
        result_id=result1.id,
        root_cause="missing_in_netsuite",
        action="carry_forward",
        booking_vehicle="none",
        group_key="missing_in_netsuite:carry_forward:none",
        source="agent",
        narrative="Agent investigation: payout likely still in transit.",
        proposed_amount=prop1.proposed_amount,
        currency=prop1.currency,
        above_materiality=prop1.above_materiality,
        status="approved",
        charge_source_id=charge_source_id,
    )
    db.add(agent_prop)
    await db.flush()

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

    await db.refresh(agent_prop)
    assert agent_prop.status == "superseded"  # NOT preserved like a human override
    assert out["recency_holds_superseded_count"] == 1

    supersede_events = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "recon.resolution.recency_hold_superseded",
                    AuditEvent.resource_id == str(agent_prop.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(supersede_events) == 1
    assert supersede_events[0].payload == {"superseding_run_id": str(run2.id), "prior_run_id": str(run1.id)}

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


async def test_plan_run_enriches_failed_payout_status_and_emits_needs_human(db, tenant_a):
    """Gate r2 Fix C: plan_run's batched enrichment must select Payout.status
    alongside fee/arrival_date and thread it into plan_result — a recent
    charge tied to a FAILED payout must land on needs_human, not the default
    carry_forward that recency alone would otherwise produce."""
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    payout = await create_test_payout(db, tenant_a.id, status="failed", arrival_date=date.today() - timedelta(days=1))
    payout_line = await create_test_payout_line(db, tenant_a.id, payout=payout)
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
    assert prop.action == "needs_human"
    assert out["by_action"]["needs_human"] == 1


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
