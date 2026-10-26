"""A refund credit booked by hand for an order (C09, 2026-10-25), with synthetic ids only.

Finance books a refund the order's return authorization never processed: a credit memo created from
its own customer refund, tagged with the order reference (custbody_fw_order_number) and naming the
return authorization in otherrefnum. Nothing links the credit to the sales order, so the native graph
never reached it and the order read 0.00 refunded. Here: sales order 1001 (customer 77) -> return
authorization 1002 (RA900000001); credit 1003, created from refund 1004, which pays it 200.00.
"""

import re
from contextlib import asynccontextmanager
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.transaction_ops import netsuite_bulk as bulk
from app.services.transaction_ops.netsuite_refunds import MAX_REFUND_CALLS, collect_refunds
from app.services.transaction_ops.order_reconciliation import reconcile_order
from tests.test_netsuite_custom_refunds import CustomReader
from tests.test_netsuite_refund_graph import edge
from tests.test_order_balance_reconciliation import evidence as balance_evidence

REFERENCE = "R900000301"
ORDER, RETURN, CREDIT, REFUND = "1001", "1002", "1003", "1004"


def tagged_row(**changes):
    """One row of the tagged-credit read, shaped like the live SuiteQL row (checked 2026-10-25)."""
    row = {
        "id": CREDIT,
        "type": "CustCred",
        "currency": "1",
        "subsidiary": "1",
        "posting": "T",
        "voided": "F",
        "credit_reference": REFERENCE,
        "credit_customer": "77",
        "order_id": ORDER,
        "order_customer": "77",
        "created_from": REFUND,
        "return_number": "RA900000001",
        "return_id": RETURN,
        "return_customer": "77",
    }
    row.update(changes)
    return row


class TaggedReader(CustomReader):
    def __init__(self, return_status="H"):
        super().__init__()
        self.requests = []
        self.edges = [
            {**edge(ORDER, RETURN, "SalesOrd", "RtnAuth"), "nextstatus": return_status},
            # The only link NetSuite keeps for the credit: the refund that pays it.
            edge(REFUND, CREDIT, "CustRfnd", "CustCred"),
        ]
        self.record = {
            "id": REFUND,
            "currency": {"id": "1"},
            "subsidiary": {"id": "1"},
            "total": "200.00",
            "apply": {"items": [{"apply": True, "doc": {"id": CREDIT}, "line": 0, "amount": "200.00"}]},
        }
        self.tagged = [tagged_row()]
        self.tagged_complete = True
        self.tagged_queries = []

    async def request(self, method, path, **kwargs):
        query = kwargs.get("body", {}).get("q", "")
        if "t.otherrefnum" in query:
            self.calls += 1
            self.tagged_queries.append(query)
            rows = deepcopy(self.tagged)
            return {"items": rows, "count": len(rows), "totalResults": len(rows), "hasMore": not self.tagged_complete}
        return await super().request(method, path, **kwargs)


async def read(reader):
    return await collect_refunds(reader, ORDER, "1", "1", order_reference=REFERENCE)


async def test_a_tagged_credit_from_its_own_refund_counts_through_its_return_authorization():
    reader = TaggedReader()
    result = await read(reader)
    assert result["amount"] == Decimal("200.00")
    assert result["record_ids"] == [REFUND] and result["refund_count"] == 1
    assert result["tagged_credits"] == [CREDIT] and result["tagged_credits_rejected"] == []
    assert result["credit_refunds"] == {CREDIT: "200.00"}
    assert result["dependency_manifest"]["transaction_ids"] == [ORDER, RETURN, CREDIT, REFUND]
    # One read finds the order's tagged credits, for its own customer only.
    (query,) = reader.tagged_queries
    assert f"so.id IN ({ORDER})" in query and f"IN ('{REFERENCE}')" in query and "so.entity=t.entity" in query
    # Requests, tagged credits, two graph depths, the refund record, and the reverse request check
    # that no other order's refund request claims the credit.
    assert reader.calls == 6 <= MAX_REFUND_CALLS


async def test_an_open_return_authorization_keeps_the_refund_out_and_says_why():
    """Review counter-example: an authorization still Pending Refund (F) can pay the customer a second
    time, so the hand-booked refund must not quietly match the order and take it off the review queue."""
    result = await read(TaggedReader(return_status="F"))
    assert result["amount"] == 0 and result["record_ids"] == [] and result["credit_refunds"] == {}
    assert result["tagged_credits"] == []
    assert result["tagged_credits_rejected"] == [
        {"id": CREDIT, "reason": "return_authorization_open", "return_authorization_id": RETURN}
    ]
    # Still an observed participant, so a write tool reading the graph meets the credit and stops.
    assert CREDIT in result["dependency_manifest"]["transaction_ids"]


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"credit_customer": "78"}, "customer_differs"),
        ({"currency": "2"}, "credit_scope_differs"),
        ({"subsidiary": "2"}, "credit_scope_differs"),
        ({"posting": "F"}, "credit_scope_differs"),
        ({"voided": "T"}, "credit_scope_differs"),
        ({"type": "CashSale"}, "credit_scope_differs"),
        ({"return_id": None, "return_customer": None, "return_number": None}, "return_authorization_unnamed"),
        ({"return_customer": "78"}, "return_authorization_customer_differs"),
    ],
)
async def test_a_tagged_credit_that_fails_its_own_checks_is_reported_not_attributed(changes, reason):
    reader = TaggedReader()
    reader.tagged = [tagged_row(**changes)]
    result = await read(reader)
    assert result["amount"] == 0 and result["record_ids"] == [] and result["tagged_credits"] == []
    assert result["tagged_credits_rejected"] == [
        {"id": CREDIT, "reason": reason, "return_authorization_id": changes.get("return_id", RETURN)}
    ]


async def test_a_tagged_credit_naming_two_return_authorizations_is_not_attributed():
    reader = TaggedReader()
    reader.tagged = [tagged_row(), tagged_row(return_id="2002")]
    result = await read(reader)
    assert result["amount"] == 0 and result["tagged_credits"] == []
    assert result["tagged_credits_rejected"] == [
        {"id": CREDIT, "reason": "return_authorization_ambiguous", "return_authorization_id": None}
    ]


async def test_a_return_authorization_outside_the_order_is_refused_and_the_difference_stays():
    reader = TaggedReader()
    reader.tagged = [tagged_row(return_id="2002", return_number="RA900000009")]
    result = await read(reader)
    assert result["amount"] == 0 and result["record_ids"] == [] and result["tagged_credits"] == []
    assert result["tagged_credits_rejected"] == [
        {"id": CREDIT, "reason": "return_authorization_not_in_order", "return_authorization_id": "2002"}
    ]


async def test_a_credit_created_from_a_refund_that_does_not_pay_it_is_not_attributed():
    reader = TaggedReader()
    reader.tagged = [tagged_row(created_from="1005")]
    result = await read(reader)
    assert result["amount"] == 0 and result["tagged_credits"] == []
    assert result["tagged_credits_rejected"] == [
        {"id": CREDIT, "reason": "created_from_unproven", "return_authorization_id": RETURN}
    ]


async def test_a_tagged_credit_created_from_another_orders_invoice_is_ambiguous():
    reader = TaggedReader()
    reader.tagged = [tagged_row(created_from="900")]
    reader.edges.append(edge("900", CREDIT, "CustInvc", "CustCred"))  # NetSuite links a credit to its origin
    with pytest.raises(ValueError, match="refund_allocation_ambiguous"):
        await read(reader)


async def test_a_tagged_credit_its_return_authorization_already_links_counts_once():
    reader = TaggedReader()
    reader.edges.append(edge(RETURN, CREDIT, "RtnAuth", "CustCred"))
    result = await read(reader)
    assert result["amount"] == Decimal("200.00") and result["refund_count"] == 1
    assert result["tagged_credits"] == [] and result["tagged_credits_rejected"] == []


async def test_a_tagged_credit_a_refund_request_already_links_counts_once():
    reader = TaggedReader()
    reader.requests = [
        {
            **CustomReader().requests[0],
            "order_reference": REFERENCE,
            "order_id": ORDER,
            "credit_id": CREDIT,
            "refund_id": REFUND,
            "amount": "200.00",
            "credit_reference": REFERENCE,
        }
    ]
    result = await read(reader)
    assert result["amount"] == Decimal("200.00") and result["refund_count"] == 1
    assert result["tagged_credits"] == [] and result["tagged_credits_rejected"] == []
    assert result["request_links"][0]["stage"] == "refund_verified"


async def test_a_refund_that_also_pays_another_orders_credit_counts_only_this_orders_share():
    reader = TaggedReader()
    reader.record["total"] = "250.00"
    reader.record["apply"]["items"].append({"apply": True, "doc": {"id": "999"}, "line": 1, "amount": "50.00"})
    result = await read(reader)
    assert result["amount"] == Decimal("200.00") and result["credit_refunds"] == {CREDIT: "200.00"}
    assert result["tagged_credits"] == [CREDIT]


async def test_the_graph_reading_the_credit_differently_from_its_tag_fails_the_proof():
    reader = TaggedReader()
    reader.edges[1]["nextposting"] = "F"
    with pytest.raises(ValueError, match="refund_graph_changed"):
        await read(reader)


async def test_a_tagged_credit_another_orders_refund_request_claims_fails_the_proof():
    reader = TaggedReader()
    reader.conflicts = [
        {**CustomReader().requests[0], "order_reference": "R900000999", "order_id": "9001", "credit_id": CREDIT}
    ]
    with pytest.raises(ValueError, match="refund_request_shared_or_changed"):
        await read(reader)


@pytest.mark.parametrize("failure", ["incomplete", "too_many", "other_order", "other_reference", "bad_id"])
async def test_an_unproven_tagged_read_never_proves_a_refund(failure):
    reader = TaggedReader()
    if failure == "incomplete":
        reader.tagged_complete = False
    elif failure == "too_many":
        reader.tagged = [tagged_row(id=str(2000 + i)) for i in range(11)]
    elif failure == "other_order":
        reader.tagged = [tagged_row(order_id="1009")]
    elif failure == "other_reference":
        reader.tagged = [tagged_row(credit_reference="R900000302")]
    else:
        reader.tagged = [tagged_row(id="CM1")]
    with pytest.raises(ValueError, match="tagged"):
        await read(reader)


async def test_a_tagged_order_with_many_refunds_stops_within_the_native_read_budget():
    reader = TaggedReader()
    reader.edges += [edge(str(i), CREDIT, "CustRfnd", "CustCred") for i in range(2000, 2040)]
    original = reader.request

    async def request(method, path, **kwargs):
        result = await original(method, path, **kwargs)
        if method == "GET":
            result["id"] = path.rsplit("/", 1)[-1]
        return result

    reader.request = request
    with pytest.raises(ValueError, match="budget"):
        await read(reader)
    assert reader.calls <= MAX_REFUND_CALLS


# --- reconciliation ----------------------------------------------------------------------------


def reconciled(graph, source_refund):
    """The order's reconciliation from this NetSuite refund read and a completed source refund."""
    source, target, config, refunds = balance_evidence()
    source["orders"][0]["number"] = target["orders"][0]["order_reference"] = REFERENCE
    refunds["source"].update(order_reference=REFERENCE, amount=source_refund)
    refunds["target"] = {**graph, "amount": str(graph["amount"]), "complete": True, "order_reference": REFERENCE}
    refunds["target"]["currency"] = refunds["source"]["currency"]
    return reconcile_order(source, target, config, refunds=refunds)


def paid_twice(return_status):
    """The integration refunded the order through its return authorization (credit 1005, paid 200.00 by
    refund 1006), and finance booked the same refund again by hand (credit 1003, refund 1004)."""
    reader = TaggedReader(return_status=return_status)
    reader.edges += [
        {**edge(RETURN, "1005", "RtnAuth", "CustCred"), "previousstatus": return_status},
        edge("1006", "1005", "CustRfnd", "CustCred"),
    ]
    records = {
        REFUND: reader.record,
        "1006": {
            **deepcopy(reader.record),
            "id": "1006",
            "apply": {"items": [{"apply": True, "doc": {"id": "1005"}, "line": 0, "amount": "200.00"}]},
        },
    }
    original = reader.request

    async def request(method, path, **kwargs):
        if method == "GET":
            reader.record = records[path.rsplit("/", 1)[-1]]
        return await original(method, path, **kwargs)

    reader.request = request
    return reader


async def test_a_refund_held_behind_an_open_return_authorization_never_matches_the_order():
    """Review C09-R1: the source shows no refund and NetSuite paid 200.00 by hand. Behind a closed
    authorization the refund counts and the order differs; behind an open one the read holds it out, and
    0.00 against 0.00 must not match the order and hide the money."""
    closed = reconciled(await read(TaggedReader(return_status="H")), "0.00")
    assert (closed["status"], closed["amounts"]["refunds"]["delta"]) == ("difference", "-200.00")
    held = reconciled(await read(TaggedReader(return_status="F")), "0.00")
    assert held["status"] == "difference"
    assert held["reason"] == "return_authorization_open_after_manual_refund"
    assert held["open_return_authorizations"] == [RETURN]
    # The amounts stay what was proven: the held refund is never counted to make the difference.
    assert held["amounts"]["refunds"] == {"source": "0.00", "target": "0.00", "delta": "0.00"}


async def test_a_second_payout_held_behind_an_open_return_authorization_never_matches_the_order():
    """Review C09-R1: NetSuite paid 400.00 for a 200.00 source refund. Closed, the read counts both and the
    order differs; open, it counts only the integration's refund, and 200.00 against 200.00 must not match."""
    closed = reconciled(await read(paid_twice("H")), "200.00")
    assert (closed["status"], closed["amounts"]["refunds"]["target"]) == ("difference", "400.00")
    graph = await read(paid_twice("F"))
    assert graph["amount"] == Decimal("200.00") and graph["credit_refunds"] == {"1005": "200.00"}
    held = reconciled(graph, "200.00")
    assert (held["status"], held["reason"]) == ("difference", "return_authorization_open_after_manual_refund")
    assert held["open_return_authorizations"] == [RETURN]


# --- bulk path ---------------------------------------------------------------------------------


class BulkTaggedReader(TaggedReader):
    """The batch reads every order's graph in shared queries (limit above a single read's)."""

    async def request(self, method, path, **kwargs):
        sql = kwargs.get("body", {}).get("q", "")
        if method == "POST" and "NextTransactionLink" in sql:
            self.calls += 1
            frontier = set(re.search(r"l.previousdoc IN \(([^)]+)\)", sql)[1].split(","))
            return bulk.collection([r for r in self.edges if {r["previousdoc"], r["nextdoc"]} & frontier])
        return await super().request(method, path, **kwargs)


async def test_bulk_reads_the_same_tagged_credit_as_the_single_reader_without_extra_calls():
    reader = BulkTaggedReader()
    batch = await bulk.RefundGraphBatch.collect(reader, {REFERENCE: {"record_id": ORDER}})
    (query,) = reader.tagged_queries
    assert f"so.id IN ({ORDER})" in query and f"IN ('{REFERENCE}')" in query
    calls = reader.calls
    actual = await collect_refunds(
        reader,
        ORDER,
        "1",
        "1",
        order_reference=REFERENCE,
        request_links_reader=batch.links,
        graph_reader=batch.graph,
        tagged_reader=batch.tagged_credits,
    )
    assert actual == await read(TaggedReader())
    assert actual["tagged_credits"] == [CREDIT] and actual["amount"] == Decimal("200.00")
    assert reader.calls - calls == 2  # the refund record and the reverse request check only
    # Custom attribution makes live reverse checks, so these orders are read one at a time.
    assert batch.parallel_fits([ORDER], None) is False


async def test_two_orders_of_one_customer_each_keep_only_their_own_tagged_credit():
    """The batched read joins every order of the customer, so each credit comes back once per order
    (seen live 2026-10-25 for two orders of one customer). Each order keeps only the credit that
    names it."""
    other, other_reference = {"order": "1101", "return": "1102", "credit": "1103", "refund": "1104"}, "R900000302"
    reader = BulkTaggedReader()
    reader.edges += [
        {**edge(other["order"], other["return"], "SalesOrd", "RtnAuth"), "nextstatus": "H"},
        edge(other["refund"], other["credit"], "CustRfnd", "CustCred"),
    ]
    sibling = tagged_row(
        id=other["credit"],
        order_id=other["order"],
        credit_reference=other_reference,
        created_from=other["refund"],
        return_number="RA900000002",
        return_id=other["return"],
    )
    reader.tagged = [
        tagged_row(),
        sibling,
        tagged_row(order_id=other["order"]),
        {**sibling, "order_id": ORDER},
    ]
    records = {
        REFUND: reader.record,
        other["refund"]: {
            **deepcopy(reader.record),
            "id": other["refund"],
            "total": "75.00",
            "apply": {"items": [{"apply": True, "doc": {"id": other["credit"]}, "line": 0, "amount": "75.00"}]},
        },
    }
    original = reader.request

    async def request(method, path, **kwargs):
        if method == "GET":
            reader.record = records[path.rsplit("/", 1)[-1]]
        return await original(method, path, **kwargs)

    reader.request = request
    batch = await bulk.RefundGraphBatch.collect(
        reader, {REFERENCE: {"record_id": ORDER}, other_reference: {"record_id": other["order"]}}
    )
    results = {}
    for order, reference in ((ORDER, REFERENCE), (other["order"], other_reference)):
        results[reference] = await collect_refunds(
            reader,
            order,
            "1",
            "1",
            order_reference=reference,
            request_links_reader=batch.links,
            graph_reader=batch.graph,
            tagged_reader=batch.tagged_credits,
        )
    assert (results[REFERENCE]["tagged_credits"], results[REFERENCE]["amount"]) == ([CREDIT], Decimal("200.00"))
    assert (results[other_reference]["tagged_credits"], results[other_reference]["amount"]) == (
        [other["credit"]],
        Decimal("75.00"),
    )
    assert results[REFERENCE]["tagged_credits_rejected"] == results[other_reference]["tagged_credits_rejected"] == []


async def test_bulk_refund_read_gives_each_order_its_batched_tagged_credits(monkeypatch):
    reader = BulkTaggedReader()
    reader.max_api_calls, reader.credential_fingerprint, reader.peak_concurrency = 99, "fingerprint", 1
    budgets = []

    @asynccontextmanager
    async def authenticated(*args, **kwargs):
        budgets.append(kwargs["max_api_calls"])
        yield reader

    monkeypatch.setattr(bulk, "authenticated_reader", authenticated)
    connection = uuid4()
    target = {
        "provider": "netsuite",
        "scope": {"account_id": "6738075", "subsidiary_id": "1", "connection_id": str(connection)},
        "lookup": {"complete": True, "count": 1},
        "orders": [
            {
                "record_id": ORDER,
                "order_reference": REFERENCE,
                "header_complete": True,
                "header": {"id": ORDER, "subsidiary": {"id": "1"}, "currency": {"id": "1"}},
                "currency_metadata": {"symbol": "USD"},
            }
        ],
    }
    result = await bulk.read_refunds(None, uuid4(), connection, "6738075", "1", {REFERENCE: target})
    value = result["refunds"][REFERENCE]
    assert value["amount"] == "200.00" and value["tagged_credits"] == [CREDIT]
    # Requests, tagged credits and two graph depths for the batch; the order then reads only its
    # refund record and the reverse request check. The tagged read comes out of the same ceiling.
    assert result["api_calls"] == 6
    assert budgets == [bulk.MAX_CALLS]


# --- write tools sharing the refund graph ------------------------------------------------------


@pytest.mark.parametrize("status", ["H", "F"])
async def test_credit_creation_meets_the_tagged_credit_through_the_refund_graph(monkeypatch, status):
    """The credit's memo does not name the order, so only the refund graph finds it. Refunded behind a
    closed authorization it is part of the order's posted balance; behind an open one it is not this
    order's refund, and credit creation stops rather than book a second credit beside it."""
    from tests import test_credit_creation as cc_tests

    graph = await read(TaggedReader(return_status=status))
    ids = graph["dependency_manifest"]["transaction_ids"]
    credit = cc_tests._credit(CREDIT, 200, applied_to=REFUND, memo="refund booked by hand")
    reader, _ = cc_tests._netsuite(credits=[credit], graph_credits=[i for i in ids if i == CREDIT])
    db = cc_tests._patch_reads(monkeypatch, reader, graph_ids=ids, credit_refunds=graph["credit_refunds"])
    if status == "H":
        found, _ = await cc_tests.cc.gather(db, "tenant", cc_tests.CASE, ["1471"])
        assert [document["id"] for document, _ in found["credits"]] == [CREDIT]
    else:
        with pytest.raises(cc_tests.cc.RefusalError) as exc:
            await cc_tests.cc.gather(db, "tenant", cc_tests.CASE, ["1471"])
        assert exc.value.code == "existing_credit_not_applied_to_invoice"


async def test_credit_line_reallocation_leaves_an_order_with_a_tagged_credit_to_a_person(monkeypatch):
    """Recorded as intended (C09): the reallocation's evidence lists only request-linked credits, so an
    order whose refund graph now also reaches a hand-booked credit differs from it and is refused."""
    from app.services.transaction_ops import credit_line_reallocation as clr
    from app.services.transaction_ops import netsuite_refunds

    invoice, native_credit = "1005", "1007"
    graph_reader = TaggedReader()
    graph_reader.edges += [
        edge(ORDER, invoice, "SalesOrd", "CustInvc"),
        edge(RETURN, native_credit, "RtnAuth", "CustCred"),
    ]
    real = netsuite_refunds.collect_refunds

    async def graph(reader, order_id, subsidiary, currency, *, order_reference):
        return await real(graph_reader, order_id, subsidiary, currency, order_reference=order_reference)

    types = {
        ORDER: "SalesOrd",
        RETURN: "RtnAuth",
        CREDIT: "CustCred",
        REFUND: "CustRfnd",
        invoice: "CustInvc",
        native_credit: "CustCred",
    }

    class NetSuite:
        async def request(self, method, path, *, params=None, body=None):
            query = (body or {}).get("q", "")
            if query.startswith("SELECT id, type FROM transaction WHERE id IN"):
                ids = query.split("IN (")[1].split(")")[0].split(",")
                return bulk.collection([{"id": i, "type": types[i]} for i in ids])
            raise AssertionError(f"read past the refund-graph check: {method} {path} {query[:60]}")

    @asynccontextmanager
    async def authenticated(*args, **kwargs):
        yield NetSuite()

    case_id = uuid4()
    scope = {"netsuite_account_id": "6738075", "subsidiary_id": "1"}
    review = {
        "configuration_status": "scoped_configuration_found",
        "connection_active": True,
        "native_mcp_connector_id": "conn",
        "scope": scope,
        "config_id": str(uuid4()),
        "netsuite_connection_id": str(uuid4()),
    }
    case = SimpleNamespace(id=case_id, order_reference=REFERENCE, scope_json=scope, latest_report_json={})
    config = SimpleNamespace(
        mapping_json={
            "refund_adjustments": {
                "schema_version": 1,
                "account_id": "6738075",
                "subsidiary_id": "1",
                "tax_reversal_reason_ids": ["4"],
                "tax_item_accounts": {"5005": "210"},
                "tax_accounts": ["210"],
            }
        }
    )
    sections = {
        "sales_order": {"id": ORDER, "tranId": REFERENCE},
        "posting_documents": [{"record_type": "invoice", "id": invoice, "currency": {"id": "1"}}],
        "related_refund_documents": {
            "documents": [
                {
                    "record_type": "creditMemo",
                    "id": native_credit,
                    "postingPeriod": {"id": "173"},
                    "currency": {"id": "1"},
                    "line_evidence": {"lines": [{"item": {"id": "1471"}}]},
                }
            ]
        },
        "gl": {invoice: {"rows": [{"account": "119"}]}, native_credit: {"rows": [{"account": "119"}]}},
    }
    prefetched = {
        "case_id": str(case_id),
        "field_map": None,
        "review": review,
        "report": {},
        "source": {"number": REFERENCE},
        "evidence": {"sections": sections},
    }
    monkeypatch.setattr("app.services.transaction_ops.case_service.get_case", AsyncMock(return_value=case))
    monkeypatch.setattr(
        "app.services.transaction_ops.accounting_review.accounting_context", AsyncMock(return_value=review)
    )
    monkeypatch.setattr("app.services.transaction_ops.netsuite_reader.authenticated_reader", authenticated)
    monkeypatch.setattr(netsuite_refunds, "collect_refunds", graph)
    db = SimpleNamespace(scalar=AsyncMock(return_value=config))
    with pytest.raises(clr.RefusalError) as exc:
        await clr.gather(db, "tenant", case_id, native_credit, prefetched=prefetched)
    assert (exc.value.code, exc.value.detail) == (
        "evidence_incomplete",
        {"reason": "order_documents_differ_from_refund_graph"},
    )
