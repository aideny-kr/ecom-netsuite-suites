"""Wire-level fan-out, financial parity, spend and cancellation under real readers."""

import asyncio
import copy
import json
import re
from collections import Counter
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.services.transaction_ops import netsuite_bulk as bulk
from app.services.transaction_ops import netsuite_reader as native
from app.services.transaction_ops.call_meter import metered
from tests.test_netsuite_refund_graph import edge
from tests.test_transaction_ops_netsuite_reader import ACCOUNT, CONNECTION, SUBSIDIARY, TENANT, record

REFS = [f"R1000000{i:02}" for i in range(10)]


class Wire:
    def __init__(self, *, block=None, fail=None, status=503):
        self.block, self.fail, self.status = block, fail, status
        self.active = self.peak = 0
        self.seven, self.release = asyncio.Event(), asyncio.Event()
        self.calls = Counter()
        self.edges = []
        for i in range(10):
            for row in [
                edge(str(100 + i), str(200 + i), "SalesOrd", "CashSale"),
                edge(str(200 + i), str(300 + i), "CashSale", "CashRfnd"),
            ]:
                row.update(
                    previouscurrency="4", nextcurrency="4", previoussubsidiary=SUBSIDIARY, nextsubsidiary=SUBSIDIARY
                )
                self.edges.append(row)

    async def __call__(self, request):
        path = request.url.path.removeprefix("/services/rest")
        sql = json.loads(request.content).get("q", "") if request.method == "POST" else ""
        kind = "period" if "FROM accountingperiod" in sql else path
        self.calls[kind] += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == 7:
            self.seven.set()
        try:
            if self.block and self.block in path:
                await self.seven.wait()
                if path != self.fail:
                    await self.release.wait()
            await asyncio.sleep(0)  # Allow independent sends to overlap deterministically.
            if path == self.fail:
                return httpx.Response(self.status, json={})
            if "/salesOrder/" in path:
                identifier = int(path.rsplit("/", 1)[1])
                value = record(id=str(identifier), tranId=REFS[identifier - 100])
            elif "/currency/" in path:
                value = {"id": "4", "symbol": "EUR", "currencyPrecision": 2}
            elif "/cashrefund/" in path:
                identifier = int(path.rsplit("/", 1)[1])
                value = {
                    "id": str(identifier),
                    "currency": {"id": "4"},
                    "subsidiary": {"id": SUBSIDIARY},
                    "total": str(identifier - 299),
                }
            elif kind == "period":
                value = bulk.collection([{"id": "10", "closed": "F", "alllocked": "F"}])
            elif "customrecord_fw_refund_requests" in sql:
                value = bulk.collection([])
            elif "NextTransactionLink" in sql:
                frontier = set(re.search(r"l.previousdoc IN \(([^)]+)\)", sql)[1].split(","))
                value = bulk.collection([r for r in self.edges if {r["previousdoc"], r["nextdoc"]} & frontier])
            else:
                value = bulk.collection(
                    [{"id": str(100 + i), "type": "SalesOrd", "order_reference": ref} for i, ref in enumerate(REFS)]
                )
            return httpx.Response(200, json=value)
        finally:
            self.active -= 1


def normalized(values):
    values = copy.deepcopy(values)
    for value in values.values():
        value.pop("observed_at", None)
        value.pop("api_calls", None)
    return values


async def test_seven_wire_calls_preserve_order_evidence_and_coalesce_metadata():
    serial_wire = Wire()
    async with httpx.AsyncClient(transport=httpx.MockTransport(serial_wire)) as client:
        reader = native._Reader(client, "https://netsuite.test/services/rest", "test", max_api_calls=32)
        serial = await bulk.collect_orders(reader, REFS, "tranid", SUBSIDIARY)
        assert reader.peak_concurrency == 1

    wire = Wire(block="/salesOrder/")
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        reader = native._Reader(
            client, "https://netsuite.test/services/rest", "test", max_api_calls=32, max_concurrent_calls=7
        )
        with metered() as meter:
            task = asyncio.create_task(bulk.collect_orders(reader, REFS, "tranid", SUBSIDIARY))
            await asyncio.wait_for(wire.seven.wait(), 2)
            assert wire.active == reader.active_calls == 7
            assert sum(wire.calls.values()) == 8  # Identity plus exactly seven in flight.
            wire.release.set()
            parallel = await task
        assert reader.peak_concurrency == wire.peak == 7
        assert reader.active_calls == wire.active == 0
        assert reader.calls == meter.calls == sum(wire.calls.values()) == 13
        assert sum(v["api_calls"] for v in parallel.values()) == 12  # Shared identity is outside branches.
        assert wire.calls["period"] == wire.calls["/record/v1/currency/4"] == 1
        assert list(parallel) == REFS
        assert normalized(parallel) == normalized(serial)


@pytest.mark.parametrize("stop", ["cancel", "timeout", "429", "503", "budget"])
async def test_failure_drains_every_branch_before_meter_is_settled(stop):
    wire = Wire(
        block="/salesOrder/" if stop != "budget" else None,
        fail="/record/v1/salesOrder/100" if stop in {"429", "503"} else None,
        status=int(stop) if stop.isdigit() else 503,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        reader = native._Reader(
            client,
            "https://netsuite.test/services/rest",
            "test",
            max_api_calls=5 if stop == "budget" else 32,
            max_concurrent_calls=7,
        )
        with metered() as meter:
            if stop == "timeout":
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(0.05):
                        await bulk.collect_orders(reader, REFS, "tranid", SUBSIDIARY)
            else:
                task = asyncio.create_task(bulk.collect_orders(reader, REFS, "tranid", SUBSIDIARY))
                if stop == "cancel":
                    await asyncio.wait_for(wire.seven.wait(), 2)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    code = "api_call_budget" if stop == "budget" else "upstream_http_" + stop
                    with pytest.raises(native.NetSuiteEvidenceError, match=code):
                        await task
            assert reader.active_calls == wire.active == 0
            count = reader.calls
            await asyncio.sleep(0)
            assert count == reader.calls == meter.calls == sum(wire.calls.values())
            assert count <= reader.max_api_calls
            assert wire.peak <= 7
            if stop == "429":
                assert reader.throttled and count == 8  # Queued calls never reach the wire.
            if stop == "503":
                assert reader.aborted and count == 8
            if stop == "budget":
                assert count == 5


@pytest.mark.parametrize("value", [0, 8, True, "7"])
async def test_invalid_concurrency_is_rejected_before_authorization(value):
    db = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(native.NetSuiteEvidenceError, match="invalid_read_concurrency"):
        async with native.authenticated_reader(db, TENANT, CONNECTION, ACCOUNT, max_concurrent_calls=value):
            pytest.fail("Invalid concurrency reached a reader")
    db.execute.assert_not_awaited()


@pytest.fixture
def auth(monkeypatch):
    connection = SimpleNamespace(
        id=CONNECTION,
        tenant_id=TENANT,
        provider="netsuite",
        status="active",
        encrypted_credentials="sealed",
        metadata_json={"recon_bulk_concurrency": 7},
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=connection))))
    monkeypatch.setattr(native, "decrypt_credentials", Mock(return_value={"account_id": ACCOUNT}))
    monkeypatch.setattr(native, "get_valid_token", AsyncMock(return_value="test"))
    monkeypatch.setattr(native, "set_tenant_context", AsyncMock())
    return db, connection


@pytest.mark.parametrize("failure", [None, "invalid_record", "429"])
async def test_authenticated_bulk_order_and_refund_paths_use_seven_without_shared_database_work(
    auth, monkeypatch, failure
):
    db, connection = auth
    wire = Wire()
    original = native.authenticated_reader
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:

        @asynccontextmanager
        async def authenticated(*args, **kwargs):
            assert kwargs["max_concurrent_calls"] == 7
            async with original(*args, client=client, **kwargs) as reader:
                yield reader

        monkeypatch.setattr(bulk, "authenticated_reader", authenticated)
        orders = await bulk.read_orders(db, TENANT, CONNECTION, ACCOUNT, SUBSIDIARY, REFS, "tranid")
        assert orders["concurrency_peak"] == 7
        assert db.execute.await_count == 1  # Authorization only, before fan-out.
        targets = orders["orders"]
        for target in targets.values():
            assert target["scope"]["connection_id"] == str(CONNECTION)
        if failure:
            wire.fail = "/record/v1/cashrefund/300"
            wire.status = 429 if failure == "429" else 404
        wire.block = "/cashrefund/"
        wire.seven.clear()
        wire.peak = 0
        with metered() as meter:
            task = asyncio.create_task(bulk.read_refunds(db, TENANT, CONNECTION, ACCOUNT, SUBSIDIARY, targets))
            await asyncio.wait_for(wire.seven.wait(), 2)
            wire.release.set()
            refunds = await task
            assert refunds["concurrency_peak"] == 7
            expected = REFS[1:7] if failure == "429" else REFS[1:] if failure else REFS
            assert list(refunds["refunds"]) == expected
            for ref in expected:
                value = refunds["refunds"][ref]
                i = REFS.index(ref)
                assert value["complete"] and value["amount"] == str(i + 1)
                assert value["record_ids"] == [str(300 + i)]
                assert value["api_calls"] == 1
            assert meter.calls == refunds["api_calls"] == (11 if failure == "429" else 14)
        assert wire.active == 0 and wire.peak == 7
        assert db.execute.await_count == 2
        connection.tenant_id = CONNECTION  # Another tenant cannot authorize a new batch.
        before = sum(wire.calls.values())
        with pytest.raises(native.NetSuiteEvidenceError, match="invalid_connection"):
            await bulk.read_orders(db, TENANT, CONNECTION, ACCOUNT, SUBSIDIARY, REFS, "tranid")
        assert sum(wire.calls.values()) == before


@pytest.mark.parametrize(
    "setting,expected",
    [
        (None, 1),
        ({}, 1),
        ({"recon_bulk_concurrency": 7}, 7),
        ({"recon_bulk_concurrency": 3}, 3),
        ({"recon_bulk_concurrency": True}, 1),
        ({"recon_bulk_concurrency": "7"}, 1),
        ({"recon_bulk_concurrency": 8}, 1),
    ],
)
async def test_parallelism_requires_valid_selected_connection_opt_in(auth, setting, expected):
    db, connection = auth
    connection.metadata_json = setting
    async with httpx.AsyncClient() as client:
        async with native.authenticated_reader(
            db, TENANT, CONNECTION, ACCOUNT, client=client, max_concurrent_calls=7
        ) as reader:
            assert reader.max_concurrent_calls == expected
        async with native.authenticated_reader(db, TENANT, CONNECTION, ACCOUNT, client=client) as reader:
            assert reader.max_concurrent_calls == 1  # Non-bulk callers remain serial.


async def test_refund_budget_finishes_same_proofs_as_serial(auth, monkeypatch):
    db, connection = auth
    original = native.authenticated_reader
    results = []
    for concurrency in (1, 7):
        wire = Wire()
        # Three refunds per order require 30 GETs after four shared graph queries.
        for i in range(10):
            for extra in (10, 20):
                row = copy.deepcopy(wire.edges[i * 2 + 1])
                row["nextdoc"] = str(300 + i + extra)
                wire.edges.append(row)
        connection.metadata_json = {"recon_bulk_concurrency": concurrency}
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:

            @asynccontextmanager
            async def authenticated(*args, **kwargs):
                async with original(*args, client=client, **kwargs) as reader:
                    yield reader

            monkeypatch.setattr(bulk, "authenticated_reader", authenticated)
            orders = await bulk.read_orders(db, TENANT, CONNECTION, ACCOUNT, SUBSIDIARY, REFS, "tranid")
            with metered() as meter:
                refunds = await bulk.read_refunds(db, TENANT, CONNECTION, ACCOUNT, SUBSIDIARY, orders["orders"])
            assert refunds["api_calls"] == meter.calls == 32
            assert refunds["concurrency_peak"] == 1
            assert list(refunds["refunds"]) == REFS[:9]
            results.append(normalized(refunds["refunds"]))
    assert results[0] == results[1]
