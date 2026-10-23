"""A billed sales order whose invoice already equals Solidus gets the header discount it lacks.

Decided by Aiden 2026-10-22: the sales order must match (10-18), and the amendment is a header discount
equal to the sales order total less the invoice total, the mechanism already proven on a billed order
(R146850445, 2026-09-14). About 40 open Framework cases have this shape: twelve orders adjusted to zero
in Solidus are invoiced at 0.00 while the sales order keeps the full price. Only a net gap is amended:
the invoice must equal Solidus in total, and the tax is zero on Solidus, the invoice and the order.
"""

from copy import deepcopy

import pytest

from app.services.transaction_ops import sales_order_alignment as alignment
from tests.test_sales_order_alignment import inputs as discount_inputs


def inputs():
    """The existing fixture with the invoice already equal to Solidus (95) and no proof of an invoice
    discount: the invoice reached 95 its own way; the sales order still says 100."""
    d = discount_inputs()
    support = d["support"]
    invoice = support["invoice"]
    invoice.update(discountItem=None, discountRate=None, discountTotal="0", status={"id": "Paid In Full"})
    support.update(invoice_amount_paid="95", invoice_amount_remaining="0", returns_and_credits=[])
    for key in ("invoice_applications", "discount_item"):
        support.pop(key)
    d.pop("evidence")
    return d


def test_the_order_gets_a_header_discount_down_to_its_invoice():
    d = inputs()
    before = deepcopy(d)
    p = alignment.build_matching_candidate(**d)
    assert p["kind"] == "sales_order_source_alignment" and p["basis"] == "invoice_matches_source"
    assert (p["record_type"], p["record_id"], p["invoice_id"]) == ("salesorder", "90", "20")
    assert p["proposed_fields"] == {"discountItem": {"id": "50"}, "discountRate": -5.0}
    assert p["expected_after"] == {"total": "95", "subtotal": "100", "taxTotal": "0.00", "discountTotal": "-5"}
    assert "already equals the finalized source" in p["approval_basis"]
    assert d == before


@pytest.mark.parametrize(
    "section,key,value",
    [
        # The invoice must already equal Solidus: this amendment never changes what was billed.
        ("invoice", "total", "94"),
        ("invoice", "taxTotal", "1"),
        ("invoice", "createdFrom", {"id": "99"}),
        ("invoice", "entity", {"id": "99"}),
        ("invoice", "exchangeRate", "2"),
        # A net gap on an order with no tax and no existing header discount, fully billed.
        ("order", "status", {"id": "F"}),
        ("order", "taxTotal", "1"),
        ("order", "taxRate", "1"),
        ("order", "discountItem", {"id": "50"}),
        ("order", "discountRate", "-1"),
        ("order", "discountTotal", "-1"),
        ("order", "billingSchedule", {"id": "1"}),
        ("order", "shippingCost", "5"),
        ("order", "total", "95"),  # nothing to amend
        ("order", "total", "90"),  # the order is below its invoice: not a discount
        ("order", "exchangeRate", "2"),
        ("currency", "symbol", "CAD"),
        ("currency", "currencyPrecision", 0),
    ],
)
def test_anything_but_a_billed_net_gap_against_a_matching_invoice_is_refused(section, key, value):
    d = inputs()
    d["support"][section][key] = value
    assert alignment.build_matching_candidate(**d) is None


@pytest.mark.parametrize(
    "linked",
    [
        [],
        [{"id": "20", "type": "CustInvc"}, {"id": "21", "type": "CustInvc"}],
        [{"id": "20", "type": "CustInvc"}, {"id": "30", "type": "CashSale"}],
        [{"id": "20", "type": "CustInvc"}, {"id": "31", "type": "CustCred"}],
        [{"id": "20", "type": "CustInvc"}, {"id": "32", "type": "CustRfnd"}],
        [{"id": "21", "type": "CustInvc"}],
    ],
)
def test_one_invoice_and_nothing_else_billed_or_returned_from_the_order(linked):
    d = inputs()
    d["support"]["linked_documents"] = linked
    assert alignment.build_matching_candidate(**d) is None


def test_shipments_from_the_order_are_fine():
    d = inputs()
    d["support"]["linked_documents"].append({"id": "40", "type": "ItemShip"})
    assert alignment.build_matching_candidate(**d) is not None


@pytest.mark.parametrize(
    "credits",
    [[{"id": "60", "type": "CustCred"}], [{"id": "61", "type": "RtnAuth"}], None, "unread"],
)
def test_a_return_or_credit_on_the_order_or_invoice_or_an_unread_check_refuses(credits):
    # A return or credit on the order or its invoice means NetSuite's net is already below Solidus.
    d = inputs()
    d["support"]["returns_and_credits"] = credits
    assert alignment.build_matching_candidate(**d) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("tax_total", "1"),
        ("total", "100"),
        ("state", "canceled"),
        ("requires_review", True),
        ("completed_at", None),
        ("currency", "CAD"),
    ],
)
def test_only_a_finalized_tax_free_source_order(field, value):
    d = inputs()
    d["source"][field] = value
    assert alignment.build_matching_candidate(**d) is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("quantityBilled", "0"),
        ("isClosed", True),
        ("itemType", {"id": "Discount"}),
    ],
)
def test_every_order_line_is_billed_and_none_is_a_discount_or_closed(key, value):
    d = inputs()
    d["support"]["order"]["lines"][0][key] = value
    assert alignment.build_matching_candidate(**d) is None


def test_the_returns_and_credits_query_reads_the_order_and_the_invoice():
    # PR #423 review round 1 (R1): a credit created from a return authorization links to neither the
    # invoice nor the order's billing documents; the authorization itself links from the order.
    query = alignment.returns_and_credits_query("90", "20", "1")
    assert "l.previousdoc IN (90,20)" in query
    assert "t.type IN ('RtnAuth','CustCred','CashRfnd')" in query
    with pytest.raises(ValueError):
        alignment.returns_and_credits_query("90", "20 OR 1=1", "1")


def _patch_context(monkeypatch, d, review, source, sections):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "app.services.mcp_connector_service.get_mcp_connector",
        AsyncMock(
            return_value=SimpleNamespace(
                status="active", is_enabled=True, server_url="https://123.suitetalk.api.netsuite.com"
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.transaction_ops.case_service.get_case",
        AsyncMock(return_value=SimpleNamespace(id=d["case_id"], scope_json={}, latest_report_json={})),
    )
    monkeypatch.setattr(
        "app.services.transaction_ops.accounting_review.accounting_context", AsyncMock(return_value=review)
    )
    monkeypatch.setattr("app.services.transaction_ops.tax_correction.refresh_source", AsyncMock(return_value=source))
    monkeypatch.setattr(
        "app.services.transaction_ops.accounting_evidence.collect_accounting_evidence",
        AsyncMock(return_value={"sections": sections}),
    )
    commercial = AsyncMock()
    monkeypatch.setattr("app.services.transaction_ops.commercial_credits.collect_commercial_credits", commercial)
    return commercial


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [None, "returns_and_credits", "invoice_gl", "invoice", "basis", "source"])
async def test_approval_re_proves_the_same_basis_on_fresh_evidence(monkeypatch, changed):
    import json
    from unittest.mock import AsyncMock
    from uuid import uuid4

    d = inputs()
    d["case_id"] = uuid4()
    d["review"]["native_mcp_connector_id"] = str(uuid4())
    p = alignment.build_matching_candidate(**d)
    fresh = deepcopy(p)
    if changed == "returns_and_credits":
        fresh["support"]["returns_and_credits"] = [{"id": "60"}]
    elif changed in {"invoice_gl", "invoice"}:
        fresh["support"][changed]["unexpected_change"] = True
    elif changed == "basis":
        fresh["basis"] = None
    elif changed == "source":
        fresh["source"]["total"] = "94"
    commercial = _patch_context(monkeypatch, d, deepcopy(d["review"]), d["source"], {})
    matching = AsyncMock(return_value=fresh)
    discount = AsyncMock(return_value=None)
    monkeypatch.setattr(alignment, "prepare_matching", matching)
    monkeypatch.setattr(alignment, "prepare", discount)
    call = alignment.validate_approved(
        None,
        d["tenant_id"],
        f"ext__{p['connector_id'].replace('-', '')}__ns_updateRecord",
        {"recordType": "salesorder", "recordId": p["record_id"], "data": json.dumps(p["proposed_fields"])},
        p,
    )
    if changed:
        with pytest.raises(ValueError):
            await call
    else:
        await call
    # Never the invoice-discount treatment's evidence or preparation.
    matching.assert_awaited_once()
    discount.assert_not_awaited()
    commercial.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        None,
        "credit_added",
        "credits_unread",
        "invoice_total",
        "gl",
        "source",
        "lines",
        # PR #423 review round 1 (R2): what the later evidence sees, and the last read, also count.
        "later_invoice_paid_down",
        "later_invoice_modified",
        "later_invoice_missing",
        "credit_at_the_last_read",
        "last_read_unread",
        # Round 2 (R4): the later evidence must also show one billing document and the same order.
        "later_second_invoice",
        "later_cash_sale",
        "later_order_total_changed",
        "later_order_modified",
        "later_links_incomplete",
    ],
)
async def test_readback_needs_the_order_amended_and_the_invoice_and_its_credits_unchanged(monkeypatch, changed):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from uuid import uuid4

    d = inputs()
    d["case_id"] = uuid4()
    p = alignment.build_matching_candidate(**d)
    fresh = {"order": deepcopy(p["before"]), **deepcopy(p["support"])}
    fresh["order"].update(total="95", discountItem={"id": "50"}, discountRate="-5", discountTotal="-5")
    later = {
        "id": "20",
        "lastModifiedDate": fresh["invoice"]["lastModifiedDate"],
        "total": fresh["invoice"]["total"],
        "amountPaid": fresh["invoice_amount_paid"],
        "amountRemaining": fresh["invoice_amount_remaining"],
    }
    sections = {
        "gl": {"20": deepcopy(fresh["invoice_gl"])},
        "posting_documents": [later],
        "sales_order": {
            "id": "90",
            "total": fresh["order"]["total"],
            "lastModifiedDate": fresh["order"]["lastModifiedDate"],
        },
        "linked_documents": {"complete": True, "rows": [{"id": "20", "type": "CustInvc"}]},
    }
    source = deepcopy(p["source"])
    last = []
    if changed == "credit_added":
        fresh["returns_and_credits"] = [{"id": "60", "type": "CustCred"}]
    if changed == "credits_unread":
        fresh["returns_and_credits"] = None
    if changed == "invoice_total":
        fresh["invoice"]["total"] = "90"
    if changed == "gl":
        sections["gl"]["20"]["rows"][1]["account"] = "999"
    if changed == "source":
        source["total"] = "94"
    if changed == "lines":
        fresh["order"]["lines"][0]["quantityBilled"] = "0"
    if changed == "later_invoice_paid_down":
        later.update(amountPaid="10", amountRemaining="85")
    if changed == "later_invoice_modified":
        later["lastModifiedDate"] = "2026-10-23T00:00:00Z"
    if changed == "later_invoice_missing":
        sections["posting_documents"] = []
    if changed == "later_second_invoice":
        sections["linked_documents"]["rows"].append({"id": "21", "type": "CustInvc"})
        sections["posting_documents"].append({**later, "id": "21"})
    if changed == "later_cash_sale":
        sections["linked_documents"]["rows"].append({"id": "22", "type": "CashSale"})
    if changed == "later_order_total_changed":
        sections["sales_order"]["total"] = "105"
    if changed == "later_order_modified":
        sections["sales_order"]["lastModifiedDate"] = "2026-10-23T00:00:00Z"
    if changed == "later_links_incomplete":
        sections["linked_documents"]["complete"] = False
    if changed == "credit_at_the_last_read":
        last = [{"id": "60", "type": "CustCred"}]
    if changed == "last_read_unread":
        last = None
    read = AsyncMock(return_value=fresh)
    monkeypatch.setattr(alignment, "read_support", read)
    commercial = _patch_context(monkeypatch, d, {}, source, sections)

    @asynccontextmanager
    async def reader(*args, **kwargs):
        yield object()

    final = AsyncMock(return_value=last)
    monkeypatch.setattr(alignment, "authenticated_reader", reader)
    monkeypatch.setattr(alignment, "read_returns_and_credits", final)
    result = await alignment.verify_after(None, p["tenant_id"], p)
    assert result["status"] == ("verified" if changed is None else "needs_review")
    assert result["retry_allowed"] is False
    assert read.await_args.kwargs["returns_and_credits"] is True
    commercial.assert_not_awaited()
    if changed is None:
        final.assert_awaited_once()  # the last read happens after the evidence


@pytest.mark.parametrize(
    "change,expected",
    [
        (None, True),
        (lambda o, i, s: i.update(total="94"), False),  # the invoice is not the source
        (lambda o, i, s: o.update(total="95"), False),  # nothing to amend
        (lambda o, i, s: o.update(taxTotal="1"), False),
        (lambda o, i, s: i.update(taxTotal="1"), False),
        (lambda o, i, s: i.update(record_type="cashsale"), False),
        (lambda o, i, s: s.update(total=None), False),
    ],
)
def test_the_cheap_gate_reads_only_evidence_already_collected(change, expected):
    order = {"total": "100", "taxTotal": "0"}
    invoice = {"total": "95", "taxTotal": "0", "record_type": "invoice"}
    source = {"total": "95"}
    if change:
        change(order, invoice, source)
    sections = {"sales_order": order, "posting_documents": [invoice]}
    assert alignment.matching_invoice_shape(source, sections) is expected
    assert alignment.matching_invoice_shape(source, {**sections, "posting_documents": [invoice, invoice]}) is False


def test_a_source_order_with_no_review_flag_is_final_like_every_other_treatment_reads_it():
    d = inputs()
    d["source"]["requires_review"] = None
    assert alignment.build_matching_candidate(**d) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", [True, False])
async def test_the_evidence_tool_tries_the_amendment_only_when_the_evidence_shows_its_shape(monkeypatch, shape):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from app.mcp.tools import transaction_ops_tools as mod
    from app.services.transaction_ops import (
        accounting_evidence,
        accounting_review,
        case_service,
        commercial_credits,
        credit_reallocation,
        resolution_assessment,
        sales_credit,
        tax_correction,
    )
    from app.services.transaction_ops import case_resolution_scope as scope
    from tests.test_accounting_case_scope import restriction
    from tests.test_native_accounting_service import prepared

    p, data = prepared()
    source, review, evidence, _ = data[:4]
    review = {**review, "sales_credit_profile": {"item_id": "50"}}
    evidence.update(blockers=[], assessment={})
    db = AsyncMock()
    db.info = {}
    db.scalar.return_value = None
    tenant, actor_id, case_id = uuid4(), uuid4(), uuid4()
    case = SimpleNamespace(id=case_id, scope_json={}, latest_report_json={}, order_reference=source["number"])
    monkeypatch.setattr(mod, "_authorize", AsyncMock(return_value=(db, tenant, SimpleNamespace(id=actor_id))))
    monkeypatch.setattr(scope, "load", AsyncMock(return_value=restriction()))
    monkeypatch.setattr(case_service, "get_case", AsyncMock(return_value=case))
    monkeypatch.setattr(accounting_review, "accounting_context", AsyncMock(return_value=review))
    monkeypatch.setattr(accounting_evidence, "collect_accounting_evidence", AsyncMock(return_value=evidence))
    monkeypatch.setattr(tax_correction, "refresh_source", AsyncMock(return_value=source))
    monkeypatch.setattr(commercial_credits, "collect_commercial_credits", AsyncMock())
    monkeypatch.setattr(tax_correction, "candidate", lambda *a: None)
    monkeypatch.setattr(sales_credit, "collect_support", AsyncMock(return_value=None))
    monkeypatch.setattr(credit_reallocation, "collect_support", AsyncMock(return_value=None))
    monkeypatch.setattr(resolution_assessment, "reference_provenance", AsyncMock(return_value=[]))
    monkeypatch.setattr(resolution_assessment, "assess", lambda *a, **kw: {})
    monkeypatch.setattr("app.services.audit_service.log_event", AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    monkeypatch.setattr(alignment, "matching_invoice_shape", lambda source, sections: shape)
    amend = AsyncMock(return_value={**p, "record_type": "salesorder", "record_id": "30"})
    monkeypatch.setattr(alignment, "prepare_matching", amend)
    result = await mod.execute_accounting_evidence({"case_id": str(case_id)})
    assert result["success"] is True
    assert amend.await_count == (1 if shape else 0)
    if shape:
        # The amendment became the case's candidate and met the same case-scope gate as any other.
        assert "outside_case_resolution_scope" in result["accounting_evidence"]["blockers"]


def test_an_order_adjusted_to_zero_is_discounted_to_zero():
    # The 12 "Adjusted to zero" orders: Solidus and the invoice at 0.00, the sales order at full price.
    d = inputs()
    d["source"]["total"] = "0"
    d["support"]["invoice"]["total"] = "0"
    p = alignment.build_matching_candidate(**d)
    assert p["proposed_fields"] == {"discountItem": {"id": "50"}, "discountRate": -100.0}
    assert p["expected_after"]["total"] == "0" and p["expected_after"]["discountTotal"] == "-100"
