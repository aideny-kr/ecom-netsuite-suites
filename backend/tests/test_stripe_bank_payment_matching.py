"""Regression: a Stripe bank payment settles its order the same way a card charge does.

Stripe records a bank payment (ACH and similar, ``py_…``) as balance-transaction type
``payment`` and its refund as ``payment_refund``. The payment match read only ``charge``
lines, so none of October 2026's 22 bank payments ($102,549.89) was ever matched, even
though every one has an exact NetSuite record: 20 customer deposits and 2 customer
payments. Each test below covers one site that used to name ``"charge"`` itself.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.canonical import PayoutLine
from app.models.reconciliation import ReconciliationResult
from app.services.ingestion import netsuite_customer_payments as customer_payments
from app.services.reconciliation.order_recon_job import OrderReconJob
from app.services.reconciliation.pipeline import ReconPipeline
from app.services.reconciliation.resolution_agent import _verify_cached_washout
from app.services.reconciliation.resolution_verifier import currency_basis_verified
from tests.conftest import create_test_netsuite_posting, create_test_payout
from tests.test_customer_payment_matching import application
from tests.test_deposit_sync import _seed_netsuite_connection

WINDOW = {"date_from": date(2026, 10, 1), "date_to": date(2026, 10, 28)}


async def _line(
    db,
    tenant_id,
    *,
    line_type: str,
    ref: str,
    amount: Decimal,
    fee: Decimal = Decimal("0"),
    arrival_date: date = date(2026, 10, 14),
    event_date: date = date(2026, 10, 10),
) -> PayoutLine:
    payout = await create_test_payout(db, tenant_id, arrival_date=arrival_date)
    prefix = "REFUND FOR PAYMENT (" if line_type == "payment_refund" else ""
    line = PayoutLine(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        dedupe_key=f"pl-{uuid.uuid4().hex}",
        source="stripe",
        source_id=f"{'py' if line_type.startswith('payment') else 'ch'}_{uuid.uuid4().hex[:12]}",
        payout_id=payout.id,
        line_type=line_type,
        amount=amount,
        fee=fee,
        net=amount - fee,
        currency="USD",
        description=f"{prefix}Framework Marketplace Order ID: {ref}-2WWTCTEV",
        raw_data={"created": int(datetime.combine(event_date, datetime.min.time(), timezone.utc).timestamp())},
    )
    db.add(line)
    await db.flush()
    return line


async def _results(db, run_id: str) -> list[ReconciliationResult]:
    return list(
        (
            await db.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == uuid.UUID(run_id)))
        ).scalars()
    )


async def test_bank_payment_matches_its_customer_deposit(db, tenant_a):
    await _line(db, tenant_a.id, line_type="payment", ref="R101907473", amount=Decimal("4772.00"), fee=Decimal("5"))
    deposit = await create_test_netsuite_posting(
        db,
        tenant_a.id,
        netsuite_internal_id="421001",
        related_payout_id="R101907473",
        transaction_date=date(2026, 10, 10),
        amount=Decimal("4772.00"),
    )

    summary = await OrderReconJob(db, str(tenant_a.id)).run(**WINDOW)

    assert (summary.matched_count, summary.unmatched_count) == (1, 0)
    [result] = await _results(db, summary.run_id)
    assert result.deposit_id == deposit.id
    assert result.variance_type is None


async def test_bank_payment_matches_a_verified_customer_payment(db, tenant_a, monkeypatch):
    monkeypatch.setattr(customer_payments, "_deleted_ids", AsyncMock(return_value=set()))
    remote = AsyncMock(
        return_value={
            "columns": [],
            "rows": [application(transaction_date="2026-10-10", payment_total="3189", foreignamount="3189")],
            "truncated": False,
        }
    )
    monkeypatch.setattr(customer_payments, "execute_suiteql_via_rest", remote)
    connection = await _seed_netsuite_connection(db, tenant_a.id)
    synced = await customer_payments.sync_payments(
        db, str(tenant_a.id), connection.id, "test", "6738075", date(2026, 10, 1), date(2026, 10, 28)
    )
    assert synced == 1
    await _line(db, tenant_a.id, line_type="payment", ref="R123456789", amount=Decimal("3189.00"), fee=Decimal("5"))

    summary = await OrderReconJob(db, str(tenant_a.id)).run(**WINDOW)

    assert (summary.matched_count, summary.unmatched_count) == (1, 0)
    [result] = await _results(db, summary.run_id)
    assert result.evidence["netsuite_record_type"] == "customerpayment"
    assert result.variance_type is None


async def test_bank_payment_refunded_in_full_is_a_washout(db, tenant_a):
    await _line(db, tenant_a.id, line_type="payment", ref="R540592344", amount=Decimal("100.00"))
    await _line(
        db,
        tenant_a.id,
        line_type="payment_refund",
        ref="R540592344",
        amount=Decimal("-100.00"),
        event_date=date(2026, 10, 12),
    )

    summary = await OrderReconJob(db, str(tenant_a.id)).run(**WINDOW)

    [result] = await _results(db, summary.run_id)
    assert result.evidence["washout"] is True
    assert result.evidence["net_after_refund"] == "0.00"


async def test_an_order_paid_by_card_and_by_bank_counts_both_payments(db, tenant_a):
    """Washout evidence requires exactly one payment on the order; a bank payment counts."""
    await _line(db, tenant_a.id, line_type="charge", ref="R777000111", amount=Decimal("60.00"))
    await _line(db, tenant_a.id, line_type="payment", ref="R777000111", amount=Decimal("40.00"))

    counts = await OrderReconJob(db, str(tenant_a.id))._cross_run_charge_ref_counts(
        {"R777000111"}, WINDOW["date_from"], WINDOW["date_to"], None
    )

    assert counts == {"R777000111": 2}


async def test_progress_stepper_counts_bank_payments(db, tenant_a):
    await _line(db, tenant_a.id, line_type="charge", ref="R777000222", amount=Decimal("60.00"))
    await _line(db, tenant_a.id, line_type="payment", ref="R777000333", amount=Decimal("40.00"))

    assert await ReconPipeline(db, str(tenant_a.id))._count_charges(WINDOW["date_from"], WINDOW["date_to"], None) == 2


async def test_agent_recheck_accepts_a_bank_payment_washout(db, tenant_a):
    payment = await _line(db, tenant_a.id, line_type="payment", ref="R540592355", amount=Decimal("100.00"))
    await _line(
        db,
        tenant_a.id,
        line_type="payment_refund",
        ref="R540592355",
        amount=Decimal("-100.00"),
        event_date=date(2026, 10, 12),
    )

    verified = await _verify_cached_washout(
        db, tenant_a.id, payment, "R540592355", None, {"currency": "USD", "stripe_amount": "100.00"}
    )

    assert verified is True


def _fee_context(line_type: str) -> dict:
    return {
        "currency": "USD",
        "subsidiary_id": "1",
        "stripe_amount": "4772.00",
        "netsuite_amount": "4772.00",
        "evidence": {"order_reference": "R101907473"},
        "matched_posting": {
            "related_payout_id": "R101907473",
            "subsidiary_id": "1",
            "record_type": "custdep",
            "currency": "USD",
            "transaction_currency": "USD",
            "amount": "4772.00",
            "foreign_amount": "4772.00",
        },
        "payout_line": {
            "order_reference": "R101907473",
            "currency": "USD",
            "subsidiary_id": "1",
            "line_type": line_type,
            "amount": "4772.00",
            "fee": "5.00",
            "net": "4767.00",
        },
    }


@pytest.mark.parametrize(("line_type", "verified"), [("charge", True), ("payment", True), ("refund", False)])
def test_deposit_proof_accepts_card_and_bank_payments_only(line_type, verified):
    assert currency_basis_verified(_fee_context(line_type)) is verified
