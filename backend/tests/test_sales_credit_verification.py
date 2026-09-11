"""Exercise native readback including receipt loss, applications and GL proof."""

from contextlib import asynccontextmanager
from copy import deepcopy
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.services.transaction_ops.sales_credit import build_candidate, verify_after
from tests.test_commercial_credits import fixture
from tests.test_sales_credit import inputs


@pytest.mark.parametrize(
    "variant",
    [
        "valid",
        "unpaid",
        "partial_payment",
        "lost_receipt",
        "receipt_conflict",
        "missing_credit",
        "multiple_credits",
        "wrong_application",
        "remaining_credit",
        "wrong_period",
        "source_changed",
        "wrong_gl",
        "wrong_book",
        "invoice_gl_changed",
        "invoice_changed",
        "wrong_location",
        "wrong_department",
        "unexpected_class",
        "line_location_changed",
    ],
)
async def test_credit_readback_requires_exact_native_application_and_gl(variant):
    paid = "0" if variant == "unpaid" else "25" if variant == "partial_payment" else "101"
    data = inputs(paid=paid)
    proposal = build_candidate(**data)
    source = deepcopy(data["source"])
    _, _, applications, gl = fixture()
    invoice = {**proposal["before"], "amountPaid": str(Decimal(paid) + 5), "amountRemaining": str(101 - Decimal(paid))}
    if variant == "unpaid":
        applications["documents"].pop("40")
        applications["links"] = [row for row in applications["links"] if row["type"] == "CustCred"]
    elif variant == "partial_payment":
        deposit = applications["documents"]["40"]
        deposit.update(applied=paid, total=paid)
        deposit["applications"][0]["amount"] = paid
        next(row for row in applications["links"] if row["type"] == "DepAppl")["foreignamount"] = paid
    credit = applications["documents"].pop("30")
    credit.update(
        id="31",
        tranId="CM31",
        memo=proposal["proposed_fields"]["memo"],
        createdFrom={},
        entity={"id": "70"},
        postingPeriod={"id": "171"},
        tranDate=proposal["proposed_fields"]["tranDate"],
        location=proposal["proposed_fields"]["location"],
        department=proposal["proposed_fields"]["department"],
    )
    applications["documents"]["31"] = credit
    applications["links"][0]["nextdoc"] = "31"
    credit_gl = applications["credit_gl"]["30"]["rows"]
    receipt = {"id": "31"}
    if variant == "lost_receipt":
        receipt = None
    if variant == "receipt_conflict":
        receipt = {"id": "999"}
    if variant == "wrong_application":
        credit["applications"][0]["doc"]["id"] = "999"
    if variant == "remaining_credit":
        credit["unapplied"] = "1"
    if variant == "wrong_period":
        credit["postingPeriod"]["id"] = "172"
    if variant == "wrong_location":
        credit["location"] = {"id": "99"}
    if variant == "wrong_department":
        credit["department"] = {"id": "99"}
    if variant == "unexpected_class":
        credit["class"] = {"id": "99"}
    if variant == "line_location_changed":
        credit["line_items"][0]["location"] = {"id": "99"}
    if variant == "source_changed":
        source["total"] = "102"
    if variant == "wrong_gl":
        credit_gl[0]["account"] = "999"
    if variant == "wrong_book":
        for row in [*credit_gl, *gl["rows"]]:
            row["accountingbook"] = "2"
    if variant == "invoice_gl_changed":
        gl["rows"][1]["account"] = "999"
    payloads = {"/record/v1/invoice/20": invoice, "/record/v1/discountItem/50": applications["credit_items"]["50"]}
    for ident, doc in applications["documents"].items():
        raw = deepcopy(doc)
        raw["apply"] = {"items": raw.pop("applications")}
        if ident == "31":
            raw["item"] = {"items": raw.pop("line_items")}
        payloads[f"/record/v1/{'creditMemo' if ident == '31' else 'depositApplication'}/{ident}"] = raw

    class Reader:
        calls = 0
        invoice_reads = 0

        async def request(self, method, path, params=None, body=None):
            self.calls += 1
            assert method == "GET" or path == "/query/v1/suiteql", "Verification cannot send a financial write"
            if path in payloads:
                if path == "/record/v1/invoice/20":
                    self.invoice_reads += 1
                    if variant == "invoice_changed" and self.invoice_reads > 1:
                        return {**invoice, "lastModifiedDate": "changed"}
                return deepcopy(payloads[path])
            q = body["q"]
            if "externalid=" in q:
                assert proposal["proposed_fields"]["externalId"] in q
                rows = [] if variant == "missing_credit" else [{"id": "31"}]
                if variant == "multiple_credits":
                    rows.append({"id": "32"})
            elif "nexttransactionlinelink" in q:
                rows = applications["links"]
            elif "tal.transaction=20" in q:
                rows = gl["rows"]
            else:
                assert "tal.transaction=31" in q
                rows = credit_gl
            return {"items": deepcopy(rows), "count": len(rows), "totalResults": len(rows), "hasMore": False}

    reader = Reader()

    @asynccontextmanager
    async def connected(*args, **kwargs):
        assert args[3] == "123"
        yield reader

    with (
        patch("app.services.transaction_ops.netsuite_reader.authenticated_reader", connected),
        patch("app.services.transaction_ops.commercial_credits.authenticated_reader", connected),
        patch("app.services.transaction_ops.tax_correction.refresh_source", AsyncMock(return_value=source)),
    ):
        result = await verify_after(None, "tenant", proposal, receipt)
    assert result["status"] == (
        "verified" if variant in {"valid", "lost_receipt", "unpaid", "partial_payment"} else "needs_review"
    ), (
        result.get("reason"),
        result.get("resolution"),
        result.get("evidence", {}).get("blockers"),
        result.get("evidence", {}).get("sections", {}).get("invoice_applications", {}).get("documents", {}).get("31"),
    )
    assert result["retry_allowed"] is False
    assert reader.calls <= 9
    if result["status"] == "verified":
        assert result["credit_memo_id"] == "31"
        assert result["cash_settlement"] == "not_verified"
        assert Decimal(result["resolution"]["invoice_remaining"]) == 101 - Decimal(paid)
