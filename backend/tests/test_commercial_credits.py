from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import patch

import pytest

from app.services.transaction_ops.commercial_credits import (
    collect_commercial_credits,
    source_adjustment_basis,
    verify_applied_credit,
)
from app.services.transaction_ops.record_links import correct_record_links, evidence_record_links


def fixture():
    source = dict(
        id="10",
        number="R100",
        currency="USD",
        state="complete",
        completed_at="2026-09-01",
        requires_review=False,
        included_tax_total="0",
        additional_tax_total="6",
        tax_total="6",
        ship_total="0",
        item_total="100",
        total="101",
        adjustment_total="1",
        adjustments=[
            dict(
                id="11",
                amount="-5",
                finalized=True,
                adjustable_type="Spree::Order",
                adjustable_id="10",
                label="Reseller Adjustment 5%",
            )
        ],
    )
    invoice = dict(
        id="20",
        record_type="invoice",
        total="106",
        taxTotal="6",
        amountPaid="106",
        amountRemaining="0",
        currency={"id": "1"},
        subsidiary={"id": "1"},
        exchangeRate="1",
        lastModifiedDate="2026-09-10T00:00:00Z",
    )

    def doc(ident, amount):
        return dict(
            id=ident,
            total=amount,
            applied=amount,
            unapplied="0",
            currency={"id": "1"},
            subsidiary={"id": "1"},
            exchangeRate="1",
            applications_complete=True,
            applications=[dict(doc={"id": "20"}, apply=True, amount=amount)],
        )

    credit = doc("30", "5")
    credit.update(
        createdFrom={"id": "20"},
        taxTotal="0",
        memo="R100 Reseller Discount",
        lines_complete=True,
        line_items=[dict(amount="5", isTaxable=False, item={"id": "50"}, itemType={"id": "Discount"})],
        tranId="CM30",
        record_type="creditmemo",
    )
    deposit = doc("40", "101")
    deposit.update(record_type="depositapplication")
    gl = dict(
        complete=True,
        rows=[
            dict(account="100", accountingbook="1", debit="106"),
            dict(account="600", accountingbook="1", credit="100"),
            dict(account="700", accountingbook="1", credit="6"),
        ],
    )
    credit_gl = dict(
        complete=True,
        rows=[dict(account="100", accountingbook="1", credit="5"), dict(account="500", accountingbook="1", debit="5")],
    )

    def link(ident, kind, amount):
        return dict(
            previousdoc="20",
            previousline="0",
            nextdoc=ident,
            nextline="0",
            linktype="Payment",
            foreignamount=amount,
            type=kind,
            currency="1",
        )

    applications = dict(
        complete=True,
        links=[link("30", "CustCred", "5"), link("40", "DepAppl", "101")],
        documents={"30": credit, "40": deposit},
        credit_gl={"30": credit_gl},
        credit_items={"50": {"id": "50", "account": {"id": "500"}, "isInactive": False}},
    )
    return source, invoice, applications, gl


def test_finalized_order_adjustment_explains_header_and_existing_credit():
    source, invoice, applications, gl = fixture()
    basis = source_adjustment_basis(source)
    assert basis["status"] == "header_explained_by_order_adjustments"
    result = verify_applied_credit(basis, invoice, applications, gl)
    assert result["status"] == "existing_credit_verified" and result["net_invoice_total"] == "101"
    assert result["bank_processor_clearance"] == "not_verified" and result["sales_adjustment_account"] == "500"


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s["adjustments"][0].update(finalized=False),
        lambda s: s["adjustments"][0].update(eligible=False),
        lambda s: s["adjustments"][0].update(adjustable_id="999"),
        lambda s: s.update(total="101.01"),
        lambda s: s.update(ship_total="2"),
        lambda s: s.update(included_tax_total="6"),
        lambda s: s.update(adjustment_total="NaN"),
        lambda s: s.pop("adjustments"),
    ],
)
def test_invalid_or_other_source_treatments_are_not_inferred(change):
    source, *_ = fixture()
    change(source)
    assert source_adjustment_basis(source) is None


@pytest.mark.parametrize(
    "change",
    [
        lambda i, a, g: a.update(complete=False),
        lambda i, a, g: a["links"].append(deepcopy(a["links"][0])),
        lambda i, a, g: a["documents"]["30"].update(taxTotal="0.01"),
        lambda i, a, g: a["documents"]["30"].update(total="4.99"),
        lambda i, a, g: a["documents"]["30"].update(unapplied="1"),
        lambda i, a, g: a["documents"]["30"]["applications"][0].update(doc={"id": "999"}),
        lambda i, a, g: a["documents"]["30"].update(applications_complete=False),
        lambda i, a, g: a["documents"]["30"].update(currency={"id": "2"}),
        lambda i, a, g: a["documents"]["30"].update(subsidiary={"id": "2"}),
        lambda i, a, g: a["documents"]["30"].update(createdFrom={"id": "999"}),
        lambda i, a, g: a["documents"]["30"]["line_items"][0].update(isTaxable=True),
        lambda i, a, g: a["credit_gl"]["30"]["rows"][1].update(debit="4.99"),
        lambda i, a, g: a["credit_gl"]["30"].update(complete=False),
        lambda i, a, g: a["credit_gl"]["30"]["rows"][1].update(accountingbook="2"),
        lambda i, a, g: i.update(amountRemaining="5"),
        lambda i, a, g: a["links"].pop(0),
        lambda i, a, g: a["links"][1].update(type="CustPymt"),
        lambda i, a, g: a["credit_items"]["50"].update(isInactive=True),
        lambda i, a, g: a["credit_items"]["50"].update(account={"id": "700"}),
        lambda i, a, g: g["rows"].pop(),
    ],
)
def test_partial_conflicting_or_incomplete_credit_never_clears_case(change):
    source, invoice, applications, gl = fixture()
    change(invoice, applications, gl)
    assert verify_applied_credit(source_adjustment_basis(source), invoice, applications, gl) is None


@pytest.mark.parametrize("changed", [False, True])
async def test_collector_reads_applications_and_rechecks_invoice_version(changed):
    source, invoice, applications, gl = fixture()
    payloads = {}
    for ident, doc in applications["documents"].items():
        raw = deepcopy(doc)
        raw["apply"] = {"items": raw.pop("applications")}
        if ident == "30":
            raw["item"] = {"items": raw.pop("line_items")}
        payloads["/record/v1/" + ("creditMemo" if ident == "30" else "depositApplication") + "/" + ident] = raw
    current = deepcopy(invoice)
    if changed:
        current["lastModifiedDate"] = "2026-09-11T00:00:00Z"
    payloads["/record/v1/invoice/20"] = current
    payloads["/record/v1/discountItem/50"] = applications["credit_items"]["50"]

    class Reader:
        calls = 0

        async def request(self, method, path, params=None, body=None):
            self.calls += 1
            if path in payloads:
                return payloads[path]
            rows = (
                applications["links"]
                if "nexttransactionlinelink" in body["q"]
                else applications["credit_gl"]["30"]["rows"]
            )
            return {"items": rows, "count": len(rows), "totalResults": len(rows), "hasMore": False}

    reader = Reader()

    @asynccontextmanager
    async def connected(*args, **kwargs):
        yield reader

    review = {"scope": {"subsidiary_id": "1", "netsuite_account_id": "123"}, "netsuite_connection_id": "connection"}
    report = {"order_reference": "R100", "source": {"record_id": "10", "currency": "USD"}}
    evidence = {
        "verified_connection_scope": {"account_id": "123"},
        "sections": {"posting_documents": [invoice], "gl": {"20": gl}},
        "blockers": [],
        "assessment": {},
    }
    with patch("app.services.transaction_ops.commercial_credits.authenticated_reader", new=connected):
        await collect_commercial_credits(None, None, review, report, source, evidence)
    assert reader.calls == 6 and ("commercial_credit_resolution" in evidence) is not changed
    if changed:
        assert "invoice_changed_during_application_read" in evidence["blockers"][-1]


def test_verified_links_replace_wrong_environment_and_credit_path_not_unknown_ids():
    _, invoice, applications, _ = fixture()
    evidence = {
        "verified_connection_scope": {"account_id": "123"},
        "sections": {"posting_documents": [invoice], "invoice_applications": applications},
    }
    links = evidence_record_links(evidence)
    text = "[Invoice](https://123-sb1.app.netsuite.com/app/accounting/transactions/custinvc.nl?id=20) [Credit](https://123-sb1.app.netsuite.com/app/accounting/transactions/credmemo.nl?id=30) [Unknown](https://123.app.netsuite.com/app/accounting/transactions/custinvc.nl?id=99)"
    fixed = correct_record_links(text, [{"record_links": links}])
    assert "sb1" not in fixed and "credmemo.nl" not in fixed and "id=99" not in fixed
    assert "custcred.nl?id=30" in fixed and "Unknown" in fixed
    assert evidence_record_links({"sections": evidence["sections"]}) == []


def test_links_survive_tool_log_and_final_message_coercion():
    import json

    from app.services.chat.orchestrator import _coerce_assistant_content
    from app.services.chat.tool_call_results import build_tool_call_log_entry

    _, invoice, _, _ = fixture()
    links = evidence_record_links(
        {"verified_connection_scope": {"account_id": "123"}, "sections": {"posting_documents": [invoice]}}
    )
    entry = build_tool_call_log_entry(
        step=0,
        tool_name="transaction_ops_accounting_evidence",
        params={"case_id": "case"},
        duration_ms=1,
        result_str=json.dumps({"success": True, "accounting_evidence": {"record_links": links}}),
    )
    result = _coerce_assistant_content(
        "[Invoice](https://123-sb1.app.netsuite.com/app/accounting/transactions/custinvc.nl?id=20)",
        None,
        tool_calls=[entry],
    )
    assert result == "[Invoice](https://123.app.netsuite.com/app/accounting/transactions/custinvc.nl?id=20)"


def recon_fixture():
    from tests.test_order_balance_reconciliation import evidence

    source, target, config, refunds = evidence()
    row, invoice, applications, gl = fixture()
    row["business_entity"] = "US"
    source["orders"] = [row]
    config.update(
        netsuite_account_id="123",
        subsidiary_id="1",
        netsuite_connection_id="conn",
        mapping_json={"business_entity_subsidiaries": {"US": "1"}},
    )
    target["scope"] = {"account_id": "123", "subsidiary_id": "1"}
    target["orders"][0].update(
        order_reference="R100",
        currency_metadata={"id": "1", "symbol": "USD", "currencyPrecision": 2},
        header={"id": "5", "subsidiary": {"id": "1"}, "currency": {"id": "1"}, "total": "106", "taxTotal": "6"},
    )
    invoice["createdFrom"] = {"id": "5"}
    for r in refunds.values():
        r.update(order_reference="R100", currency="USD")
    proof = {
        "verified_connection_scope": {"account_id": "123", "connection_id": "conn"},
        "subsidiary_id": "1",
        "order_record_id": "5",
        "sections": {"posting_documents": [invoice], "invoice_applications": applications, "gl": {"20": gl}},
    }
    target["commercial_credit_evidence"] = proof
    return source, target, config, refunds


def test_reconciliation_preserves_original_variance_and_matches_after_verified_credit():
    from app.services.transaction_ops.order_reconciliation import reconcile_order

    source, target, config, refunds = recon_fixture()
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched"
    assert result["original_amounts"]["order_total"]["delta"] == "-5.00"
    assert result["amounts"]["order_total"] == {"source": "101.00", "target": "101.00", "delta": "0.00"}
    assert result["adjustments"][0]["credit_memo_id"] == "30"
    assert result["adjustments"][0]["verification_evidence"]["sections"]["invoice_applications"]["credit_gl"]


@pytest.mark.parametrize(
    "change",
    [
        lambda s, t, c, r: t["commercial_credit_evidence"]["verified_connection_scope"].update(account_id="123-sb1"),
        lambda s, t, c, r: t["commercial_credit_evidence"]["verified_connection_scope"].update(connection_id="other"),
        lambda s, t, c, r: t["commercial_credit_evidence"].update(subsidiary_id="2"),
        lambda s, t, c, r: t["commercial_credit_evidence"].update(order_record_id="99"),
        lambda s, t, c, r: t["commercial_credit_evidence"]["sections"]["posting_documents"][0].update(
            createdFrom={"id": "99"}
        ),
        lambda s, t, c, r: t["commercial_credit_evidence"]["sections"]["posting_documents"][0].update(
            subsidiary={"id": "2"}
        ),
        lambda s, t, c, r: t["commercial_credit_evidence"]["sections"]["posting_documents"][0].update(
            currency={"id": "2"}
        ),
        lambda s, t, c, r: t.pop("commercial_credit_evidence"),
    ],
)
def test_wrong_scope_or_missing_proof_retains_the_real_variance(change):
    from app.services.transaction_ops.order_reconciliation import reconcile_order

    s, t, c, r = recon_fixture()
    change(s, t, c, r)
    result = reconcile_order(s, t, c, refunds=r)
    assert result["status"] == "difference" and result["amounts"]["order_total"]["delta"] == "-5.00"


def test_matching_credit_does_not_hide_a_refund_variance():
    from app.services.transaction_ops.order_reconciliation import reconcile_order

    s, t, c, r = recon_fixture()
    r["source"]["amount"] = "0.01"
    result = reconcile_order(s, t, c, refunds=r)
    assert result["status"] == "difference" and result["amounts"]["refunds"]["delta"] == "0.01"
