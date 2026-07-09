"""Group approve/reject/override — set-based, audited, materiality-capped."""

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.reconciliation import (
    approve_resolution_group,
    override_resolution_proposal,
    plan_resolutions,
    reject_resolution_group,
)
from app.models.audit import AuditEvent
from app.models.reconciliation import ReconciliationResult, ReconResolutionProposal
from app.schemas.reconciliation import ResolutionGroupApprove, ResolutionProposalOverride
from tests.conftest import (
    create_test_recon_result,
    create_test_recon_run,
    create_test_user,
    enable_feature_flag,
)


async def _seed_fees(db, tenant, above_too=True):
    user, _ = await create_test_user(db, tenant)
    # recon_resolution_ui defaults OFF (DEFAULT_FLAGS) — these mutation
    # endpoints are flag-gated, so tests seeding through them must enable it.
    await enable_feature_flag(db, tenant.id, "recon_resolution_ui")
    run = await create_test_recon_run(db, tenant.id, status="completed")
    # $9 on $1000 = sub-materiality (R2a OR-semantics: not > $50 abs, not > 1%).
    amounts = [(Decimal("9.00"), Decimal("1000"))]
    if above_too:
        amounts.append((Decimal("120.00"), Decimal("10000")))
    for amt, stripe in amounts:
        await create_test_recon_result(
            db,
            tenant.id,
            run.id,
            status="pending",
            bucket="auto_classifications",
            match_type="deterministic",
            variance_type="fees",
            variance_amount=amt,
            stripe_amount=stripe,
            netsuite_amount=stripe - amt,
            evidence={"charge_source_id": f"ch_{amt}", "order_reference": "R1"},
        )
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)
    return user, run


async def _props(db, run_id):
    return (
        (await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run_id)))
        .scalars()
        .all()
    )


async def test_approve_group_skips_above_materiality_by_default(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a)
    out = await approve_resolution_group(
        str(run.id),
        "fees:book_fee_line:deposit",
        ResolutionGroupApprove(notes="month-end"),
        user=user,
        db=db,
    )
    assert out.approved_count == 1  # only the sub-materiality item
    assert out.skipped_count == 1
    props = await _props(db, run.id)
    by_amount = {p.proposed_amount: p.status for p in props}
    assert by_amount[Decimal("9.00")] == "approved"
    assert by_amount[Decimal("120.00")] == "proposed"


async def test_approve_group_includes_ticked_above_materiality(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a)
    above = next(p for p in await _props(db, run.id) if p.above_materiality)
    out = await approve_resolution_group(
        str(run.id),
        "fees:book_fee_line:deposit",
        ResolutionGroupApprove(included_above_materiality_ids=[str(above.id)]),
        user=user,
        db=db,
    )
    assert out.approved_count == 2


async def test_approve_group_flips_result_status_and_audits(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a, above_too=False)
    out = await approve_resolution_group(
        str(run.id),
        "fees:book_fee_line:deposit",
        ResolutionGroupApprove(),
        user=user,
        db=db,
    )
    results = (
        (await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id))).scalars().all()
    )
    assert all(r.status == "approved" and r.approved_by == user.id for r in results)
    per_line = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "recon.resolution.approve",
                    AuditEvent.correlation_id == out.correlation_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(per_line) == 1
    summary = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "recon.resolution.bulk_approve",
                    AuditEvent.correlation_id == out.correlation_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(summary) == 1


async def test_approve_group_result_flip_scoped_to_this_batch_only(db, tenant_a):
    """Result-flip predicate must scope to (correlation_id == this batch, status ==
    approved) — not 'any approved proposal in the tenant'. Regression for the
    >32,767-row asyncpg IN-list fix: the flip now goes via a correlated subquery on
    ReconResolutionProposal.correlation_id instead of a Python id list.

    Simulates a proposal already decided by a DIFFERENT batch: status='approved'
    with its own (different) correlation_id, its result deliberately left un-flipped.
    Because it's already 'approved' (not 'proposed'), this batch's own proposal
    UPDATE can never touch it — so this exercises the result-flip subquery's WHERE
    clause specifically: only the correlation_id predicate stops it being picked up
    (the status predicate alone would NOT exclude it, since it genuinely is
    'approved'). A subquery that dropped the correlation_id filter (scoping to
    'any approved proposal in the tenant' instead of 'this batch') would
    incorrectly flip this result too.
    """
    user, run = await _seed_fees(db, tenant_a, above_too=True)
    above = next(p for p in await _props(db, run.id) if p.above_materiality)
    above.status = "approved"
    above.correlation_id = str(uuid.uuid4())
    await db.flush()

    out = await approve_resolution_group(
        str(run.id),
        "fees:book_fee_line:deposit",
        ResolutionGroupApprove(),  # default: skip above-materiality
        user=user,
        db=db,
    )
    assert out.approved_count == 1  # only the sub-materiality item, this batch

    above_result = (
        await db.execute(select(ReconciliationResult).where(ReconciliationResult.id == above.result_id))
    ).scalar_one()
    assert above_result.status == "pending"  # untouched: not part of THIS batch's correlation_id


async def test_approve_group_skips_proposal_whose_result_is_already_terminal(db, tenant_a):
    """T2 gate finding: a result independently made terminal (e.g. locked via
    the classic per-result approve path) must not have its still-'proposed'
    group proposal flipped to 'approved' — that would audit a per-line success
    for a result the group-approve never actually touched. It must land in
    skipped_count instead, like any other already-decided item."""
    user, run = await _seed_fees(db, tenant_a, above_too=False)
    prop = (await _props(db, run.id))[0]
    result = (
        await db.execute(select(ReconciliationResult).where(ReconciliationResult.id == prop.result_id))
    ).scalar_one()
    result.status = "locked"
    await db.flush()

    out = await approve_resolution_group(
        str(run.id),
        "fees:book_fee_line:deposit",
        ResolutionGroupApprove(),
        user=user,
        db=db,
    )
    assert out.approved_count == 0
    assert out.skipped_count == 1

    await db.refresh(prop)
    assert prop.status == "proposed"

    per_line_audit = (
        (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "recon.resolution.approve",
                    AuditEvent.resource_id == str(prop.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert per_line_audit == []


async def test_approve_group_403_when_resolution_ui_flag_disabled(db, tenant_a):
    """T2 gate finding: the resolution-plan mutation endpoints must require
    recon_resolution_ui (default OFF), not just recon.run permission."""
    user, run = await _seed_fees(db, tenant_a, above_too=False)
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui", enabled=False)

    with pytest.raises(HTTPException) as exc:
        await approve_resolution_group(
            str(run.id),
            "fees:book_fee_line:deposit",
            ResolutionGroupApprove(),
            user=user,
            db=db,
        )
    assert exc.value.status_code == 403


async def test_carry_forward_group_sets_carried_forward_not_approved(db, tenant_a):
    user, _ = await create_test_user(db, tenant_a)
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    r = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="rules",
        match_type="fuzzy",
        variance_type="timing",
        variance_amount=Decimal("0"),
        stripe_amount=Decimal("50"),
        netsuite_amount=Decimal("50"),
        evidence={"charge_source_id": "ch_t", "order_reference": "R2"},
    )
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)
    await approve_resolution_group(
        str(run.id),
        "timing:carry_forward:none",
        ResolutionGroupApprove(),
        user=user,
        db=db,
    )
    await db.refresh(r)
    assert r.status == "carried_forward"


async def test_needs_human_group_not_approvable(db, tenant_a):
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
        variance_type="chargeback",
        variance_amount=Decimal("42"),
        stripe_amount=Decimal("42"),
        netsuite_amount=Decimal("0"),
        evidence={"charge_source_id": "ch_c"},
    )
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)
    with pytest.raises(HTTPException) as exc:
        await approve_resolution_group(
            str(run.id),
            "chargeback:needs_human:none",
            ResolutionGroupApprove(),
            user=user,
            db=db,
        )
    assert exc.value.status_code == 400


async def test_approve_rejected_on_closed_run(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a, above_too=False)
    run.status = "closed"
    await db.flush()
    with pytest.raises(HTTPException) as exc:
        await approve_resolution_group(
            str(run.id),
            "fees:book_fee_line:deposit",
            ResolutionGroupApprove(),
            user=user,
            db=db,
        )
    assert exc.value.status_code == 400


async def test_reject_group_leaves_results_untouched(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a, above_too=False)
    out = await reject_resolution_group(
        str(run.id),
        "fees:book_fee_line:deposit",
        user=user,
        db=db,
    )
    assert out.rejected_count == 1
    result = (await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id))).scalar_one()
    assert result.status == "pending"  # result untouched; proposal history retained


async def test_reject_rejected_on_closed_run(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a, above_too=False)
    run.status = "closed"
    await db.flush()
    prop_before = (await _props(db, run.id))[0]
    with pytest.raises(HTTPException) as exc:
        await reject_resolution_group(
            str(run.id),
            "fees:book_fee_line:deposit",
            user=user,
            db=db,
        )
    assert exc.value.status_code == 400
    assert "closed" in exc.value.detail.lower()
    prop_after = (await _props(db, run.id))[0]
    assert prop_after.status == prop_before.status == "proposed"


async def test_override_rejected_on_closed_run(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a, above_too=False)
    prop = (await _props(db, run.id))[0]
    run.status = "closed"
    await db.flush()
    with pytest.raises(HTTPException) as exc:
        await override_resolution_proposal(
            str(prop.id),
            ResolutionProposalOverride(action="needs_human", notes="not a fee"),
            user=user,
            db=db,
        )
    assert exc.value.status_code == 400
    assert "closed" in exc.value.detail.lower()
    await db.refresh(prop)
    assert prop.status == "proposed"


async def test_override_supersedes_and_creates_new_active(db, tenant_a):
    user, run = await _seed_fees(db, tenant_a, above_too=False)
    prop = (await _props(db, run.id))[0]
    new = await override_resolution_proposal(
        str(prop.id),
        ResolutionProposalOverride(action="needs_human", notes="not a fee"),
        user=user,
        db=db,
    )
    await db.refresh(prop)
    assert prop.status == "superseded"
    assert new.action == "needs_human"
    assert new.source == "human"
    assert new.result_id == str(prop.result_id)
