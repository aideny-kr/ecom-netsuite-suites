import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.services.transaction_ops import refund_reader
from app.services.transaction_ops.source_reader import SourceReadError


@pytest.fixture
def context(monkeypatch):
    tenant, identifier = uuid4(), uuid4()
    step = SimpleNamespace(id=identifier, adaptor_type="RDBMSExport", connection_celigo_id="a" * 24)
    connection = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(
        "app.services.transaction_ops.source_reader._load_source",
        AsyncMock(return_value=(step, connection, "credential", "us")),
    )
    return tenant, identifier, step


async def perform(context, row, *, database_type="postgresql"):
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"_id": "a" * 24, "type": "rdbms", "rdbms": {"type": database_type}})
        return httpx.Response(200, json={"data": row if isinstance(row, list) else [row], "stages": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await refund_reader.read_solidus_refunds(
            AsyncMock(), context[0], context[1], "R123456789", client=client
        )
    return result, requests


async def test_reads_completed_refunds_with_a_fixed_select_not_saved_export_logic(context):
    result, requests = await perform(
        context,
        {
            "order_reference": "R123456789",
            "currency": "GBP",
            "refund_count": "2",
            "pending_count": "0",
            "amount": "50.25",
        },
    )
    assert result["complete"] is True and result["amount"] == "50.25"
    assert len(requests) == 2
    query = json.loads(requests[1].content)["rdbms"]["query"]
    assert query.startswith("SELECT ") and "spree_refunds" in query and "R123456789" in query
    assert "transaction_id" in query and "reimbursement_id IS NULL" not in query
    assert "credential" not in json.dumps(result)


@pytest.mark.parametrize("count,amount", [("0", "0.00"), ("1", "12.50")])
async def test_zero_and_partial_refunds_require_complete_evidence(context, count, amount):
    result, _ = await perform(
        context,
        {
            "order_reference": "R123456789",
            "currency": "GBP",
            "refund_count": count,
            "pending_count": "0",
            "amount": amount,
        },
    )
    assert result["complete"] is True and result["amount"] == amount


async def test_unconfirmed_refund_does_not_appear_as_zero(context):
    result, _ = await perform(
        context,
        {"order_reference": "R123456789", "currency": "GBP", "refund_count": "1", "pending_count": "1", "amount": "0"},
    )
    assert result["complete"] is False and result["amount"] is None


@pytest.mark.parametrize(
    "row",
    [
        [],
        [{"order_reference": "R123456789"}, {"order_reference": "R123456789"}],
        {"order_reference": "R000000000", "currency": "GBP", "refund_count": "0", "pending_count": "0", "amount": "0"},
    ],
)
async def test_missing_ambiguous_or_wrong_identity_never_proves_refund_balance(context, row):
    with pytest.raises(SourceReadError):
        await perform(context, row)


async def test_connection_type_must_match_verified_postgresql_source(context):
    with pytest.raises(SourceReadError):
        await perform(context, {}, database_type="mysql")


@pytest.mark.parametrize("shape", ["postgresql", ["postgresql"], 1])
async def test_malformed_database_metadata_fails_with_a_safe_code(context, shape):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"_id": "a" * 24, "type": "rdbms", "rdbms": shape})
        )
    ) as client:
        with pytest.raises(SourceReadError, match="unsupported_refund_source"):
            await refund_reader.read_solidus_refunds(AsyncMock(), context[0], context[1], "R123456789", client=client)


async def test_reference_cannot_inject_sql(context):
    with pytest.raises(SourceReadError):
        await refund_reader.read_solidus_refunds(AsyncMock(), context[0], context[1], "R123456789' OR 1=1")


async def test_complete_refund_events_prove_source_identity_and_payment_number(context):
    events = [{"id": "41", "payment_number": "PAY123", "amount": "50.25", "confirmation": "gateway_transaction"}]
    result, requests = await perform(
        context,
        {
            "order_reference": "R123456789",
            "currency": "GBP",
            "refund_count": "1",
            "pending_count": "0",
            "amount": "50.25",
            "events": events,
        },
    )
    assert result["events_complete"] is True and result["events"] == events
    assert "p.number" in json.loads(requests[1].content)["rdbms"]["query"]


@pytest.mark.parametrize(
    "events",
    [
        [{"id": "41", "payment_number": "PAY123", "amount": "49", "confirmation": "gateway_transaction"}],
        [{"id": "41", "payment_number": "PAY123", "amount": "50.25", "confirmation": "gateway_transaction"}] * 2,
        [{"id": "41", "payment_number": "PAY123", "amount": "50.25", "confirmation": "unknown"}],
        [{"id": "41", "payment_number": "PAY123", "amount": "50.25"}],
        None,
    ],
)
async def test_unproven_event_detail_does_not_invent_refund_identity(context, events):
    result, _ = await perform(
        context,
        {
            "order_reference": "R123456789",
            "currency": "GBP",
            "refund_count": "1",
            "pending_count": "0",
            "amount": "50.25",
            "events": events,
        },
    )
    assert result["complete"] is True and result["amount"] == "50.25"
    assert result["events_complete"] is False and result["events"] == []


def batch_context(context):
    from app.services.transaction_ops import source_reader

    step, connection, _, _ = source_reader._load_source.return_value
    connection.encrypted_credentials = "encrypted-one"
    connection.metadata_json = {"region": "us"}
    return step, connection


def refund_row(ref, **updates):
    return {
        "order_reference": ref,
        "currency": "USD",
        "refund_count": "0",
        "pending_count": "0",
        "amount": "0",
        "events": [],
        **updates,
    }


async def fill_batch(context, rows):
    batch_context(context)
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"_id": "a" * 24, "type": "rdbms", "rdbms": {"type": "postgresql"}})
        return httpx.Response(200, json={"data": rows, "stages": []})

    batch = refund_reader.RefundBatch()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await batch.read(AsyncMock(), context[0], context[1], ["R123456789", "R123456790"], client=client)
    return batch, result, calls


async def test_batch_preserves_pending_and_event_proofs_without_changing_timestamp(context):
    from datetime import timedelta

    batch, first, calls = await fill_batch(
        context,
        [
            refund_row("R123456789"),
            refund_row("R123456790", refund_count="1", pending_count="1"),
        ],
    )
    second = await batch.get(
        AsyncMock(), context[0], context[1], "R123456790", now=batch.observed_at + timedelta(seconds=30)
    )
    assert len(calls) == 2
    assert first["complete"] and first["events_complete"]
    assert second["complete"] is False and second["amount"] is None
    assert second["observed_at"] == first["observed_at"]
    assert await batch.get(AsyncMock(), context[0], context[1], "R123456790", now=batch.observed_at) is None
    query = json.loads(calls[-1].content)["rdbms"]["query"]
    assert "IN ('R123456789','R123456790')" in query and query.endswith("LIMIT 3")


@pytest.mark.parametrize(
    "rows",
    [
        [refund_row("R123456789")],
        [refund_row("R123456789")] * 2,
        [refund_row("R123456789"), refund_row("R999999999")],
        [refund_row("R123456789"), refund_row("R123456790", amount=None)],
    ],
)
async def test_partial_ambiguous_foreign_or_invalid_batch_never_populates_cache(context, rows):
    with pytest.raises(SourceReadError):
        await fill_batch(context, rows)


@pytest.mark.parametrize(
    "change", ["tenant", "step", "credential", "connection", "region", "remote", "expiry", "future", "revoked"]
)
async def test_batch_hit_requires_current_exact_authorized_scope(context, change):
    from datetime import timedelta

    from app.services.transaction_ops import source_reader

    batch, _, _ = await fill_batch(context, [refund_row("R123456789"), refund_row("R123456790")])
    tenant, step_id = context[:2]
    step, connection = batch_context(context)
    now = batch.observed_at
    if change == "tenant":
        tenant = uuid4()
    if change == "step":
        step.id = uuid4()
    if change == "credential":
        connection.encrypted_credentials = "encrypted-two"
    if change == "connection":
        connection.id = uuid4()
    if change == "region":
        connection.metadata_json = {"region": "eu"}
    if change == "remote":
        step.connection_celigo_id = "b" * 24
    if change == "expiry":
        now += timedelta(minutes=5, microseconds=1)
    if change == "future":
        now -= timedelta(seconds=1)
    if change == "revoked":
        source_reader._load_source.side_effect = SourceReadError("source_not_found")
        with pytest.raises(SourceReadError):
            await batch.get(AsyncMock(), tenant, step_id, "R123456790", now=now)
    else:
        assert await batch.get(AsyncMock(), tenant, step_id, "R123456790", now=now) is None


@pytest.mark.parametrize("refs", [[], ["R123456789"] * 2, ["R123456789' OR 1=1"], [f"R{i:09}" for i in range(21)]])
async def test_batch_input_bound_checked_before_any_provider_read(context, refs):
    from app.services.transaction_ops import source_reader

    with pytest.raises(SourceReadError):
        await refund_reader.RefundBatch().read(AsyncMock(), context[0], context[1], refs)
    source_reader._load_source.assert_not_awaited()


# C12 (2026-10-27): the select runs in Solidus's PostgreSQL, so it is executed here against the same
# columns, not inspected. Synthetic rows only.
_SPREE = (
    "CREATE TEMP TABLE spree_orders (id bigint, number text, currency text)",
    "CREATE TEMP TABLE spree_payment_methods (id bigint, type text)",
    "CREATE TEMP TABLE spree_payments (id bigint, order_id bigint, number text, source_type text, "
    "payment_method_id bigint, state text)",
    "CREATE TEMP TABLE spree_refunds (id bigint, payment_id bigint, amount numeric(10,2), transaction_id text)",
    "INSERT INTO spree_orders VALUES (1, 'R123456789', 'EUR')",
    "INSERT INTO spree_payment_methods VALUES (10, 'Spree::PaymentMethod::Check'), (11, 'Spree::PaymentMethod::Stripe')",
)


async def _solidus_refunds(db, payments, refunds):
    from sqlalchemy import text

    for statement in _SPREE:
        await db.execute(text(statement))
    for row in payments:
        await db.execute(
            text("INSERT INTO spree_payments VALUES (:id, 1, :number, :source_type, :method, :state)"), row
        )
    for row in refunds:
        await db.execute(text("INSERT INTO spree_refunds VALUES (:id, :payment, :amount, :txn)"), row)
    rows = (await db.execute(text(refund_reader._refund_query("R123456789")))).mappings().all()
    assert len(rows) == 1
    row = dict(rows[0])
    step = SimpleNamespace(id=uuid4())
    from datetime import datetime, timezone

    return refund_reader._refund_report(
        row, step, SimpleNamespace(id=uuid4()), "R123456789", datetime.now(timezone.utc)
    )


def _payment(identifier, source_type, method, state="completed"):
    return {"id": identifier, "number": f"P{identifier}", "source_type": source_type, "method": method, "state": state}


def _refund_row(identifier, payment, amount, txn=None):
    return {"id": identifier, "payment": payment, "amount": amount, "txn": txn}


async def test_a_refund_on_a_completed_check_payment_is_confirmed_without_a_gateway_transaction(db):
    report = await _solidus_refunds(db, [_payment(100, None, 10)], [_refund_row(500, 100, "181.82")])
    assert (report["complete"], report["amount"], report["unconfirmed_count"]) == (True, "181.82", 0)
    assert report["events_complete"] is True
    assert report["events"] == [
        {"id": "500", "payment_number": "P100", "amount": "181.82", "confirmation": "manual_payment"}
    ]


async def test_a_gateway_refund_without_a_transaction_stays_unconfirmed(db):
    report = await _solidus_refunds(
        db,
        [_payment(100, "StripeGateway::PaymentSource", 11), _payment(101, None, 10)],
        [_refund_row(500, 100, "1101.48"), _refund_row(501, 101, "10.00")],
    )
    assert (report["complete"], report["amount"], report["unconfirmed_count"]) == (False, None, 1)


@pytest.mark.parametrize(
    "payment",
    [
        _payment(100, None, 10, state="invalid"),  # a check payment that never completed
        _payment(100, None, 99),  # its payment method cannot be read: never dropped, never confirmed
        _payment(100, None, 11),  # no source but a gateway method
        _payment(100, "Spree::CreditCard", 10),  # a payment source on a check method
    ],
)
async def test_any_other_refund_without_a_transaction_is_counted_as_unconfirmed(db, payment):
    report = await _solidus_refunds(db, [payment], [_refund_row(500, 100, "50.00")])
    assert (report["refund_count"], report["unconfirmed_count"], report["complete"]) == (1, 1, False)


async def test_gateway_and_manual_refunds_add_up_with_their_confirmation(db):
    report = await _solidus_refunds(
        db,
        [_payment(100, "StripeGateway::PaymentSource", 11), _payment(101, None, 10)],
        [_refund_row(500, 100, "20.00", "re_123"), _refund_row(501, 101, "30.00")],
    )
    assert (report["complete"], report["amount"]) == (True, "50.00")
    assert [event["confirmation"] for event in report["events"]] == ["gateway_transaction", "manual_payment"]
