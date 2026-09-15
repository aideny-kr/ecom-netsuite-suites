from copy import deepcopy
from decimal import Decimal

import pytest

from app.services.transaction_ops.accounting_projection import comparison_fingerprint, project_report
from app.services.transaction_ops.posting_balance import repriced_credit_balance
from tests.test_credit_reallocation import fixture


def inputs():
    source, review, evidence, support = fixture()
    support["refund_graph"]["request_links"][0].update(source_refund_id="50", payment_number="PAY-1")
    report = {
        "refund_evidence": {
            "source": {
                "complete": True,
                "events_complete": True,
                "amount": "440",
                "currency": "USD",
                "order_reference": "R123",
                "refund_count": 1,
                "events": [{"id": "50", "payment_number": "PAY-1", "amount": "440"}],
            }
        }
    }
    return source, review, evidence, support, report


def test_net_and_tax_reclassification_has_zero_gross_variance_and_separate_order_alignment():
    args = inputs()
    before = deepcopy(args)
    result = repriced_credit_balance(*args)
    assert result is not None
    assert {k: Decimal(v["delta"]) for k, v in result["amounts"].items()} == {
        "net": Decimal("40"),
        "tax": Decimal("-40"),
        "order_total": 0,
        "refunds": 0,
    }
    assert result["status"] == "difference"
    assert Decimal(result["sales_order_alignment"]["amounts"]["order_total"]["delta"]) == -440
    assert args == before


def test_corrected_credit_matches_posting_without_inferring_original_order_error():
    args = inputs()
    support = args[3]
    support["period"].update(closed=True, arLocked=True, allLocked=True)
    support["credit"].update(subtotal="400", taxTotal="40", isTaxable=True)
    support["credit_gl"]["rows"][0]["debit"] = "400"
    support["credit_gl"]["rows"].append({"account": "13", "accountingbook": "1", "debit": "40"})
    result = repriced_credit_balance(*args)
    assert result["status"] == "matched"
    assert result["sales_order_alignment"]["status"] == "observed_difference"


def test_reported_433_80_credit_preserves_gross_and_exposes_33_80_tax_reclassification():
    source, review, evidence, support, report = args = inputs()
    source.update(total="3963.84", tax_total="308.84", item_total="3655.00", payment_total="3963.84")
    source["line_items"][0]["price"] = "3655.00"
    source["line_items"][0]["adjustments"][0]["amount"] = "308.84"
    for doc in [evidence["sections"]["sales_order"], *evidence["sections"]["posting_documents"], support["invoice"]]:
        doc.update(total="4397.64", taxTotal="342.64", subtotal="4055.00", amountPaid="4397.64")
        doc["line_evidence"]["lines"][0].update(rate="4055.00", amount="4055.00", custcol_fw_vat_amount="342.64")
    support["credit"].update(total="433.80", subtotal="433.80", applied="433.80")
    support["refund"]["total"] = "433.80"
    support["refund_graph"]["amount"] = "433.80"
    support["refund_graph"]["request_links"][0]["amount"] = "433.80"
    report["refund_evidence"]["source"]["amount"] = "433.80"
    report["refund_evidence"]["source"]["events"][0]["amount"] = "433.80"
    support["credit_gl"]["rows"][0]["debit"] = "433.80"
    support["credit_gl"]["rows"][1]["credit"] = "433.80"
    for row in support["invoice_gl"]["rows"]:
        if row["account"] == "13":
            row["credit"] = "342.64"
        if row["account"] == "11":
            row["debit"] = "4397.64"
        if row["account"] == "14":
            row["credit"] = "4055.00"
    result = repriced_credit_balance(*args)
    assert result["amounts"]["net"] == {"source": "3655.00", "target": "3621.20", "delta": "33.80"}
    assert result["amounts"]["tax"]["delta"] == "-33.80"
    assert result["amounts"]["order_total"]["delta"] == "0.00"


async def test_audited_projection_is_tenant_scoped_and_new_observations_invalidate_old_explanations(
    db, admin_user, admin_user_b
):
    from uuid import uuid4

    from app.services.audit_service import log_event
    from app.services.transaction_ops.accounting_projection import project_rows

    actor, _ = admin_user
    other, _ = admin_user_b
    cid = str(uuid4())
    report = {"case_id": cid, "balance": {"status": "difference", "amounts": {}}}
    posting = repriced_credit_balance(*inputs())
    payload = {"evidence": {"comparison_fingerprint": comparison_fingerprint(report), "posting_balance": posting}}

    async def save(user, value):
        from datetime import datetime, timezone

        value = {"evidence": {**value["evidence"], "completed_at": datetime.now(timezone.utc).isoformat()}}
        return await log_event(
            db,
            user.tenant_id,
            category="transaction_ops",
            action="accounting.evidence.observed",
            actor_id=user.id,
            resource_type="transaction_case",
            resource_id=cid,
            payload=value,
        )

    own = await save(actor, payload)
    await save(other, {"evidence": {**payload["evidence"], "posting_balance": {**posting, "currency": "OTHER"}}})
    projected = (await project_rows(db, actor.tenant_id, [{"report_json": report}]))[0]["report_json"]
    assert projected["balance"]["posting_reconciliation"]["audit_id"] == str(own.id)
    assert projected["balance"]["posting_reconciliation"]["currency"] == "USD"
    await save(actor, {"evidence": {"comparison_fingerprint": comparison_fingerprint(report)}})
    assert (await project_rows(db, actor.tenant_id, [{"report_json": report}]))[0]["report_json"] == report


@pytest.mark.parametrize(
    "change",
    [
        "source_unchanged",
        "ambiguous_graph",
        "wrong_refund",
        "partial_source",
        "wrong_currency",
        "wrong_subsidiary",
        "different_book",
        "unbalanced",
        "unexplained_tax",
        "wrong_order",
        "incomplete_gl",
    ],
)
def test_does_not_net_unproven_or_unrelated_refunds(change):
    source, review, evidence, support, report = args = inputs()
    if change == "source_unchanged":
        source.update(total="1760", tax_total="160", item_total="1600")
        source["line_items"][0]["price"] = "1600"
    elif change == "ambiguous_graph":
        support["refund_graph"]["refund_count"] = 2
    elif change == "wrong_refund":
        support["refund_graph"]["request_links"][0]["source_refund_id"] = "51"
    elif change == "partial_source":
        report["refund_evidence"]["source"]["events_complete"] = False
    elif change == "wrong_currency":
        support["credit"]["currency"]["id"] = "2"
    elif change == "wrong_subsidiary":
        support["refund"]["subsidiary"]["id"] = "2"
    elif change == "different_book":
        support["credit_gl"]["rows"][0]["accountingbook"] = "2"
    elif change == "unbalanced":
        support["credit_gl"]["rows"][0]["debit"] = "439.99"
    elif change == "unexplained_tax":
        support["credit"]["taxTotal"] = "0.01"
    elif change == "wrong_order":
        support["credit"]["custbody_fw_order_number"] = "R999"
    elif change == "incomplete_gl":
        support["credit_gl"]["complete"] = False
    assert repriced_credit_balance(*args) is None


def test_projection_requires_exact_report_and_preserves_original_amounts_and_status():
    report = {"balance": {"status": "difference", "amounts": {"order_total": {"delta": "-440"}}}}
    observation = {
        "id": "audit",
        "fingerprint": comparison_fingerprint(report),
        "posting": repriced_credit_balance(*inputs()),
    }
    projected = project_report({**report, "case_id": "case"}, observation)
    assert projected["balance"]["amounts"] == report["balance"]["amounts"]
    assert projected["balance"]["status"] == "difference"
    assert projected["balance"]["posting_reconciliation"]["audit_id"] == "audit"
    changed = deepcopy(report)
    changed["balance"]["amounts"]["order_total"]["delta"] = "-441"
    assert project_report(changed, observation) is changed
    assert "posting_reconciliation" not in report["balance"]
