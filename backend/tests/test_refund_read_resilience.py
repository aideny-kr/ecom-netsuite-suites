"""Slow refund responses can finish without widening retries or losing proof."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from app.services.transaction_ops import netsuite_bulk as bulk
from app.services.transaction_ops import netsuite_reader as native
from app.services.transaction_ops import netsuite_refunds as single
from app.services.transaction_ops.native_read_context import safe_context
from app.services.transaction_ops.read_recovery import read_failure
from tests.test_netsuite_bulk import BulkReader
from tests.test_transaction_ops_netsuite_reader import (
    ACCOUNT,
    CONNECTION,
    REFERENCE,
    TENANT,
)
from tests.test_transaction_ops_netsuite_reader import (
    context as context_fixture,
)


@pytest.fixture
def context(monkeypatch):
    return context_fixture.__wrapped__(monkeypatch)


def target():
    return {
        "provider": "netsuite",
        "scope": {"connection_id": str(CONNECTION), "account_id": ACCOUNT, "subsidiary_id": "1"},
        "lookup": {"complete": True, "count": 1},
        "orders": [
            {
                "record_id": "1",
                "order_reference": REFERENCE,
                "header_complete": True,
                "header": {"id": "1", "currency": {"id": "1"}, "subsidiary": {"id": "1"}},
                "currency_metadata": {"symbol": "USD"},
            }
        ],
    }


async def collect(mode, db):
    if mode == "single":
        return await single.read_netsuite_refunds(db, TENANT, CONNECTION, ACCOUNT, "1", REFERENCE, target())
    result = await bulk.read_refunds(db, TENANT, CONNECTION, ACCOUNT, "1", {REFERENCE: target()})
    return result["refunds"][REFERENCE]


def install_reader(monkeypatch, context, handler):
    db, _ = context
    readers, clients = [], []

    @asynccontextmanager
    async def authenticated(*args, **kwargs):
        kwargs.pop("client", None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            clients.append(client)
            async with native.authenticated_reader(*args, client=client, **kwargs) as reader:
                readers.append(reader)
                yield reader

    monkeypatch.setattr(single, "authenticated_reader", authenticated)
    monkeypatch.setattr(bulk, "authenticated_reader", authenticated)
    return db, readers, clients


@pytest.mark.parametrize("mode", ["single", "bulk"])
async def test_refund_query_can_finish_after_old_idle_deadline_with_identical_money_proof(context, monkeypatch, mode):
    provider, timeouts = BulkReader(), []

    async def handler(request):
        timeout = request.extensions["timeout"]["read"]
        timeouts.append((request.method, timeout))
        if request.method == "POST" and timeout < 35:
            raise httpx.ReadTimeout("private provider body", request=request)
        value = await provider.request(
            request.method,
            request.url.path.removeprefix("/services/rest"),
            params=dict(request.url.params),
            body=json.loads(request.content) if request.content else None,
        )
        return httpx.Response(200, json=value)

    db, readers, clients = install_reader(monkeypatch, context, handler)
    result = await collect(mode, db)
    assert result["complete"] is True and result["amount"] == "100.00"
    assert result["refund_count"] == 1 and result["record_ids"] == ["4"]
    assert all(seconds == (60 if method == "POST" else 25) for method, seconds in timeouts)
    assert readers[0].calls == provider.calls <= (single.MAX_REFUND_CALLS if mode == "single" else bulk.MAX_CALLS)
    assert readers[0].active_calls == 0 and all(client.is_closed for client in clients)


async def test_ordinary_query_keeps_old_deadline_and_timeout_context_contains_no_provider_data():
    def handler(request):
        assert request.extensions["timeout"]["read"] == 25
        raise httpx.ReadTimeout("SECRET provider URL/body/token", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reader = native._Reader(client, "https://example.invalid", "SECRET-TOKEN")
        with pytest.raises(native.NetSuiteEvidenceError, match="^read_timeout$") as failure:
            await reader.request("POST", "/query/v1/suiteql", body={"q": "SELECT SECRET FROM NextTransactionLink l"})
    diagnostic = read_failure(failure.value, {}, stage="netsuite_refunds")
    assert diagnostic["provider_read_context"]["operation"] == "refund_graph"
    assert diagnostic["provider_read_context"]["idle_timeout_seconds"] == 25
    assert "SECRET" not in str(diagnostic) and "example.invalid" not in str(diagnostic)
    assert reader.calls == 1 and reader.active_calls == 0


@pytest.mark.parametrize("value", [True, 0, 26, 61, 120, float("inf"), "60"])
async def test_invalid_query_timeout_fails_before_authorization_or_spend(context, value):
    db, _ = context
    with pytest.raises(native.NetSuiteEvidenceError, match="invalid_read_timeout"):
        async with native.authenticated_reader(db, TENANT, CONNECTION, ACCOUNT, query_read_timeout_seconds=value):
            pytest.fail("invalid timeout admitted")
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("mode,seconds", [("single", 160), ("bulk", 90)])
async def test_existing_collection_deadline_cancels_stalled_query_and_drains_reader(
    context, monkeypatch, mode, seconds
):
    cancelled = []

    async def handler(request):
        assert request.extensions["timeout"]["read"] == 60
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    db, readers, clients = install_reader(monkeypatch, context, handler)
    original_timeout, deadlines = asyncio.timeout, []

    def deadline(value):
        deadlines.append(value)
        return original_timeout(0.02 if value == seconds else value)

    monkeypatch.setattr(asyncio, "timeout", deadline)
    with pytest.raises(TimeoutError if mode == "single" else native.NetSuiteEvidenceError):
        await collect(mode, db)
    assert seconds in deadlines and cancelled == [True]
    assert readers[0].calls == 1 and readers[0].active_calls == 0
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "change",
    [
        {"operation": ["refund_graph"]},
        {"operation": "SECRET"},
        {"elapsed_ms": True},
        {"elapsed_ms": -1},
        {"elapsed_ms": 300001},
        {"idle_timeout_seconds": "60"},
        {"idle_timeout_seconds": 999},
    ],
)
def test_untrusted_timing_context_is_not_exposed(change):
    assert (
        safe_context({"operation": "refund_graph", "elapsed_ms": 25000, "idle_timeout_seconds": 60, **change}) is None
    )


def test_safe_context_discards_unexpected_fields():
    assert safe_context(
        {
            "operation": "refund_requests",
            "elapsed_ms": 25000,
            "idle_timeout_seconds": 60,
            "sql": "SECRET",
            "url": "SECRET",
            "token": "SECRET",
        }
    ) == {
        "operation": "refund_requests",
        "elapsed_ms": 25000,
        "idle_timeout_seconds": 60,
    }


def test_operational_status_exposes_only_validated_provider_timing():
    from app.services.transaction_ops.operational_status import run_snapshot
    from tests.test_transaction_operational_status import NOW, run

    safe = {"operation": "refund_graph", "elapsed_ms": 60001, "idle_timeout_seconds": 60}
    row = run(
        progress_json={
            "last_read_failure": {
                "code": "netsuite_read_timeout",
                "stage": "netsuite_refunds",
                "resolved": False,
                "provider_read_context": {**safe, "sql": "SECRET-SQL", "token": "SECRET-TOKEN"},
            }
        }
    )
    value = run_snapshot(row, NOW)
    assert value["last_read_failure"]["provider_read_context"] == safe
    assert "SECRET" not in str(value)
    row.progress_json["last_read_failure"]["provider_read_context"]["operation"] = ["untrusted"]
    assert "provider_read_context" not in run_snapshot(row, NOW)["last_read_failure"]
