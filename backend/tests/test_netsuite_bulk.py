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


async def test_bulk_identity_carries_native_tax_onto_the_order_evidence():
    # Class C24: the bulk path reads the same identity-row tax as the single reader, through
    # the shared read_matches, so a tax-free order clears here too.
    import json

    import httpx

    from app.services.transaction_ops.netsuite_reader import _Reader
    from app.services.transaction_ops.order_reconciliation import reconcile_order
    from tests.test_transaction_ops_netsuite_reader import SUBSIDIARY, zero_tax_order

    identity, body, source, config, refunds = zero_tax_order({"native_taxtotal": 0, "native_total": 500})
    queries = []

    def respond(request):
        if "/currency/" in request.url.path:
            return httpx.Response(200, json={"id": "1", "symbol": "USD", "currencyPrecision": 2})
        if request.method == "POST":
            query = json.loads(request.content)["q"]
            if "FROM accountingperiod" in query:
                return httpx.Response(200, json=bulk.collection([{"id": "10", "closed": "F"}]))
            queries.append(query)
            return httpx.Response(200, json=identity)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reader = _Reader(client, "https://6738075.invalid", "token", max_api_calls=bulk.MAX_CALLS)
        result = (await bulk.collect_orders(reader, ["R900000401"], "tranid", SUBSIDIARY))["R900000401"]
    (query,) = queries
    assert "t.taxtotal AS native_taxtotal" in query and "t.foreigntotal AS native_total" in query
    assert result["orders"][0]["native_header"] == {"id": "900401", "taxtotal": "0", "total": "500"}
    result["scope"] = {"account_id": "6738075", "subsidiary_id": SUBSIDIARY}
    balance = reconcile_order(source, result, config, refunds=refunds)
    assert balance["status"] == "matched" and balance["target_tax_basis"] == "native_taxtotal"


@pytest.mark.parametrize("refs", [[], ["bad"], ["R123456789' OR 1=1"], ["R123456789"] * 2])
async def test_invalid_references_never_reach_provider(refs):
    reader = AsyncMock()
    with pytest.raises(bulk.NetSuiteEvidenceError):
        await bulk.collect_orders(reader, refs, "tranid", "1")
    reader.request.assert_not_awaited()


async def test_bulk_reports_the_same_deposits_and_billing_as_the_single_reader():
    # PR #422 review round 1 (F2): deposits come from the shared graph rows, so a closed order costs
    # the batch no extra call and no other order's proof can be starved by it.
    from tests.test_netsuite_refund_graph import DepositReader

    reader = BulkReader()
    reader.edges = DepositReader().edges
    batch = await bulk.RefundGraphBatch.collect(reader, {"R123456789": {"record_id": "1"}})
    calls = reader.calls
    actual = await collect_refunds(
        reader, "1", "1", "1", order_reference="R123456789", request_links_reader=batch.links, graph_reader=batch.graph
    )
    expected = await collect_refunds(DepositReader(), "1", "1", "1", order_reference="R123456789")
    assert actual["deposits"] == expected["deposits"] == [{"id": "2", "total": "100.00", "status": "C"}]
    assert actual["billing_ids"] == expected["billing_ids"] == []
    assert reader.calls - calls == 1  # the refund record only, as without deposits
