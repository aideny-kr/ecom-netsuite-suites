"""Regression: a posted invoice payment is not a missing customer deposit."""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.canonical import NetsuitePosting
from app.services.ingestion import netsuite_customer_payments as service
from app.services.reconciliation.order_recon_job import OrderReconJob
from app.services.reconciliation.resolution_planner import plan_result
from tests.conftest import create_test_payout, create_test_payout_line
from tests.test_deposit_sync import _seed_netsuite_connection


def application(**changes):
    return {
        "payment_id": "100",
        "payment_type": "CustPymt",
        "tranid": "PYMT100",
        "transaction_date": "2026-09-24",
        "payment_total": "42531",
        "exchange_rate": "1",
        "base_currency": "USD",
        "currency_id": "1",
        "currency": "USD",
        "subsidiary": "1",
        "payment_customer": "50",
        "posting": "T",
        "voided": "F",
        "invoice_id": "200",
        "invoice_currency": "1",
        "invoice_subsidiary": "1",
        "invoice_customer": "50",
        "invoice_posting": "T",
        "invoice_voided": "F",
        "order_id": "300",
        "order_type": "SalesOrd",
        "order_reference": "R123456789",
        "order_currency": "1",
        "order_subsidiary": "1",
        "order_customer": "50",
        "shared_invoice": "F",
        "previousline": "0",
        "nextline": "1",
        "linktype": "Payment",
        "foreignamount": "42531",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"voided": "T"},
        {"posting": "F"},
        {"invoice_currency": "4"},
        {"invoice_subsidiary": "2"},
        {"order_customer": "51"},
        {"shared_invoice": "T"},
        {"linktype": "Other"},
        {"foreignamount": "42532"},
        {"foreignamount": "NaN"},
        {"order_type": "CustCred"},
    ],
)
def test_unsafe_application_cannot_clear_a_charge(changes):
    assert service.allocations([application(**changes)]) == {"100": []}


def test_split_payment_uses_each_order_allocation_without_double_counting():
    first = application(foreignamount="100")
    second = application(invoice_id="201", order_id="301", order_reference="R987654321", foreignamount="200")
    result = service.allocations([first, second])["100"]
    assert [r["amount"] for r in result] == [Decimal("100"), Decimal("200")]
    assert service.allocations([first, first]) == {"100": []}


def test_customer_payment_variance_never_proposes_a_deposit_write():
    result = plan_result(
        match_type="deterministic",
        variance_type="amount_mismatch",
        variance_amount=Decimal("200"),
        stripe_amount=Decimal("300"),
        netsuite_amount=Decimal("100"),
        currency="USD",
        variance_explanation=None,
        evidence={"netsuite_record_type": "customerpayment"},
        already_posted=False,
        materiality_abs=Decimal("50"),
        materiality_pct=Decimal("0.01"),
        currency_basis_verified=True,
    )
    assert result.action == "needs_human"


async def test_repeated_daily_and_custom_runs_keep_payment_matched_and_void_invalidates(
    db, tenant_a, tenant_b, monkeypatch
):
    connection = await _seed_netsuite_connection(db, tenant_a.id)
    remote = AsyncMock(return_value={"columns": [], "rows": [application()], "truncated": False})
    monkeypatch.setattr(service, "execute_suiteql_via_rest", remote)
    args = (db, str(tenant_a.id), connection.id, "test", "6738075", date(2026, 9, 1), date(2026, 9, 30))
    assert await service.sync_payments(*args) == 1
    first = await db.scalar(select(NetsuitePosting).where(NetsuitePosting.tenant_id == tenant_a.id))
    original_id = first.id
    payout = await create_test_payout(db, tenant_a.id, arrival_date=date(2026, 9, 28))
    await create_test_payout_line(
        db,
        tenant_a.id,
        payout=payout,
        amount=Decimal("42531"),
        description="Framework Marketplace Order ID: R123456789",
    )
    job = OrderReconJob(db, str(tenant_a.id))
    for _ in range(2):
        assert await service.sync_payments(*args) == 1
        charges = await job._fetch_charges(date(2026, 9, 1), date(2026, 9, 30))
        deposits = await job._fetch_deposits(date(2026, 9, 1), date(2026, 9, 30), order_references={"R123456789"})
        matches = job.engine.match(charges, deposits)
        assert len(matches) == 1 and matches[0].variance_type is None and matches[0].variance_amount == 0
        assert matches[0].deposit.record_type == "customerpayment"
        assert (
            await db.scalar(select(NetsuitePosting).where(NetsuitePosting.tenant_id == tenant_a.id))
        ).id == original_id
    assert await job._fetch_charges(date(2026, 10, 1), date(2026, 10, 9)) == []
    assert await OrderReconJob(db, str(tenant_b.id))._fetch_deposits(date(2026, 9, 1), date(2026, 9, 30)) == []
    remote.return_value = {"columns": [], "rows": [application(voided="T")], "truncated": False}
    assert await service.sync_payments(*args) == 0
    assert await job._fetch_deposits(date(2026, 9, 1), date(2026, 9, 30)) == []


async def test_reapplication_removes_old_order_and_preserves_transaction_currency(db, tenant_a, monkeypatch):
    connection = await _seed_netsuite_connection(db, tenant_a.id)
    row = application(currency="EUR", base_currency="USD", exchange_rate="1.2")
    remote = AsyncMock(return_value={"rows": [row]})
    monkeypatch.setattr(service, "execute_suiteql_via_rest", remote)
    args = (db, str(tenant_a.id), connection.id, "test", "6738075", date(2026, 9, 1), date(2026, 9, 30))
    await service.sync_payments(*args)
    first = (await OrderReconJob(db, str(tenant_a.id))._fetch_deposits(date(2026, 9, 1), date(2026, 9, 30)))[0]
    assert first.currency == "EUR" and first.amount == Decimal("42531")
    remote.return_value = {"rows": [{**row, "order_id": "301", "order_reference": "R987654321"}]}
    await service.sync_payments(*args)
    current = await OrderReconJob(db, str(tenant_a.id))._fetch_deposits(date(2026, 9, 1), date(2026, 9, 30))
    assert [r.order_reference for r in current] == ["R987654321"]


async def test_partial_provider_result_cannot_invalidate_existing_proof(db, tenant_a, monkeypatch):
    monkeypatch.setattr(service, "execute_suiteql_via_rest", AsyncMock(return_value={"rows": [], "truncated": True}))
    with pytest.raises(ValueError, match="customer_payment_read_limit"):
        await service.sync_payments(
            db, str(tenant_a.id), "connection", "test", "6738075", date(2026, 9, 1), date(2026, 9, 30)
        )
