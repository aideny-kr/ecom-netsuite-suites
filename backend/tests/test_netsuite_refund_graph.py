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
        if "t.otherrefnum" in kwargs["body"]["q"]:  # no credit booked by hand for the order (C09)
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
    # Requests, tagged credits (C09), two graph depths and the refund. Inventory adds no provider requests.
    assert reader.calls == 6


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
    assert reader.calls == 6


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


async def test_each_owned_credit_reports_only_what_this_orders_refunds_paid_out_of_it():
    """Packet review 2026-10-19 (F2): a refund that also pays another order's credit (999) must not
    make that credit this order's. Only allocations the graph proved owned count, per credit."""
    reader = Reader()
    reader.edges = [
        edge("1", "2", "SalesOrd", "CustInvc"),
        edge("2", "3", "CustInvc", "CustCred"),
        edge("4", "3", "CustRfnd", "CustCred"),
    ]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["credit_refunds"] == {"3": "100.00"}


async def test_a_deposit_refund_names_no_credit():
    result = await collect_refunds(Reader(), "1", "1", "1", order_reference="R123456789")
    assert result["credit_refunds"] == {}


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
    """A credit memo's header (main line), shaped like the live SuiteQL row."""
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
        "mainline": "T",
        "taxline": "F",
        "foreignamount": "-" + total,
        "expenseaccount": "119",
        "accttype": "AcctRec",
    }
    row.update(changes)
    return row


def credit_line(identifier, amount, account="774", accttype="Income", taxline="F"):
    """One of a credit memo's other lines: an item posting to `account`, or its tax line."""
    return {
        "id": identifier,
        "type": "CustCred",
        "mainline": "F",
        "taxline": taxline,
        "foreignamount": amount,
        "expenseaccount": account,
        "accttype": accttype,
    }


def invoice_row(identifier, total, tax="0", **changes):
    row = {
        "id": identifier,
        "tranid": "INV" + identifier,
        "type": "CustInvc",
        "foreigntotal": total,
        "taxtotal": tax,
        "currency": "1",
        "posting": "T",
        "voided": "F",
        "mainline": "T",
        "taxline": "F",
        "createdfrom": "1",
        "foreignamount": total,
        "expenseaccount": "119",
        "accttype": "AcctRec",
    }
    row.update(changes)
    return row


def _income_lines(headers):
    # By default each credit posts its net amount to a sales-adjustment income account.
    lines = []
    for row in headers:
        try:
            net = -Decimal(row["foreigntotal"]) + Decimal(row["taxtotal"])
        except Exception:
            continue
        lines.append(credit_line(row["id"], str(net)))
        if Decimal(row["taxtotal"]):
            lines.append(
                credit_line(row["id"], str(-Decimal(row["taxtotal"])), account=None, accttype=None, taxline="T")
            )
    return lines


class CreditReader(Reader):
    def __init__(self, credit_rows=(), lines=None, invoices=None):
        super().__init__()
        self.credit_rows = list(credit_rows)
        self.credit_rows += _income_lines(credit_rows) if lines is None else list(lines)
        self.credit_rows += [invoice_row("2", "10.00")] if invoices is None else list(invoices)
        self.credit_queries = 0

    async def request(self, method, path, **kwargs):
        if method == "POST" and "t.foreigntotal" in kwargs.get("body", {}).get("q", ""):
            self.calls += 1
            self.credit_queries += 1
            asked = set(re.search(r"t.id IN \(([^)]+)\)", kwargs["body"]["q"])[1].split(","))
            rows = [row for row in self.credit_rows if row["id"] in asked]
            return {"items": deepcopy(rows), "count": len(rows), "totalResults": len(rows), "hasMore": False}
        return await super().request(method, path, **kwargs)


INVOICE = {"ids": ["2"], "total": "10.00", "tax": "0"}
# Every read reports the order's own return credits apart from its invoice credits (C15).
NO_RETURNS = {"complete": True, "credits": [], "total": "0", "tax": "0"}


async def test_credit_memo_created_from_the_orders_invoice_is_reported_but_never_counted_as_refund():
    reader = CreditReader([credit_row("3", "CM11788", "4.82")])
    reader.edges = [edge("1", "2", "SalesOrd", "CustInvc"), edge("2", "3", "CustInvc", "CustCred")]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["amount"] == 0 and result["refund_count"] == 0
    assert result["invoice_credits"] == {
        "complete": True,
        "credits": [
            {
                "id": "3",
                "number": "CM11788",
                "origin_id": "2",
                "origin_type": "CustInvc",
                "invoice_id": "2",
                "total": "4.82",
                "tax": "0",
                "posting": "income",
            }
        ],
        "total": "4.82",
        "tax": "0",
        "invoices": INVOICE,
        "return_credits": NO_RETURNS,
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
    reader = CreditReader([credit_row("6", "CM2", "9.00", createdfrom="5")])
    reader.edges = [
        edge("1", "2", "SalesOrd", "CustInvc"),
        edge("2", "5", "CustInvc", "RtnAuth"),
        edge("5", "6", "RtnAuth", "CustCred"),
        edge("2", "7", "CustInvc", "CustCred"),
    ]
    reader.edges[-1]["nextvoided"] = "T"
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    credits = result["invoice_credits"]
    # The invoice credits are what they were before C15; the return credit is reported apart.
    assert {key: credits[key] for key in ("complete", "credits", "total", "tax")} == NO_RETURNS
    assert [(c["id"], c["origin_type"]) for c in credits["return_credits"]["credits"]] == [("6", "RtnAuth")]
    assert reader.credit_queries == 1


async def test_no_invoice_credit_means_no_extra_provider_call():
    reader = CreditReader()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"] == {**NO_RETURNS, "return_credits": NO_RETURNS}
    assert reader.calls == 6 and reader.credit_queries == 0


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
    # it and when it was only applied to it. Only "created from" makes it an invoice credit;
    # one created from the order's own return authorization is a return credit (C15).
    reader = CreditReader([credit_row("6", "CM2", "9.00", createdfrom="5")])
    reader.edges = [
        edge("1", "2", "SalesOrd", "CustInvc"),
        edge("2", "5", "CustInvc", "RtnAuth"),
        edge("5", "6", "RtnAuth", "CustCred"),
        edge("2", "6", "CustInvc", "CustCred"),
    ]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"] == {
        "complete": True,
        "credits": [],
        "total": "0",
        "tax": "0",
        "invoices": INVOICE,
        "return_credits": {
            "complete": True,
            "credits": [
                {
                    "id": "6",
                    "number": "CM2",
                    "origin_id": "5",
                    "origin_type": "RtnAuth",
                    "total": "9.00",
                    "tax": "0",
                    "posting": "income",
                }
            ],
            "total": "9.00",
            "tax": "0",
        },
    }
    assert reader.credit_queries == 1


async def test_a_credit_whose_origin_is_not_in_the_graph_makes_the_read_unknown():
    # Packet review round 2, F4: an unresolved createdfrom is not "not an invoice credit";
    # skipping it would certify a partial sum.
    reader = CreditReader([credit_row("3", "CM1", "4.82"), credit_row("8", "CM2", "20.00", createdfrom="999")])
    reader.edges = [
        edge("1", "2", "SalesOrd", "CustInvc"),
        edge("2", "3", "CustInvc", "CustCred"),
        edge("2", "8", "CustInvc", "CustCred"),
    ]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"]["complete"] is False


async def test_a_standalone_credit_applied_to_the_invoice_is_not_an_invoice_credit():
    reader = CreditReader([credit_row("3", "CM1", "4.82", createdfrom=None)])
    reader.edges = [edge("1", "2", "SalesOrd", "CustInvc"), edge("2", "3", "CustInvc", "CustCred")]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"] == {**NO_RETURNS, "invoices": INVOICE, "return_credits": NO_RETURNS}


# --- Packet review 2026-10-16 (Codex, gpt-6-astra) ----------------------------------------
# F1: credits are subtracted from the sales order, so the order's invoices are read in the
# same call and reported; the comparison requires them to equal the sales order.
# F2: header tax misses an item line posting straight into a tax account, so every other
# line of a counted credit must post to an income account that is not a tax account, and
# those lines must add up to the credit's net amount.


def invoice_credit_edges():
    return [edge("1", "2", "SalesOrd", "CustInvc"), edge("2", "3", "CustInvc", "CustCred")]


async def test_the_orders_invoices_are_read_in_the_same_call_and_reported():
    reader = CreditReader([credit_row("3", "CM1", "4.82")], invoices=[invoice_row("2", "106.00", "6.00")])
    reader.edges = invoice_credit_edges()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"]["invoices"] == {"ids": ["2"], "total": "106.00", "tax": "6.00"}
    assert reader.credit_queries == 1


@pytest.mark.parametrize(
    "lines",
    [
        [credit_line("3", "4.82", account="210", accttype="OthCurrLiab")],  # a tax liability account
        [credit_line("3", "4.82", account="774", accttype=None)],  # account type unknown
        [credit_line("3", "4.82", account=None, accttype=None)],  # no posting account
        [credit_line("3", "4.82", account="500", accttype="COGS")],
        [credit_line("3", "4.00")],  # lines that do not add up to the credit's net amount
        [credit_line("3", "4.82"), credit_line("3", "1.00", account="210", accttype="OthCurrLiab")],
        [],  # no lines at all
    ],
)
async def test_a_credit_whose_lines_are_not_proven_sales_adjustments_is_reported_unproven(lines):
    # Round 3: the reader classifies; reconciliation decides with the proofs it accepted.
    reader = CreditReader([credit_row("3", "CM1", "4.82")], lines=lines)
    reader.edges = invoice_credit_edges()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"]["complete"] is True
    assert [(c["id"], c["posting"]) for c in result["invoice_credits"]["credits"]] == [("3", "unproven")]


async def test_a_credit_line_posting_to_a_configured_tax_account_is_unknown_even_when_typed_income():
    from app.services.transaction_ops.netsuite_refunds import read_invoice_credits

    reader = CreditReader([credit_row("3", "CM1", "4.82")], lines=[credit_line("3", "4.82", account="846")])
    nodes = {
        "1": {"type": "SalesOrd", "voided": "F"},
        "2": {"type": "CustInvc", "voided": "F"},
        "3": {"type": "CustCred", "voided": "F"},
    }
    args = (reader.request, ["3"], nodes, {"1", "2", "3"}, "1")
    income = await read_invoice_credits(*args, billing=["2"], taxed=frozenset())
    assert income["credits"][0]["posting"] == "income"
    taxed = await read_invoice_credits(*args, billing=["2"], taxed=frozenset({"846"}))
    assert taxed["complete"] is True and taxed["credits"][0]["posting"] == "unproven"


async def test_zero_amount_lines_and_the_native_tax_line_need_no_income_account():
    lines = [
        credit_line("3", "4.82"),
        credit_line("3", "0", account="777", accttype="Income"),  # a $0 shipping line
        credit_line("3", "0", account=None, accttype=None, taxline="T"),  # "-Not Taxable-"
    ]
    reader = CreditReader([credit_row("3", "CM1", "4.82")], lines=lines)
    reader.edges = invoice_credit_edges()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"]["complete"] is True and result["invoice_credits"]["total"] == "4.82"


@pytest.mark.parametrize(
    "invoices",
    [
        [],  # the invoice vanished between the graph and the read
        [invoice_row("2", "10.00", voided="T")],
        [invoice_row("2", "10.00", currency="2")],
        [invoice_row("2", "abc")],
        [invoice_row("2", "10.00", taxtotal=None)],
        [invoice_row("2", "10.00"), invoice_row("2", "10.00")],  # duplicated header
    ],
)
async def test_an_unproven_invoice_read_makes_the_credits_unknown(invoices):
    reader = CreditReader([credit_row("3", "CM1", "4.82")], invoices=invoices)
    reader.edges = invoice_credit_edges()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"]["complete"] is False


async def test_an_order_also_billed_by_cash_sale_has_unknown_invoice_credits():
    reader = CreditReader([credit_row("3", "CM1", "4.82")])
    reader.edges = [*invoice_credit_edges(), edge("1", "9", "SalesOrd", "CashSale")]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"] == {"complete": False, "reason": "invoice_credit_billing_unproven"}


async def test_a_returned_items_inventory_cost_lines_carry_no_amount_and_do_not_block_the_credit():
    # Framework's lost-shipment and replacement credits (CM9762, CM11088 on 2026-10-16) return
    # the items: each item line has a cost pair (COGS and inventory asset) with no transaction
    # amount. Those lines cannot move the order total or tax; the net-amount check still has to hold.
    lines = [
        credit_line("3", "4.82", account="54"),
        credit_line("3", None, account="230", accttype="COGS"),
        credit_line("3", None, account="212", accttype="OthCurrAsset"),
    ]
    reader = CreditReader([credit_row("3", "CM1", "4.82")], lines=lines)
    reader.edges = invoice_credit_edges()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"]["complete"] is True and result["invoice_credits"]["total"] == "4.82"


@pytest.mark.parametrize(
    "line",
    [
        credit_line("3", None, account="774", accttype="Income"),  # a sales line with its amount missing
        credit_line("3", None, account="210", accttype="OthCurrLiab"),
        credit_line("3", None, account=None, accttype=None),
    ],
)
async def test_any_other_line_without_an_amount_makes_the_credit_unproven(line):
    reader = CreditReader([credit_row("3", "CM1", "4.82")], lines=[credit_line("3", "4.82"), line])
    reader.edges = invoice_credit_edges()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"]["credits"][0]["posting"] == "unproven"


# Packet review rounds 2-3, F3/F4: a tax reversal is itself a credit created from the invoice
# and posts to a tax account. The reader never drops a credit: it reports every one with its
# classification, and reconciliation leaves out only the credits a tax adjustment it actually
# applied accounts for. Dropping in the reader (round 2's fix) lost a credit whose tax proof
# reconciliation later rejected, so a partial sum could close a case (F4).
def tax_reversal_and_income_credit():
    rows = [credit_row("3", "CM3", "20.00"), credit_row("4", "CM4", "20.00")]
    lines = [credit_line("3", "20.00", account="210", accttype="OthCurrLiab"), credit_line("4", "20.00")]
    reader = CreditReader(rows, lines=lines, invoices=[invoice_row("2", "120.00", "20.00")])
    reader.edges = [*invoice_credit_edges(), edge("2", "4", "CustInvc", "CustCred")]
    return reader


async def test_a_tax_reversal_is_reported_unproven_beside_an_income_credit():
    reader = tax_reversal_and_income_credit()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["invoice_credits"]["complete"] is True
    assert [(c["id"], c["posting"]) for c in result["invoice_credits"]["credits"]] == [
        ("3", "unproven"),
        ("4", "income"),
    ]
    assert result["invoice_credits"]["total"] == "40.00"


async def test_the_reader_never_drops_a_credit_a_tax_adjustment_names(monkeypatch):
    from app.services.transaction_ops import netsuite_refunds as service

    async def tax_adjustments(*args, **kwargs):
        return [{"credit_memo_id": "3", "amount": "20.00", "tax_amount": "20.00"}]

    monkeypatch.setattr(service, "read_tax_adjustments", tax_adjustments)
    profile = {
        "schema_version": 1,
        "account_id": "123-sb1",
        "subsidiary_id": "1",
        "tax_reversal_reason_ids": ["102"],
        "tax_item_accounts": {"80": "210"},
        "tax_accounts": ["210"],
    }
    reader = tax_reversal_and_income_credit()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789", adjustment_profile=profile)
    assert [c["id"] for c in result["invoice_credits"]["credits"]] == ["3", "4"]


# --- Credit memos created from the order's own return authorization (C15) ----------------
# A fully returned order is credited from its return authorization, not its invoice, so the
# invoice credits never saw it. The reader reports those credits as `return_credits`, apart
# from the invoice credits, which stay exactly what they were: a problem with a return credit
# makes only the return credits unknown. They are never refunded money.


def return_edges():
    """Order 1 billed by invoice 2; its return authorization 5 and credit 6 created from it."""
    return [
        edge("1", "2", "SalesOrd", "CustInvc"),
        edge("1", "5", "SalesOrd", "RtnAuth"),
        edge("5", "6", "RtnAuth", "CustCred"),
    ]


def without_returns(credits):
    return {key: value for key, value in credits.items() if key != "return_credits"}


async def test_a_refunded_return_credit_the_invoice_never_links_is_read_and_never_counted_as_refunded_twice():
    # The order is returned in full: credit 6 from return authorization 5, refunded by 4. No
    # invoice links to the credit, so before C15 the reader never saw it.
    reader = CreditReader(
        [credit_row("6", "CM6", "100.00", tax="10.00", createdfrom="5")],
        invoices=[invoice_row("2", "100.00", "10.00")],
    )
    reader.edges = [*return_edges(), edge("4", "6", "CustRfnd", "CustCred")]
    reader.record["apply"]["items"] = [{"apply": True, "doc": {"id": "6"}, "line": 1, "amount": "100.00"}]
    reader.record["total"] = "100.00"
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R900000801")
    # The refund is counted once, as the refund it is; the credit is not more refunded money.
    assert result["amount"] == Decimal("100.00") and result["refund_count"] == 1
    assert result["credit_refunds"] == {"6": "100.00"}
    credits = result["invoice_credits"]
    assert without_returns(credits) == {**NO_RETURNS, "invoices": {"ids": ["2"], "total": "100.00", "tax": "10.00"}}
    assert credits["return_credits"] == {
        "complete": True,
        "credits": [
            {
                "id": "6",
                "number": "CM6",
                "origin_id": "5",
                "origin_type": "RtnAuth",
                "total": "100.00",
                "tax": "10.00",
                "posting": "income",
            }
        ],
        "total": "100.00",
        "tax": "10.00",
    }
    assert reader.credit_queries == 1


async def test_another_orders_return_credit_applied_to_this_invoice_is_skipped_and_the_invoice_credit_still_counts():
    # Credit 8 was created from another order's return authorization (900) and only applied
    # here. It is not this order's, so it is skipped exactly as before C15, never refused.
    rows = [credit_row("3", "CM3", "4.82"), credit_row("8", "CM8", "20.00", createdfrom="900")]
    reader = CreditReader(rows)
    reader.edges = [
        *invoice_credit_edges(),
        edge("2", "8", "CustInvc", "CustCred"),
        edge("900", "8", "RtnAuth", "CustCred"),
    ]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R900000802")
    credits = result["invoice_credits"]
    assert credits["complete"] is True
    assert [(c["id"], c["origin_type"]) for c in credits["credits"]] == [("3", "CustInvc")]
    assert credits["total"] == "4.82" and credits["return_credits"] == NO_RETURNS


@pytest.mark.parametrize(
    "shared",
    [
        edge("800", "5", "SalesOrd", "RtnAuth"),  # the return authorization is also another order's
        edge("901", "6", "CustInvc", "CustCred"),  # the return credit is applied to another order's invoice
    ],
)
async def test_a_return_credit_another_order_shares_makes_only_the_return_credits_unknown(shared):
    rows = [credit_row("3", "CM3", "4.82"), credit_row("6", "CM6", "9.00", createdfrom="5")]
    reader = CreditReader(rows)
    reader.edges = [*return_edges(), edge("2", "3", "CustInvc", "CustCred"), shared]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R900000803")
    credits = result["invoice_credits"]
    assert credits["complete"] is True and [c["id"] for c in credits["credits"]] == ["3"]
    assert credits["return_credits"] == {"complete": False, "reason": "return_credit_ownership_ambiguous"}


@pytest.mark.parametrize(
    "row",
    [
        credit_row("6", "CM6", "9.00", createdfrom="5", foreigntotal="9.00"),  # a credit cannot be positive
        credit_row("6", "CM6", "9.00", createdfrom="5", currency="2"),
        credit_row("6", "CM6", "9.00", createdfrom="5", posting="F"),
        credit_row("6", "CM6", "9.00", createdfrom="999"),  # an origin the graph never saw
        credit_row("6", "CM6", "9.00", createdfrom="2"),  # reached from the return, created elsewhere
        credit_row("6", "CM6", "9.00", createdfrom=None),
    ],
)
async def test_an_unproven_return_credit_never_touches_the_invoice_credits(row):
    reader = CreditReader([credit_row("3", "CM3", "4.82"), row])
    reader.edges = [*return_edges(), edge("2", "3", "CustInvc", "CustCred")]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R900000804")
    credits = result["invoice_credits"]
    assert credits["complete"] is True and credits["total"] == "4.82"
    assert [c["id"] for c in credits["credits"]] == ["3"]
    assert credits["return_credits"]["complete"] is False


async def test_a_missing_return_credit_header_never_touches_the_invoice_credits():
    reader = CreditReader([credit_row("3", "CM3", "4.82")])
    reader.edges = [*return_edges(), edge("2", "3", "CustInvc", "CustCred")]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R900000804")
    credits = result["invoice_credits"]
    assert credits["complete"] is True and [c["id"] for c in credits["credits"]] == ["3"]
    assert credits["return_credits"] == {"complete": False, "reason": "return_credit_read_incomplete"}


async def test_a_return_credit_with_a_non_income_line_is_reported_unproven_beside_the_invoice_credit():
    # A restocking fee posted to an expense account: the reader classifies, reconciliation decides.
    rows = [credit_row("3", "CM3", "4.82"), credit_row("6", "CM6", "9.00", createdfrom="5")]
    lines = [credit_line("3", "4.82"), credit_line("6", "8.00"), credit_line("6", "1.00", "610", "Expense")]
    reader = CreditReader(rows, lines=lines)
    reader.edges = [*return_edges(), edge("2", "3", "CustInvc", "CustCred")]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R900000805")
    credits = result["invoice_credits"]
    assert [(c["id"], c["posting"]) for c in credits["credits"]] == [("3", "income")]
    assert credits["return_credits"]["complete"] is True
    assert [(c["id"], c["posting"]) for c in credits["return_credits"]["credits"]] == [("6", "unproven")]


async def test_more_return_credits_than_the_budget_never_touch_the_invoice_credits():
    from app.services.transaction_ops.netsuite_refunds import MAX_INVOICE_CREDITS

    returns = [str(10 + i) for i in range(MAX_INVOICE_CREDITS)]
    reader = CreditReader(
        [credit_row("3", "CM3", "4.82"), *(credit_row(i, "CM" + i, "1.00", createdfrom="5") for i in returns)]
    )
    reader.edges = [*return_edges()[:2], edge("2", "3", "CustInvc", "CustCred")]
    reader.edges += [edge("5", i, "RtnAuth", "CustCred") for i in returns]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R900000806")
    credits = result["invoice_credits"]
    assert credits["complete"] is True and credits["total"] == "4.82"
    assert credits["return_credits"] == {"complete": False, "reason": "return_credit_budget"}
    assert reader.credit_queries == 1


async def test_return_credit_rows_past_their_budget_never_touch_the_invoice_credits(monkeypatch):
    monkeypatch.setattr("app.services.transaction_ops.netsuite_refunds.MAX_RETURN_CREDIT_ROWS", 1)
    reader = CreditReader([credit_row("3", "CM3", "4.82"), credit_row("6", "CM6", "9.00", createdfrom="5")])
    reader.edges = [*return_edges(), edge("2", "3", "CustInvc", "CustCred")]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R900000806")
    credits = result["invoice_credits"]
    assert credits["complete"] is True and [c["id"] for c in credits["credits"]] == ["3"]
    assert credits["return_credits"] == {"complete": False, "reason": "return_credit_read_incomplete"}


async def test_a_read_with_no_return_credit_asks_exactly_what_it_asked_before():
    asked = []
    reader = CreditReader([credit_row("3", "CM3", "4.82")])
    reader.edges = invoice_credit_edges()
    original = reader.request

    async def request(method, path, **kwargs):
        if "t.foreigntotal" in kwargs.get("body", {}).get("q", ""):
            asked.append((kwargs["params"], kwargs["body"]["q"].rsplit("WHERE", 1)[1]))
        return await original(method, path, **kwargs)

    reader.request = request
    await collect_refunds(reader, "1", "1", "1", order_reference="R900000809")
    assert asked == [({"limit": 201}, " t.id IN (3,2) AND (t.type='CustCred' OR tl.mainline='T')")]


async def test_a_cash_sale_order_with_a_return_credit_keeps_its_invoice_credits_unchanged():
    # Billed by cash sale 9 and returned through 5: no invoice, so no invoice credit, exactly as
    # before C15. The return credit cannot be counted against invoices either, so it is unknown.
    reader = CreditReader([credit_row("6", "CM6", "9.00", createdfrom="5")])
    reader.edges = [
        edge("1", "9", "SalesOrd", "CashSale"),
        edge("1", "5", "SalesOrd", "RtnAuth"),
        edge("5", "6", "RtnAuth", "CustCred"),
    ]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R900000807")
    assert result["invoice_credits"] == {
        **NO_RETURNS,
        "return_credits": {"complete": False, "reason": "invoice_credit_billing_unproven"},
    }
    assert reader.credit_queries == 0


async def test_an_unproven_invoice_read_for_return_credits_alone_never_makes_the_invoice_credits_unknown():
    reader = CreditReader([credit_row("6", "CM6", "9.00", createdfrom="5")], invoices=[])
    reader.edges = return_edges()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R900000808")
    credits = result["invoice_credits"]
    assert without_returns(credits) == NO_RETURNS
    assert credits["return_credits"]["complete"] is False


# Canceled orders (decided by Aiden, 2026-10-22): reconciliation settles a canceled order only when it
# can see that nothing was billed and every deposit is fully applied. The graph names the order's billing
# documents and reports its deposits from the status and total columns of the graph query it already
# runs: no extra call, no extra time (PR #422 review round 1, F2/F3).
def with_document(row, prefix, status, total):
    return {**row, prefix + "status": status, prefix + "total": total}


class DepositReader(Reader):
    """The default graph (order 1 -> deposit 2 -> application 3 <- refund 4) with document columns."""

    def __init__(self, status="C", total="100.00"):
        super().__init__()
        self.edges = [
            with_document(edge("1", "2", "SalesOrd", "CustDep"), "next", status, total),
            with_document(edge("2", "3", "CustDep", "DepAppl"), "previous", status, total),
            edge("4", "3", "CustRfnd", "DepAppl"),
        ]


async def test_the_graph_names_the_orders_billing_documents_and_deposits_at_no_extra_call():
    plain = Reader()
    await collect_refunds(plain, "1", "1", "1", order_reference="R123456789")
    reader = DepositReader()
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["billing_ids"] == []
    assert result["deposits"] == [{"id": "2", "total": "100.00", "status": "C"}]
    assert reader.calls == plain.calls
    reader = DepositReader()
    reader.edges.append(edge("1", "5", "SalesOrd", "CustInvc"))
    assert (await collect_refunds(reader, "1", "1", "1", order_reference="R123456789"))["billing_ids"] == ["5"]


async def test_an_order_with_no_deposit_has_none():
    reader = DepositReader()
    reader.edges = []
    assert (await collect_refunds(reader, "1", "1", "1", order_reference="R123456789"))["deposits"] == []


async def test_a_deposit_shared_with_another_order_is_unknown_not_absent():
    # PR #422 review round 1 (F1): a deposit linked to two sales orders is not owned by either.
    reader = DepositReader()
    reader.edges = [
        with_document(edge("1", "2", "SalesOrd", "CustDep"), "next", "B", "100.00"),
        with_document(edge("900", "2", "SalesOrd", "CustDep"), "next", "B", "100.00"),
    ]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["deposits"] is None


@pytest.mark.parametrize(
    "status,total",
    [(None, "100.00"), ("C", None), ("C", "0"), ("C", "x"), (3, "100.00")],
)
async def test_a_deposit_the_rows_do_not_describe_is_unknown_and_never_breaks_the_refund_proof(status, total):
    reader = DepositReader(status, total)
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["deposits"] is None
    assert result["amount"] == Decimal("100.00")  # the refund proof stands


async def test_rows_that_disagree_on_a_deposit_leave_it_unknown():
    reader = DepositReader()
    reader.edges[1] = with_document(reader.edges[1], "previous", "B", "100.00")  # the second row says B
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["deposits"] is None and result["amount"] == Decimal("100.00")


def test_the_graph_query_reads_each_documents_status_and_total():
    from app.services.transaction_ops.netsuite_refunds import _query

    query = _query({"1"})
    for column in ("p.status AS previousstatus", "n.status AS nextstatus", "p.foreigntotal AS previoustotal"):
        assert column in query


# C14b (2026-10-27): a fully billed order whose sales order header was never updated. Reconciliation needs
# the posted invoices' own totals, tax and exchange rates, read from the columns of the graph query it
# already runs: no extra call. Anything the rows do not prove leaves them unknown, never zero.
def with_invoice(row, prefix, total, tax, rate="1"):
    return {**row, prefix + "total": total, prefix + "taxtotal": tax, prefix + "exchangerate": rate}


def invoiced_reader(*invoices):
    reader = DepositReader()
    for identifier, total, tax, *rate in invoices:
        reader.edges.append(with_invoice(edge("1", identifier, "SalesOrd", "CustInvc"), "next", total, tax, *rate))
    return reader


async def test_the_graph_sums_the_orders_posted_invoices_at_no_extra_call():
    plain = DepositReader()
    await collect_refunds(plain, "1", "1", "1", order_reference="R123456789")
    reader = invoiced_reader(("5", "0", "0", "0.0918428"), ("6", "2499", "499.8", "0.0924702"))
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["billed_invoices"] == {
        "complete": True,
        "ids": ["5", "6"],
        "total": "2499",
        "tax": "499.8",
        "exchange_rates": ["0.0918428", "0.0924702"],
    }
    assert reader.calls == plain.calls


async def test_an_order_with_nothing_billed_has_no_billed_invoices():
    result = await collect_refunds(DepositReader(), "1", "1", "1", order_reference="R123456789")
    assert result["billed_invoices"] == {"complete": False, "reason": "not_billed"}


@pytest.mark.parametrize(
    "change, reason",
    [
        (lambda r: r.edges.append(edge("1", "7", "SalesOrd", "CashSale")), "billed_by_cash_sale"),
        (
            lambda r: r.edges.append(with_invoice(edge("900", "5", "SalesOrd", "CustInvc"), "next", "10", "0")),
            "billing_ownership_ambiguous",
        ),
        (lambda r: r.edges[-1].update(nextposting="F"), "billing_unproven"),
        (lambda r: r.edges[-1].update(nexttaxtotal=None), "billing_amounts_unread"),
        (lambda r: r.edges[-1].update(nexttotal="-1"), "billing_amounts_unread"),
        (lambda r: r.edges[-1].update(nexttaxtotal=0.5), "billing_amounts_unread"),  # never a binary float
        (lambda r: r.edges[-1].update(nextexchangerate=None), "billing_amounts_unread"),
        # two rows that disagree on the same invoice
        (
            lambda r: r.edges.append(with_invoice(edge("5", "8", "CustInvc", "CustPymt"), "previous", "10", "1")),
            "billing_amounts_unread",
        ),
    ],
)
async def test_invoices_the_rows_do_not_prove_are_unknown_and_never_break_the_refund_proof(change, reason):
    reader = invoiced_reader(("5", "10", "0"))
    change(reader)
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["billed_invoices"] == {"complete": False, "reason": reason}
    assert result["amount"] == Decimal("100.00")


async def test_a_voided_invoice_is_not_billed():
    reader = invoiced_reader(("5", "10", "0"), ("6", "5", "0"))
    reader.edges[-1].update(nextvoided="T")
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert (result["billed_invoices"]["ids"], result["billed_invoices"]["total"]) == (["5"], "10")


def test_the_graph_query_reads_each_documents_tax_and_exchange_rate():
    from app.services.transaction_ops.netsuite_refunds import _query

    query = _query({"1"})
    for column in ("n.taxtotal AS nexttaxtotal", "p.exchangerate AS previousexchangerate"):
        assert column in query


# C14b review R1: a standalone credit applied to an invoice is no invoice credit (it has no createdfrom),
# so empty invoice credits do not prove the invoice was not reduced. Only the documents NetSuite links to
# an invoice can: its sales order, its payments, deposit applications and realized FX revaluations.
@pytest.mark.parametrize("neighbour", ["CustCred", "Journal", "CustRfnd", "RtnAuth", "CashRfnd", "Unknown"])
async def test_an_invoice_reduced_by_any_other_linked_document_is_unknown(neighbour):
    reader = invoiced_reader(("5", "100.00", "20.00"))
    reader.edges.append(with_invoice(edge("5", "9", "CustInvc", neighbour), "previous", "100.00", "20.00"))
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["billed_invoices"] == {"complete": False, "reason": "billing_reduced_or_unproven"}
    assert result["amount"] == Decimal("100.00")


async def test_payments_deposit_applications_and_fx_revaluations_leave_the_invoice_as_billed():
    reader = invoiced_reader(("5", "100.00", "20.00"))
    reader.edges += [
        with_invoice(edge("5", following, "CustInvc", kind), "previous", "100.00", "20.00")
        for following, kind in (("10", "CustPymt"), ("11", "DepAppl"), ("12", "FxReval"))
    ]
    result = await collect_refunds(reader, "1", "1", "1", order_reference="R123456789")
    assert result["billed_invoices"]["complete"] is True
