"""Explicit linked-record fixtures for resolution accounting verification tests."""

from decimal import Decimal

from tests.conftest import create_test_netsuite_posting, create_test_payout_line


async def seed_linked_evidence(db, run, result, *, fee=None, transaction_currency=None):
    reference = (result.evidence or {}).get("order_reference", "R123456789")
    run.subsidiary_id = "1"
    fee = fee if fee is not None else abs(result.variance_amount)
    line = await create_test_payout_line(
        db,
        result.tenant_id,
        source_id=f"charge-{result.id}",
        amount=result.stripe_amount,
        fee=fee,
        currency=result.currency,
        description=f"Order {reference}",
        subsidiary_id="1",
    )
    line.related_order_id = reference
    posting = await create_test_netsuite_posting(
        db,
        result.tenant_id,
        amount=result.netsuite_amount,
        currency=result.currency,
        transaction_currency=transaction_currency or result.currency,
        foreign_amount=result.netsuite_amount,
        related_payout_id=reference,
        exchange_rate=Decimal("1"),
    )
    posting.subsidiary_id = "1"
    result.deposit_id = posting.id
    result.evidence = {**(result.evidence or {}), "order_reference": reference, "charge_payout_line_id": str(line.id)}
    await db.flush()
    return line, posting


async def seed_run_linked_evidence(db, tenant_id, run_id):
    from sqlalchemy import select

    from app.models.reconciliation import ReconciliationResult, ReconciliationRun

    run = (await db.execute(select(ReconciliationRun).where(ReconciliationRun.id == run_id))).scalar_one()
    rows = (await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run_id))).scalars().all()
    for result in rows:
        if (
            result.variance_type in {"manual_adjustment", "amount_mismatch", "fx_rounding", "fees"}
            and result.deposit_id is None
        ):
            await seed_linked_evidence(db, run, result)


async def seed_washout_evidence(db, run, result):
    from datetime import datetime, timezone

    reference = result.evidence.get("order_reference") or "R123456789"
    charge = await create_test_payout_line(
        db, result.tenant_id, amount=result.stripe_amount, fee=Decimal("0"), description=reference
    )
    refund = await create_test_payout_line(
        db, result.tenant_id, amount=-result.stripe_amount, fee=Decimal("0"), line_type="refund", description=reference
    )
    for line in (charge, refund):
        line.related_order_id = reference
        line.raw_data = {"created": int(datetime(2026, 3, 18, tzinfo=timezone.utc).timestamp())}
    result.netsuite_amount = None
    result.evidence = {**result.evidence, "order_reference": reference, "charge_payout_line_id": str(charge.id)}
    await db.flush()


async def seed_legacy_fee_proposals(db, run_id):
    """Presentation/approval fixtures also cover persisted pre-verifier proposals.

    These are deliberately legacy rows with incomplete canonical evidence. They
    are not an assertion that the current planner may create new such proposals.
    Pipeline tests use seed_linked_evidence instead.
    """
    from sqlalchemy import select

    from app.models.reconciliation import ReconResolutionProposal

    rows = (
        (
            await db.execute(
                select(ReconResolutionProposal).where(
                    ReconResolutionProposal.run_id == run_id,
                    ReconResolutionProposal.root_cause == "fees",
                )
            )
        )
        .scalars()
        .all()
    )
    for proposal in rows:
        proposal.action = "book_fee_line"
        proposal.booking_vehicle = "deposit"
        proposal.group_key = "fees:book_fee_line:deposit"
    await db.flush()
