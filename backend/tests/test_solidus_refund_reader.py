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
