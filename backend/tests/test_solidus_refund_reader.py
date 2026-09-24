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
    events = [{"id": "41", "payment_number": "PAY123", "amount": "50.25"}]
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
        [{"id": "41", "payment_number": "PAY123", "amount": "49"}],
        [{"id": "41", "payment_number": "PAY123", "amount": "50.25"}] * 2,
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


@pytest.mark.parametrize("change", ["credential", "region", "remote"])
async def test_batch_hit_sees_database_change_outside_its_identity_map(db, admin_user, change):
    from datetime import datetime, timezone

    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.encryption import encrypt_credentials
    from app.models.celigo import CeligoFlowStep
    from app.models.connection import Connection
    from app.services.transaction_ops import source_reader
    from tests.test_transaction_ops_state_db import seed_config

    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    step = await db.scalar(select(CeligoFlowStep).where(CeligoFlowStep.id == config.source_step_id))
    connection = await db.get(Connection, step.celigo_connection_id)
    step.adaptor_type = "RDBMSExport"
    # Fixture-only SQL, consistent with seed_config: no connector lifecycle writes.
    await db.execute(
        text("UPDATE connections SET encrypted_credentials=:secret, metadata_json=CAST(:meta AS jsonb) WHERE id=:id"),
        {"secret": encrypt_credentials({"token": "first"}), "meta": '{"region":"us"}', "id": connection.id},
    )
    await db.flush()
    loaded_step, loaded_conn, _, _ = await source_reader._load_source(db, actor.tenant_id, step.id)
    batch = refund_reader.RefundBatch()
    batch.scope = refund_reader._batch_scope(actor.tenant_id, loaded_step, loaded_conn)
    batch.observed_at = datetime.now(timezone.utc)
    batch.reports = {"R123456789": {"complete": True}}
    old_scope = batch.scope
    # Separate identity map, shared outer test transaction: durable database
    # update is visible without refreshing the original session's objects.
    async with AsyncSession(bind=await db.connection(), join_transaction_mode="create_savepoint") as other:
        if change == "remote":
            await other.execute(
                text("UPDATE celigo_flow_steps SET connection_celigo_id=:remote WHERE id=:id"),
                {"remote": "f" * 24, "id": step.id},
            )
        elif change == "credential":
            await other.execute(
                text("UPDATE connections SET encrypted_credentials=:secret WHERE id=:id"),
                {"secret": encrypt_credentials({"token": "rotated"}), "id": connection.id},
            )
        else:
            await other.execute(
                text("UPDATE connections SET metadata_json=CAST(:meta AS jsonb) WHERE id=:id"),
                {"meta": '{"region":"eu"}', "id": connection.id},
            )
        await other.commit()
    assert refund_reader._batch_scope(actor.tenant_id, loaded_step, loaded_conn) == old_scope
    assert await batch.get(db, actor.tenant_id, step.id, "R123456789", now=batch.observed_at) is None
