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
            result.variance_type in {"manual_adjustment", "amount_mismatch", "fx_rounding"}
            and result.deposit_id is None
        ):
            await seed_linked_evidence(db, run, result)
