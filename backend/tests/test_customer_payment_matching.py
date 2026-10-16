"""Regression: a posted invoice payment is not a missing customer deposit."""

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.models.canonical import NetsuitePosting
from app.models.pipeline import CursorState
from app.services.ingestion import netsuite_customer_payments as service
from app.services.reconciliation.order_recon_job import OrderReconJob
from app.services.reconciliation.resolution_planner import plan_result
from tests.conftest import create_test_payout, create_test_payout_line, create_test_recon_run
from tests.test_deposit_sync import _patch_netsuite_boundary, _seed_netsuite_connection

REAL_DELETION_READER = service._deleted_ids


@pytest.fixture(autouse=True)
def deletion_feed(monkeypatch):
    read = AsyncMock(return_value=set())
    monkeypatch.setattr(service, "_deleted_ids", read)
    return read


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
    run = await create_test_recon_run(db, tenant_a.id)
    failed = job.engine.match(charges, [])
    await job._store_results(run.id, failed)
    from app.models.reconciliation import ReconciliationResult

    receipt = await db.scalar(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id))
    assert receipt.evidence["payment_application_unverified"] is True
    assert receipt.variance_type != "missing_in_netsuite"


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
    connection = await _seed_netsuite_connection(db, tenant_a.id)
    remote = AsyncMock(return_value={"rows": [application()]})
    monkeypatch.setattr(service, "execute_suiteql_via_rest", remote)
    args = (db, str(tenant_a.id), connection.id, "test", "6738075", date(2026, 9, 1), date(2026, 9, 30))
    await service.sync_payments(*args)
    marker = await db.scalar(select(CursorState).where(CursorState.connection_id == connection.id))
    cutoff = marker.cursor_value
    remote.return_value = {"rows": [], "truncated": True}
    with pytest.raises(ValueError, match="customer_payment_read_limit"):
        await service.sync_payments(*args)
    proof = await db.scalar(select(NetsuitePosting).where(NetsuitePosting.tenant_id == tenant_a.id))
    assert proof.raw_data["application_verified"] is True
    assert marker.cursor_value == cutoff


@pytest.mark.parametrize("deleted", ["100", "200", "300"])
async def test_deletion_of_old_payment_or_owned_document_invalidates_cached_match(
    db, tenant_a, monkeypatch, deletion_feed, deleted
):
    connection = await _seed_netsuite_connection(db, tenant_a.id)
    remote = AsyncMock(return_value={"rows": [application()]})
    monkeypatch.setattr(service, "execute_suiteql_via_rest", remote)
    args = (db, str(tenant_a.id), connection.id, "test", "6738075")
    await service.sync_payments(*args, date(2026, 9, 1), date(2026, 9, 30))
    deletion_feed.return_value = {deleted}
    remote.return_value = {"rows": []}
    await service.sync_payments(*args, date(2026, 10, 9), date(2026, 10, 16))
    assert "p.id IN (100)" in remote.await_args.kwargs["query"]
    job = OrderReconJob(db, str(tenant_a.id))
    assert await job._fetch_deposits(date(2026, 9, 1), date(2026, 9, 30)) == []
    assert "R123456789" in job._observed_customer_payments


async def test_complete_date_population_invalidates_absent_payment(db, tenant_a, monkeypatch):
    connection = await _seed_netsuite_connection(db, tenant_a.id)
    remote = AsyncMock(return_value={"rows": [application()]})
    monkeypatch.setattr(service, "execute_suiteql_via_rest", remote)
    args = (db, str(tenant_a.id), connection.id, "test", "6738075", date(2026, 9, 1), date(2026, 9, 30))
    await service.sync_payments(*args)
    remote.return_value = {"rows": []}
    await service.sync_payments(*args)
    assert await OrderReconJob(db, str(tenant_a.id))._fetch_deposits(date(2026, 9, 1), date(2026, 9, 30)) == []


async def test_overlapping_sync_cannot_read_a_stale_first_observation(db, tenant_a, monkeypatch, deletion_feed):
    connection = await _seed_netsuite_connection(db, tenant_a.id)
    remote = AsyncMock()
    monkeypatch.setattr(service, "execute_suiteql_via_rest", remote)
    engine = getattr(db.bind, "engine", db.bind)
    async with engine.connect() as other:
        await other.execute(select(func.pg_advisory_xact_lock(service.sync_lock_key(tenant_a.id, connection.id))))
        try:
            with pytest.raises(ValueError, match="customer_payment_sync_in_progress"):
                await service.sync_payments(
                    db, str(tenant_a.id), connection.id, "test", "6738075", date(2026, 9, 1), date(2026, 9, 30)
                )
            remote.assert_not_awaited()
            deletion_feed.assert_not_awaited()
            assert connection.label == "Test NetSuite"
        finally:
            await other.rollback()


async def test_retired_connection_proof_cannot_clear_an_order(db, tenant_a, monkeypatch):
    connection = await _seed_netsuite_connection(db, tenant_a.id)
    monkeypatch.setattr(service, "execute_suiteql_via_rest", AsyncMock(return_value={"rows": [application()]}))
    await service.sync_payments(
        db, str(tenant_a.id), connection.id, "test", "6738075", date(2026, 9, 1), date(2026, 9, 30)
    )
    connection.status = "revoked"
    await db.flush()
    assert await OrderReconJob(db, str(tenant_a.id))._fetch_deposits(date(2026, 9, 1), date(2026, 9, 30)) == []


async def test_deletion_feed_rejects_partial_or_malformed_records(monkeypatch):
    remote = AsyncMock(return_value={"rows": [{"id": "100"}], "truncated": True})
    monkeypatch.setattr(service, "execute_suiteql_via_rest", remote)
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="customer_payment_read_limit"):
        await REAL_DELETION_READER("test", "6738075", now, now)
    remote.return_value = {"rows": [{"id": "not-an-id"}]}
    with pytest.raises(ValueError, match="invalid_customer_payment_deletion"):
        await REAL_DELETION_READER("test", "6738075", now, now)


def test_customer_payment_requires_exact_reference_and_charge_currency():
    from app.schemas.order_reconciliation import ChargeRecord, NSPaymentRecord
    from app.services.reconciliation.order_matching_engine import OrderMatchingEngine

    charge = ChargeRecord(
        id="one",
        source_id="charge",
        payout_line_id="line",
        amount=100,
        fee=0,
        net=100,
        currency="USD",
        charge_date=date(2026, 9, 24),
        order_reference="R123456789",
    )
    payment = NSPaymentRecord(
        id="two",
        netsuite_internal_id="100",
        amount=100,
        currency="EUR",
        transaction_date=date(2026, 9, 24),
        record_type="customerpayment",
        order_reference="R123456789",
    )
    assert OrderMatchingEngine().match([charge], [payment])[0].deposit is None
    payment = payment.model_copy(update={"currency": "USD", "order_reference": "R987654321"})
    assert OrderMatchingEngine().match([charge], [payment])[0].deposit is None


async def test_application_failure_does_not_advance_sync_freshness_or_expire_caller(db, tenant_a, monkeypatch):
    from contextlib import ExitStack

    from app.services.ingestion import netsuite_deposit_sync as deposits

    connection = await _seed_netsuite_connection(db, tenant_a.id)
    old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    cursor = CursorState(
        connection_id=connection.id, object_type="netsuite_deposits", cursor_value="2026-09-01", last_synced_at=old
    )
    db.add(cursor)
    await db.flush()
    actual_sync = service.sync_payments
    with ExitStack() as stack:
        for patcher in _patch_netsuite_boundary(connection=connection, suiteql_rows=[], db=db):
            stack.enter_context(patcher)
        stack.enter_context(patch.object(service, "sync_payments", actual_sync))
        monkeypatch.setattr(service, "execute_suiteql_via_rest", AsyncMock(side_effect=ValueError("read_failed")))
        result = await deposits.sync_netsuite_deposits(db, str(tenant_a.id), date(2026, 9, 1), date(2026, 9, 30))
    assert result.errors
    assert connection.label == "Test NetSuite"
    await db.refresh(cursor)
    assert cursor.last_synced_at == old and cursor.cursor_value == "2026-09-01"
