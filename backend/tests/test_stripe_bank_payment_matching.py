"""Regression: a Stripe bank payment settles its order the same way a card charge does.

Stripe records a bank payment (ACH and similar, ``py_…``) as balance-transaction type
``payment`` and its refund as ``payment_refund``. The payment match read only ``charge``
lines, so none of October 2026's 22 bank payments ($102,549.89) was ever matched, even
though every one has an exact NetSuite record: 20 customer deposits and 2 customer
payments. Each test below covers one site that used to name ``"charge"`` itself.

A bank payment the bank rejected is the exception: Stripe reverses it with a
``payment_failure_refund`` linked only by source (five of them, $235,925.28, in Dec 2025
and Jan 2026). It never settled its order, so it must not match a deposit or count.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.canonical import PayoutLine
from app.models.reconciliation import ReconciliationResult, ReconResolutionProposal
from app.services.ingestion import netsuite_customer_payments as customer_payments
from app.services.reconciliation.order_recon_job import OrderReconJob
from app.services.reconciliation.pipeline import ReconPipeline
from app.services.reconciliation.resolution_agent import _verify_cached_washout, gather_context, validate_output
from app.services.reconciliation.resolution_planner import plan_run
from app.services.reconciliation.resolution_verifier import currency_basis_verified
from tests.conftest import (
    create_test_netsuite_posting,
    create_test_payout,
    create_test_recon_result,
    create_test_recon_run,
)
from tests.resolution_evidence_helpers import seed_linked_evidence
from tests.test_customer_payment_matching import application
from tests.test_deposit_sync import _seed_netsuite_connection

WINDOW = {"date_from": date(2026, 10, 1), "date_to": date(2026, 10, 28)}


_SOURCE_PREFIX = {"charge": "ch", "refund": "re", "payment": "py", "payment_refund": "pyr"}


async def _line(
    db,
    tenant_id,
    *,
    line_type: str,
    ref: str | None,
    amount: Decimal,
    fee: Decimal = Decimal("0"),
    source: str | None = None,
    arrival_date: date = date(2026, 10, 14),
    event_date: date = date(2026, 10, 10),
) -> PayoutLine:
    """A payout line shaped like stripe_sync's: the balance transaction is ``source_id`` and
    the object it moved money for (``txn.source``) is ``related_order_id``."""
    payout = await create_test_payout(db, tenant_id, arrival_date=arrival_date)
    if ref is None:
        description = f"Funds reversed due to payment {source}"
    elif line_type == "payment_refund":
        description = f"REFUND FOR PAYMENT (Framework Marketplace Order ID: {ref}-2WWTCTEV)"
    else:
        description = f"Framework Marketplace Order ID: {ref}-2WWTCTEV"
    line = PayoutLine(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        dedupe_key=f"pl-{uuid.uuid4().hex}",
        source="stripe",
        source_id=f"txn_{uuid.uuid4().hex[:16]}",
        related_order_id=source or f"{_SOURCE_PREFIX[line_type]}_{uuid.uuid4().hex[:16]}",
        payout_id=payout.id,
        line_type=line_type,
        amount=amount,
        fee=fee,
        net=amount - fee,
        currency="USD",
        description=description,
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


async def _failed_bank_payment(db, tenant_id, *, ref: str, amount: Decimal) -> PayoutLine:
    payment = await _line(db, tenant_id, line_type="payment", ref=ref, amount=amount, fee=Decimal("5"))
    await _line(
        db,
        tenant_id,
        line_type="payment_failure_refund",
        ref=None,
        amount=-amount,
        source=payment.related_order_id,
        event_date=date(2026, 10, 15),
    )
    return payment


async def test_a_bank_payment_the_bank_rejected_does_not_settle_its_order(db, tenant_a):
    await _failed_bank_payment(db, tenant_a.id, ref="R273070535", amount=Decimal("77743.98"))
    await create_test_netsuite_posting(
        db,
        tenant_a.id,
        netsuite_internal_id="421002",
        related_payout_id="R273070535",
        transaction_date=date(2026, 10, 10),
        amount=Decimal("77743.98"),
    )

    summary = await OrderReconJob(db, str(tenant_a.id)).run(**WINDOW)

    assert (summary.matched_count, summary.unmatched_count) == (0, 0)
    assert await _results(db, summary.run_id) == []


async def test_a_rejected_bank_payment_is_not_counted_as_the_orders_payment(db, tenant_a):
    """The order's only settled payment is the card charge, so it stays the one payment."""
    charge = await _line(db, tenant_a.id, line_type="charge", ref="R180490343", amount=Decimal("100.00"))
    await _failed_bank_payment(db, tenant_a.id, ref="R180490343", amount=Decimal("1200.00"))
    await _line(
        db,
        tenant_a.id,
        line_type="refund",
        ref="R180490343",
        amount=Decimal("-100.00"),
        event_date=date(2026, 10, 12),
    )
    job = OrderReconJob(db, str(tenant_a.id))

    counts = await job._cross_run_charge_ref_counts({"R180490343"}, WINDOW["date_from"], WINDOW["date_to"], None)
    stepper = await ReconPipeline(db, str(tenant_a.id))._count_charges(WINDOW["date_from"], WINDOW["date_to"], None)
    verified = await _verify_cached_washout(
        db, tenant_a.id, charge, "R180490343", None, {"currency": "USD", "stripe_amount": "100.00"}
    )

    assert counts == {"R180490343": 1}
    assert stepper == 1
    assert verified is True


@pytest.mark.parametrize("rejected", [False, True], ids=["settled", "rejected"])
async def test_fee_line_proof_holds_only_for_a_bank_payment_that_settled(db, tenant_a, rejected):
    """The match can run before Stripe syncs the rejection; the proof is built later and
    must see it."""
    run = await create_test_recon_run(db, tenant_a.id, status="completed")
    result = await create_test_recon_result(
        db,
        tenant_a.id,
        run.id,
        status="pending",
        bucket="needs_review",
        match_type="deterministic",
        variance_type="fees",
        variance_amount=Decimal("5.00"),
        stripe_amount=Decimal("4772.00"),
        netsuite_amount=Decimal("4767.00"),
        evidence={"order_reference": "R101907473"},
    )
    line, _posting = await seed_linked_evidence(db, run, result)
    line.line_type = "payment"
    line.related_order_id = "py_3TestBankPayment"
    if rejected:
        await _line(
            db,
            tenant_a.id,
            line_type="payment_failure_refund",
            ref=None,
            amount=Decimal("-4772.00"),
            source=line.related_order_id,
        )
    await db.flush()

    await plan_run(db, tenant_a.id, run.id)
    proposal = (
        await db.execute(select(ReconResolutionProposal).where(ReconResolutionProposal.result_id == result.id))
    ).scalar_one()
    context = await gather_context(db, tenant_a.id, proposal)
    agent_action = validate_output({"action": "book_fee_line"}, context, (Decimal("50"), Decimal("0.01")))["action"]

    assert context["payout_line"]["settled"] == str(not rejected)
    expected = "needs_human" if rejected else "book_fee_line"
    assert (proposal.action, agent_action) == (expected, expected)


def _fee_context(settled: str | None) -> dict:
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
            "line_type": "payment",
            "settled": settled,
            "amount": "4772.00",
            "fee": "5.00",
            "net": "4767.00",
        },
    }


@pytest.mark.parametrize(("settled", "verified"), [("True", True), ("False", False), (None, False)])
def test_deposit_proof_requires_the_payment_to_have_settled(settled, verified):
    assert currency_basis_verified(_fee_context(settled)) is verified
