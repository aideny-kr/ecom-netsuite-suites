import re
from copy import deepcopy
from decimal import Decimal

import pytest

from app.services.transaction_ops.netsuite_refunds import collect_refunds


def edge(previous, following, before_type, after_type):
    return {
        "previousdoc": previous,
        "nextdoc": following,
        "previoustype": before_type,
        "nexttype": after_type,
        "previouscurrency": "1",
        "nextcurrency": "1",
        "previoussubsidiary": "1",
        "nextsubsidiary": "1",
        "previousposting": "T",
        "nextposting": "T",
        "previousvoided": "F",
        "nextvoided": "F",
    }


class Reader:
    def __init__(self):
        self.edges = [
            edge("1", "2", "SalesOrd", "CustDep"),
            edge("2", "3", "CustDep", "DepAppl"),
            edge("4", "3", "CustRfnd", "DepAppl"),
        ]
        self.record = {
            "id": "4",
            "currency": {"id": "1"},
            "subsidiary": {"id": "1"},
            "total": "150.00",
            "apply": {
                "items": [
                    {"apply": True, "doc": {"id": "3"}, "line": 1, "amount": "100.00"},
                    {"apply": True, "doc": {"id": "999"}, "line": 2, "amount": "50.00"},
                ]
            },
        }
        self.calls = 0
        self.complete = True

    async def request(self, method, path, **kwargs):
        self.calls += 1
        if method == "GET":
            return deepcopy(self.record)
        assert path == "/query/v1/suiteql" and kwargs["params"]["limit"] <= 201
        frontier = set(re.search(r"l.previousdoc IN \(([^)]+)\)", kwargs["body"]["q"])[1].split(","))
        rows = [row for row in self.edges if row["previousdoc"] in frontier or row["nextdoc"] in frontier]
        return {"items": deepcopy(rows), "count": len(rows), "totalResults": len(rows), "hasMore": not self.complete}


async def test_deposit_refund_uses_reverse_payment_link_and_only_this_orders_applied_amount():
    reader = Reader()
    result = await collect_refunds(reader, "1", "1", "1")
    assert result["amount"] == Decimal("100.00") and result["refund_count"] == 1
    assert result["record_ids"] == ["4"]
    assert reader.calls <= 24


async def test_complete_empty_related_graph_proves_zero():
    reader = Reader()
    reader.edges = []
    assert (await collect_refunds(reader, "1", "1", "1"))["amount"] == 0


@pytest.mark.parametrize(
    "failure", ["partial_graph", "shared_deposit", "currency", "unposted", "partial_apply", "duplicate_apply"]
)
async def test_incomplete_or_ambiguous_allocations_never_prove_a_refund_amount(failure):
    reader = Reader()
    if failure == "partial_graph":
        reader.complete = False
    elif failure == "shared_deposit":
        reader.edges.append(edge("900", "2", "SalesOrd", "CustDep"))
    elif failure == "currency":
        reader.record["currency"]["id"] = "2"
    elif failure == "unposted":
        reader.edges[-1]["previousposting"] = "F"
    elif failure == "partial_apply":
        reader.record["apply"]["hasMore"] = True
    else:
        reader.record["apply"]["items"].append(deepcopy(reader.record["apply"]["items"][0]))
    with pytest.raises(ValueError):
        await collect_refunds(reader, "1", "1", "1")


async def test_voided_refund_does_not_count_as_returned_funds_in_netsuite():
    reader = Reader()
    reader.edges[-1]["previousvoided"] = "T"
    assert (await collect_refunds(reader, "1", "1", "1"))["amount"] == 0


async def test_credit_refunds_use_the_same_exact_allocation_and_shared_invoices_are_unknown():
    reader = Reader()
    reader.edges = [
        edge("1", "2", "SalesOrd", "CustInvc"),
        edge("2", "3", "CustInvc", "CustCred"),
        edge("4", "3", "CustRfnd", "CustCred"),
    ]
    assert (await collect_refunds(reader, "1", "1", "1"))["amount"] == Decimal("100.00")
    reader.edges.append(edge("900", "2", "SalesOrd", "CustInvc"))
    with pytest.raises(ValueError):
        await collect_refunds(reader, "1", "1", "1")


async def test_many_refunds_stop_within_the_native_read_budget():
    from app.services.transaction_ops.netsuite_refunds import MAX_REFUND_CALLS

    reader = Reader()
    reader.edges += [edge(str(identifier), "3", "CustRfnd", "DepAppl") for identifier in range(10, 45)]
    original = reader.request

    async def request(method, path, **kwargs):
        result = await original(method, path, **kwargs)
        if method == "GET":
            result["id"] = path.rsplit("/", 1)[-1]
        return result

    reader.request = request
    with pytest.raises(ValueError, match="budget"):
        await collect_refunds(reader, "1", "1", "1")
    assert reader.calls <= MAX_REFUND_CALLS


@pytest.mark.parametrize("broken", [None, "connection", "reference", "incomplete", "account"])
async def test_refund_entry_requires_the_exact_proven_target_scope(monkeypatch, broken):
    from contextlib import asynccontextmanager
    from uuid import uuid4

    from app.services.transaction_ops import netsuite_refunds as service

    tenant, connection = uuid4(), uuid4()
    target = {
        "provider": "netsuite",
        "scope": {"account_id": "6738075", "subsidiary_id": "1", "connection_id": str(connection)},
        "lookup": {"complete": True, "count": 1},
        "orders": [
            {
                "record_id": "1",
                "order_reference": "R123456789",
                "header_complete": True,
                "header": {"id": "1", "subsidiary": {"id": "1"}, "currency": {"id": "1"}},
                "currency_metadata": {"symbol": "USD"},
            }
        ],
    }
    native = Reader()

    @asynccontextmanager
    async def authenticated(*args, **kwargs):
        assert args[:4] == (None, tenant, connection, "6738075")
        assert kwargs["max_api_calls"] == service.MAX_REFUND_CALLS
        yield native

    monkeypatch.setattr(service, "authenticated_reader", authenticated)
    if broken == "connection":
        target["scope"]["connection_id"] = str(uuid4())
    if broken == "account":
        target["scope"]["account_id"] = "9999999"
    if broken == "reference":
        target["orders"][0]["order_reference"] = "R999999999"
    if broken == "incomplete":
        target["lookup"]["complete"] = False
    if broken:
        with pytest.raises(ValueError):
            await service.read_netsuite_refunds(None, tenant, connection, "6738075", "1", "R123456789", target)
        assert native.calls == 0
    else:
        result = await service.read_netsuite_refunds(None, tenant, connection, "6738075", "1", "R123456789", target)
        assert result["amount"] == "100.00" and result["complete"] is True
        assert result["currency"] == "USD" and result["order_reference"] == "R123456789"
