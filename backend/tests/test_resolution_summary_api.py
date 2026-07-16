"""resolution-summary aggregation + per-group proposal listing."""

from decimal import Decimal

from sqlalchemy import select

from app.api.v1.reconciliation import (
    approve_resolution_group,
    get_resolution_summary,
    list_group_proposals,
    plan_resolutions,
    reject_resolution_group,
)
from app.models.reconciliation import ReconResolutionProposal
from app.schemas.reconciliation import ResolutionGroupApprove
from tests.conftest import (
    create_test_netsuite_posting,
    create_test_recon_result,
    create_test_recon_run,
    create_test_user,
    enable_feature_flag,
)


async def _seed(db, tenant):
    user, _ = await create_test_user(db, tenant)
    await enable_feature_flag(db, tenant.id, "recon_resolution_ui")
    run = await create_test_recon_run(db, tenant.id, status="completed")
    # 2 fee lines (one above materiality), 1 timing, 1 chargeback, 1 clean match.
    # Materiality is R2a OR-semantics: above when > $50 abs OR > 1% of order.
    # $9 on $1000 (0.9%) is sub-materiality; $120 on $10000 is above ($120 > $50).
    for amt, stripe in ((Decimal("9.00"), Decimal("1000")), (Decimal("120.00"), Decimal("10000"))):
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
    await create_test_recon_result(
        db,
        tenant.id,
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
    await create_test_recon_result(
        db,
        tenant.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="chargeback",
        variance_amount=Decimal("42"),
        stripe_amount=Decimal("42"),
        netsuite_amount=Decimal("0"),
        evidence={"charge_source_id": "ch_c", "order_reference": "R3"},
    )
    await create_test_recon_result(
        db,
        tenant.id,
        run.id,
        status="auto_matched",
        bucket="matches",
        match_type="deterministic",
        variance_type=None,
        variance_amount=Decimal("0"),
        stripe_amount=Decimal("10"),
        netsuite_amount=Decimal("10"),
        evidence={"charge_source_id": "ch_m", "order_reference": "R4"},
    )
    # In production, recon_job.py/order_recon_job.py set this from bucket counts
    # once the pipeline finishes. This test seeds results directly (no pipeline
    # run), so it must set the same invariant by hand.
    run.matches_count = 1
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)
    return user, run


async def test_summary_groups_and_rates(db, tenant_a):
    user, run = await _seed(db, tenant_a)
    out = await get_resolution_summary(str(run.id), user=user, db=db)

    assert out.total_results == 5
    assert out.matches_count == 1
    assert out.proposals_count == 4
    # chargeback → needs_human; fees ×2 + timing are "explained"
    assert out.explained_count == 3
    assert out.explained_rate == Decimal("75.0")
    keys = {g.group_key for g in out.groups}
    assert "fees:book_fee_line:deposit" in keys
    assert "timing:carry_forward:none" in keys
    assert "chargeback:needs_human:none" in keys
    fee_group = next(g for g in out.groups if g.root_cause == "fees")
    assert fee_group.count == 2
    assert fee_group.above_materiality_count == 1
    assert fee_group.total_amount == Decimal("129.00")
    assert out.variance_by_root_cause["fees"] == Decimal("129.00")


async def test_guard_skipped_count_excludes_human_rejected_proposals(db, tenant_a):
    """T2 gate finding: guard_skipped_count must count only results with NO
    proposal at all (never planned / guard-skipped), not results whose
    proposal a human rejected — those fall out of the live proposals_count
    but were still planned, so they must not be mislabeled as guard-skipped."""
    user, run = await _seed(db, tenant_a)
    await reject_resolution_group(str(run.id), "fees:book_fee_line:deposit", user=user, db=db)

    out = await get_resolution_summary(str(run.id), user=user, db=db)
    assert out.guard_skipped_count == 0


async def test_above_materiality_count_excludes_decided_proposals(db, tenant_a):
    """T2 gate finding: above_materiality_count must only count still-proposed
    rows. After approving the group WITH the above-materiality id ticked in,
    the fee group's above_materiality_count must drop to 0 — otherwise the
    FE's oneClickCount = proposed_count - above_materiality_count + ticked
    goes negative/zero and wrongly disables approval on the next render."""
    user, run = await _seed(db, tenant_a)
    fee_props = (
        (
            await db.execute(
                select(ReconResolutionProposal).where(
                    ReconResolutionProposal.run_id == run.id,
                    ReconResolutionProposal.root_cause == "fees",
                )
            )
        )
        .scalars()
        .all()
    )
    above = next(p for p in fee_props if p.above_materiality)

    await approve_resolution_group(
        str(run.id),
        "fees:book_fee_line:deposit",
        ResolutionGroupApprove(included_above_materiality_ids=[str(above.id)]),
        user=user,
        db=db,
    )

    out = await get_resolution_summary(str(run.id), user=user, db=db)
    fee_group = next(g for g in out.groups if g.root_cause == "fees")
    assert fee_group.above_materiality_count == 0
    assert fee_group.proposed_count == 0


async def test_group_proposals_listing_paginated(db, tenant_a):
    user, run = await _seed(db, tenant_a)
    page = await list_group_proposals(str(run.id), "fees:book_fee_line:deposit", user=user, db=db, limit=1, offset=0)
    assert len(page) == 1
    assert page[0].action == "book_fee_line"


async def test_group_proposals_listing_includes_identifiers_when_matched(db, tenant_a):
    """A1: a proposal whose result has a linked NetSuite deposit surfaces all
    four identifiers (order ref, Stripe charge id, NetSuite id + record type)."""
    user, _ = await create_test_user(db, tenant_a)
    await enable_feature_flag(db, tenant_a.id, "recon_resolution_ui")
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    posting = await create_test_netsuite_posting(db, tenant_a.id, netsuite_internal_id="98765", record_type="custdep")
    await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="auto_classifications",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("9.00"),
        stripe_amount=Decimal("1000"),
        netsuite_amount=Decimal("991"),
        evidence={"charge_source_id": "ch_matched", "order_reference": "R9"},
        deposit_id=posting.id,
    )
    run.matches_count = 0
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)

    page = await list_group_proposals(str(run.id), "fees:book_fee_line:deposit", user=user, db=db)
    assert len(page) == 1
    item = page[0]
    assert item.order_reference == "R9"
    assert item.stripe_charge_id == "ch_matched"
    assert item.netsuite_internal_id == "98765"
    assert item.netsuite_record_type == "custdep"


async def test_group_proposals_listing_omits_identifiers_when_unmatched(db, tenant_a):
    """A1: an unmatched result (no deposit_id) surfaces order ref + charge id
    but leaves the NetSuite fields None rather than erroring the join."""
    user, run = await _seed(db, tenant_a)
    page = await list_group_proposals(str(run.id), "fees:book_fee_line:deposit", user=user, db=db)
    assert len(page) == 2
    for item in page:
        assert item.order_reference == "R1"
        assert item.stripe_charge_id is not None
        assert item.netsuite_internal_id is None
        assert item.netsuite_record_type is None


async def _seed_multi_currency_fees(db, tenant):
    user, _ = await create_test_user(db, tenant)
    await enable_feature_flag(db, tenant.id, "recon_resolution_ui")
    run = await create_test_recon_run(db, tenant.id, status="completed")
    await create_test_recon_result(
        db,
        tenant.id,
        run.id,
        status="pending",
        bucket="auto_classifications",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("5.00"),
        stripe_amount=Decimal("500"),
        netsuite_amount=Decimal("495"),
        currency="USD",
        evidence={"charge_source_id": "ch_usd", "order_reference": "R1"},
    )
    await create_test_recon_result(
        db,
        tenant.id,
        run.id,
        status="pending",
        bucket="auto_classifications",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("7.00"),
        stripe_amount=Decimal("700"),
        netsuite_amount=Decimal("693"),
        currency="EUR",
        evidence={"charge_source_id": "ch_eur", "order_reference": "R2"},
    )
    await db.flush()
    await plan_resolutions(str(run.id), user=user, db=db)
    return user, run


async def test_summary_splits_groups_by_currency(db, tenant_a):
    """T2 gate finding: a group_key alone (root_cause:action:vehicle) can span
    more than one currency — the group query must never sum proposed_amount
    across currencies under one card, and variance_by_root_cause must split
    by currency once a run has more than one."""
    user, run = await _seed_multi_currency_fees(db, tenant_a)

    out = await get_resolution_summary(str(run.id), user=user, db=db)
    fee_groups = [g for g in out.groups if g.root_cause == "fees"]
    assert len(fee_groups) == 2
    by_currency = {g.currency: g for g in fee_groups}
    assert set(by_currency) == {"USD", "EUR"}
    assert by_currency["USD"].total_amount == Decimal("5.00")
    assert by_currency["EUR"].total_amount == Decimal("7.00")
    assert out.variance_by_root_cause["fees (USD)"] == Decimal("5.00")
    assert out.variance_by_root_cause["fees (EUR)"] == Decimal("7.00")


async def test_approve_group_scoped_to_currency(db, tenant_a):
    """Approving one currency's card must not touch the other currency's
    proposals sharing the same group_key."""
    user, run = await _seed_multi_currency_fees(db, tenant_a)

    out = await approve_resolution_group(
        str(run.id),
        "fees:book_fee_line:deposit",
        ResolutionGroupApprove(currency="USD"),
        user=user,
        db=db,
    )
    assert out.approved_count == 1

    props = (
        (await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.run_id == run.id)))
        .scalars()
        .all()
    )
    by_currency = {p.currency: p.status for p in props}
    assert by_currency["USD"] == "approved"
    assert by_currency["EUR"] == "proposed"


async def test_summary_404_on_foreign_run(db, tenant_a, tenant_b):
    user, _ = await create_test_user(db, tenant_a)
    run_b = await create_test_recon_run(db, tenant_b.id, status="completed")
    await db.flush()
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await get_resolution_summary(str(run_b.id), user=user, db=db)
    assert exc.value.status_code == 404


async def test_summary_includes_running_agent_job(db, tenant_a):
    from datetime import datetime, timezone

    from app.models.job import Job

    user, run = await _seed(db, tenant_a)
    db.add(
        Job(
            tenant_id=tenant_a.id,
            job_type="tasks.recon_resolution_agent",
            status="running",
            started_at=datetime.now(timezone.utc),
            parameters={"run_id": str(run.id)},
            result_summary={"processed": 3, "total": 10},
        )
    )
    await db.flush()

    out = await get_resolution_summary(str(run.id), user=user, db=db)

    assert out.agent_job is not None
    assert out.agent_job.status == "running"
    assert out.agent_job.processed == 3
    assert out.agent_job.total == 10


async def test_summary_agent_job_none_when_no_job(db, tenant_a):
    user, run = await _seed(db, tenant_a)

    out = await get_resolution_summary(str(run.id), user=user, db=db)

    assert out.agent_job is None


async def test_summary_agent_job_lookup_normalizes_run_id(db, tenant_a):
    """Dispatch always enqueues the canonical ``str(run.id)`` (lowercase,
    hyphenated), so the Job lookup must compare against that, not the raw
    path param — otherwise a validly-parsed but non-canonical run_id (e.g.
    upper-cased) never finds its agent Job."""
    from datetime import datetime, timezone

    from app.models.job import Job

    user, run = await _seed(db, tenant_a)
    db.add(
        Job(
            tenant_id=tenant_a.id,
            job_type="tasks.recon_resolution_agent",
            status="running",
            started_at=datetime.now(timezone.utc),
            parameters={"run_id": str(run.id)},
            result_summary={"processed": 3, "total": 10},
        )
    )
    await db.flush()

    out = await get_resolution_summary(str(run.id).upper(), user=user, db=db)

    assert out.agent_job is not None
    assert out.agent_job.status == "running"
