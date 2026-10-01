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
        if "customrecord_fw_refund_requests" in kwargs["body"]["q"]:
            return {"items": [], "count": 0, "totalResults": 0, "hasMore": False}
        frontier = set(re.search(r"l.previousdoc IN \(([^)]+)\)", kwargs["body"]["q"])[1].split(","))
        rows = [row for row in self.edges if row["previousdoc"] in frontier or row["nextdoc"] in frontier]
        return {"items": deepcopy(rows), "count": len(rows), "totalResults": len(rows), "hasMore": not self.complete}


async def test_deposit_refund_uses_reverse_payment_link_and_only_this_orders_applied_amount():
    reader = Reader()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["amount"] == Decimal("100.00") and result["refund_count"] == 1
    assert result["record_ids"] == ["4"]
    assert reader.calls <= 24
    assert result["dependency_manifest"] == {
        "version": 1,
        "order_id": "1",
        "transaction_ids": ["1", "2", "3", "4", "999"],
        "refund_requests": [],
        "truncated": False,
    }
    assert reader.calls == 5  # Inventory adds no provider requests.


async def test_complete_empty_related_graph_proves_zero():
    reader = Reader()
    reader.edges = []
    assert (await collect_refunds(reader, "1", "1", "1", order_reference="R123456789"))["amount"] == 0


async def test_large_application_inventory_is_bounded_without_changing_refund_amount(monkeypatch):
    # Lower the cap to exercise truncation using the normal proved graph.
    monkeypatch.setattr("app.services.transaction_ops.netsuite_refunds.MAX_DEPENDENCIES", 4)
    reader = Reader()
    reader.edges[0]["previousdoc"] = "1000"
    result = await collect_refunds(reader, "1000", "1", "1", order_reference="R123456789")
    assert result["amount"] == Decimal("100.00")
    assert result["dependency_manifest"]["transaction_ids"] == ["1000", "2", "3", "4"]
    assert result["dependency_manifest"]["truncated"] is True
    assert reader.calls == 5


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
        await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")


async def test_voided_refund_does_not_count_as_returned_funds_in_netsuite():
    reader = Reader()
    reader.edges[-1]["previousvoided"] = "T"
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["amount"] == 0
    assert "4" in result["dependency_manifest"]["transaction_ids"]


async def test_credit_refunds_use_the_same_exact_allocation_and_shared_invoices_are_unknown():
    reader = Reader()
    reader.edges = [
        edge("1", "2", "SalesOrd", "CustInvc"),
        edge("2", "3", "CustInvc", "CustCred"),
        edge("4", "3", "CustRfnd", "CustCred"),
    ]
    assert (await collect_refunds(reader, "1", "1", "1", order_reference="R123456789"))["amount"] == Decimal("100.00")
    reader.edges.append(edge("900", "2", "SalesOrd", "CustInvc"))
    with pytest.raises(ValueError):
        await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")


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
        await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
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


# --- Credit memos created from the order's invoice (2026-10-01) -----------------------
# Framework books a Solidus order adjustment as a credit memo created from the invoice.
# The reader reports those credits with their amounts so the comparison can count them;
# they are never returned money, and a return credit (from a return authorization) is
# not one of them.


def credit_row(identifier, number, total, tax="0", createdfrom="2", **changes):
    row = {
        "id": identifier,
        "createdfrom": createdfrom,
        "tranid": number,
        "type": "CustCred",
        "foreigntotal": "-" + total,
        "taxtotal": "-" + tax if tax != "0" else "0",
        "currency": "1",
        "posting": "T",
        "voided": "F",
    }
    row.update(changes)
    return row


class CreditReader(Reader):
    def __init__(self, credit_rows=()):
        super().__init__()
        self.credit_rows = list(credit_rows)
        self.credit_queries = 0

    async def request(self, method, path, **kwargs):
        if method == "POST" and "t.foreigntotal" in kwargs.get("body", {}).get("q", ""):
            self.calls += 1
            self.credit_queries += 1
            asked = set(re.search(r"t.id IN \(([^)]+)\)", kwargs["body"]["q"])[1].split(","))
            rows = [row for row in self.credit_rows if row["id"] in asked]
            return {"items": deepcopy(rows), "count": len(rows), "totalResults": len(rows), "hasMore": False}
        return await super().request(method, path, **kwargs)


async def test_credit_memo_created_from_the_orders_invoice_is_reported_but_never_counted_as_refund():
    reader = CreditReader([credit_row("3", "CM11788", "4.82")])
    reader.edges = [edge("1", "2", "SalesOrd", "CustInvc"), edge("2", "3", "CustInvc", "CustCred")]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["amount"] == 0 and result["refund_count"] == 0
    assert result["invoice_credits"] == {
        "complete": True,
        "credits": [{"id": "3", "number": "CM11788", "invoice_id": "2", "total": "4.82", "tax": "0"}],
        "total": "4.82",
        "tax": "0",
    }
    assert reader.credit_queries == 1
    assert "3" in result["dependency_manifest"]["transaction_ids"]


async def test_refunded_invoice_credit_is_both_a_refund_and_an_invoice_credit():
    # A post-payment price adjustment: Solidus lowers the order and refunds the
    # difference; NetSuite credits the invoice and refunds the credit memo.
    reader = CreditReader([credit_row("3", "CM1", "100.00", tax="10.00")])
    reader.edges = [
        edge("1", "2", "SalesOrd", "CustInvc"),
        edge("2", "3", "CustInvc", "CustCred"),
        edge("4", "3", "CustRfnd", "CustCred"),
    ]
    reader.record["apply"]["items"] = [{"apply": True, "doc": {"id": "3"}, "line": 1, "amount": "100.00"}]
    reader.record["total"] = "100.00"
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["amount"] == Decimal("100.00")
    assert result["invoice_credits"]["total"] == "100.00" and result["invoice_credits"]["tax"] == "10.00"


async def test_return_credits_and_voided_credits_are_not_invoice_credits():
    reader = CreditReader([credit_row("6", "CM2", "9.00")])
    reader.edges = [
        edge("1", "2", "SalesOrd", "CustInvc"),
        edge("2", "5", "CustInvc", "RtnAuth"),
        edge("5", "6", "RtnAuth", "CustCred"),
        edge("2", "7", "CustInvc", "CustCred"),
    ]
    reader.edges[-1]["nextvoided"] = "T"
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"] == {"complete": True, "credits": [], "total": "0", "tax": "0"}
    assert reader.credit_queries == 0


async def test_no_invoice_credit_means_no_extra_provider_call():
    reader = CreditReader()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"] == {"complete": True, "credits": [], "total": "0", "tax": "0"}
    assert reader.calls == 5 and reader.credit_queries == 0


async def test_credit_on_an_invoice_shared_with_another_order_is_unknown_not_zero():
    reader = CreditReader([credit_row("3", "CM1", "4.82")])
    reader.edges = [
        edge("1", "2", "SalesOrd", "CustInvc"),
        edge("900", "2", "SalesOrd", "CustInvc"),
        edge("2", "3", "CustInvc", "CustCred"),
    ]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["amount"] == 0
    assert result["invoice_credits"] == {"complete": False, "reason": "invoice_credit_ownership_ambiguous"}


@pytest.mark.parametrize(
    "rows",
    [
        [],  # the credit vanished between the graph and the totals read
        [credit_row("3", "CM1", "4.82", foreigntotal="4.82")],  # a credit cannot be positive
        [credit_row("3", "CM1", "4.82", currency="2")],
        [credit_row("3", "CM1", "4.82", voided="T")],
        [credit_row("3", "CM1", "4.82", type="CustInvc")],
        [credit_row("3", "CM1", "4.82", foreigntotal="abc")],
        [credit_row("3", "CM1", "4.82"), credit_row("3", "CM1", "4.82")],  # duplicated row
    ],
)
async def test_an_unproven_credit_read_is_unknown_and_never_breaks_the_refund_proof(rows):
    reader = CreditReader(rows)
    reader.edges = [
        edge("1", "2", "SalesOrd", "CustInvc"),
        edge("2", "3", "CustInvc", "CustCred"),
        edge("4", "3", "CustRfnd", "CustCred"),
    ]
    reader.record["apply"]["items"] = [{"apply": True, "doc": {"id": "3"}, "line": 1, "amount": "150.00"}]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["amount"] == Decimal("150.00")
    assert result["invoice_credits"]["complete"] is False


async def test_a_return_credit_applied_to_the_invoice_is_not_an_invoice_credit():
    # The graph links an invoice to a credit memo both when the credit was created from
    # it and when it was only applied to it. Only "created from" makes it an invoice credit.
    reader = CreditReader([credit_row("6", "CM2", "9.00", createdfrom="5")])
    reader.edges = [
        edge("1", "2", "SalesOrd", "CustInvc"),
        edge("2", "5", "CustInvc", "RtnAuth"),
        edge("5", "6", "RtnAuth", "CustCred"),
        edge("2", "6", "CustInvc", "CustCred"),
    ]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"] == {"complete": True, "credits": [], "total": "0", "tax": "0"}
