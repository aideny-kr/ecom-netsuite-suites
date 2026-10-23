import copy
import re
from unittest.mock import AsyncMock

import pytest

from app.services.transaction_ops import netsuite_bulk as bulk
from app.services.transaction_ops.netsuite_refunds import collect_refunds
from tests.test_netsuite_refund_graph import Reader, edge


class BulkReader(Reader):
    async def request(self, method, path, **kwargs):
        if method == "POST":
            self.calls += 1
            sql = kwargs["body"]["q"]
            if "customrecord_fw_refund_requests" in sql:
                return bulk.collection([])
            frontier = set(re.search(r"l.previousdoc IN \(([^)]+)\)", sql)[1].split(","))
            rows = [r for r in self.edges if {r["previousdoc"], r["nextdoc"]} & frontier]
            return {**bulk.collection(rows), "hasMore": not self.complete}
        return await super().request(method, path, **kwargs)


async def test_bulk_graph_matches_single_verifier_and_amortizes_empty_orders():
    reader = BulkReader()
    orders = {"R123456789": {"record_id": "1"}, "R999999999": {"record_id": "900"}}
    batch = await bulk.RefundGraphBatch.collect(reader, orders)
    expected = await collect_refunds(Reader(), "1", "1", "1", order_reference="R123456789")
    actual = await collect_refunds(
        reader, "1", "1", "1", order_reference="R123456789", request_links_reader=batch.links, graph_reader=batch.graph
    )
    assert actual == expected
    calls = reader.calls
    empty = await collect_refunds(
        reader,
        "900",
        "1",
        "1",
        order_reference="R999999999",
        request_links_reader=batch.links,
        graph_reader=batch.graph,
    )
    assert empty["amount"] == 0 and reader.calls == calls


@pytest.mark.parametrize("failure", ["partial", "shared", "currency", "unposted", "apply"])
async def test_bulk_preserves_existing_refund_failure_gates(failure):
    reader = BulkReader()
    if failure == "partial":
        reader.complete = False
        with pytest.raises(bulk.NetSuiteEvidenceError, match="bulk_page_incomplete"):
            await bulk.RefundGraphBatch.collect(reader, {"R123456789": {"record_id": "1"}})
        return
    if failure == "shared":
        reader.edges.append(edge("900", "2", "SalesOrd", "CustDep"))
    if failure == "currency":
        reader.record["currency"]["id"] = "2"
    if failure == "unposted":
        reader.edges[-1]["previousposting"] = "F"
    if failure == "apply":
        reader.record["apply"]["hasMore"] = True
    batch = await bulk.RefundGraphBatch.collect(reader, {"R123456789": {"record_id": "1"}})
    with pytest.raises(ValueError):
        await collect_refunds(
            reader,
            "1",
            "1",
            "1",
            order_reference="R123456789",
            request_links_reader=batch.links,
            graph_reader=batch.graph,
        )


async def test_unscanned_node_cannot_be_treated_as_empty():
    reader = BulkReader()
    batch = bulk.RefundGraphBatch(reader, [], [], {"1"})
    result = await batch.graph(reader.request, {"2"})
    assert result["count"] == 2 and reader.calls == 1


async def test_reverse_custom_ownership_is_always_requeried():
    from tests.test_netsuite_custom_refunds import CustomReader

    # Build the exact request shape used by the native validator.
    row = CustomReader().requests[0]
    reader = AsyncMock()
    reader.request.return_value = bulk.collection([row, {**row, "id": "999", "order_reference": "R999999999"}])
    batch = bulk.RefundGraphBatch(reader, [row], [], set())
    _, _, recheck = await batch.links(reader.request, "1", "1", "1", "R123456789")
    with pytest.raises(ValueError, match="shared_or_changed"):
        await recheck()
    assert reader.request.await_count == 1


async def test_bulk_identity_keeps_duplicates_and_missing_orders_without_scope_filters():
    reader = AsyncMock()
    reader.calls = 0
    refs = ["R123456789", "R987654321"]
    rows = [{"id": str(i), "type": "SalesOrd", "order_reference": refs[0]} for i in (1, 2, 3)]
    reader.request.return_value = bulk.collection(rows)
    reader.read_matches.side_effect = lambda raw, **kw: copy.deepcopy(raw)
    result = await bulk.collect_orders(reader, refs, "tranid", "1")
    assert result[refs[0]]["hasMore"] is True and result[refs[0]]["totalResults"] == 3
    assert result[refs[1]]["count"] == 0
    sql = reader.request.call_args.kwargs["body"]["q"]
    assert "subsidiary" not in sql and "date" not in sql.lower()
    assert reader.request.await_count == 1


@pytest.mark.parametrize("refs", [[], ["bad"], ["R123456789' OR 1=1"], ["R123456789"] * 2])
async def test_invalid_references_never_reach_provider(refs):
    reader = AsyncMock()
    with pytest.raises(bulk.NetSuiteEvidenceError):
        await bulk.collect_orders(reader, refs, "tranid", "1")
    reader.request.assert_not_awaited()


class BulkDepositReader(BulkReader):
    """BulkReader that also answers the order's deposit status query."""

    credential_fingerprint = "test"
    peak_concurrency = 1
    max_api_calls = 100

    def __init__(self):
        super().__init__()
        self.deposit_queries = []

    async def request(self, method, path, **kwargs):
        query = (kwargs.get("body") or {}).get("q", "")
        if "t.type = 'CustDep'" in query:
            self.calls += 1
            self.deposit_queries.append(query)
            return bulk.collection([{"id": "2", "foreigntotal": "100.00", "status": "C"}])
        return await super().request(method, path, **kwargs)


@pytest.mark.parametrize("status,reads", [("H", 1), ("C", 1), ("B", 0), (None, 0)])
async def test_bulk_reads_deposits_only_for_a_closed_sales_order(monkeypatch, status, reads):
    # A canceled order is settled only when every deposit is fully applied (decided 2026-10-22); the
    # bulk reader asks for deposits exactly where the single reader does: a closed sales order.
    from contextlib import asynccontextmanager
    from uuid import uuid4

    reader, connection = BulkDepositReader(), uuid4()

    @asynccontextmanager
    async def authenticated(*args, **kwargs):
        yield reader

    monkeypatch.setattr(bulk, "authenticated_reader", authenticated)
    header = {"id": "1", "currency": {"id": "1"}, "subsidiary": {"id": "1"}}
    if status:
        header["orderStatus"] = {"id": status}
    target = {
        "provider": "netsuite",
        "scope": {"connection_id": str(connection), "account_id": "6738075", "subsidiary_id": "1"},
        "lookup": {"complete": True, "count": 1},
        "orders": [
            {
                "record_id": "1",
                "order_reference": "R123456789",
                "header_complete": True,
                "header": header,
                "currency_metadata": {"symbol": "USD"},
            }
        ],
    }
    result = await bulk.read_refunds(None, uuid4(), connection, "6738075", "1", {"R123456789": target})
    refund = result["refunds"]["R123456789"]
    assert len(reader.deposit_queries) == reads
    assert (refund["deposits"] == [{"id": "2", "total": "100.00", "status": "C"}]) is bool(reads)
    assert refund["billing_ids"] == []
