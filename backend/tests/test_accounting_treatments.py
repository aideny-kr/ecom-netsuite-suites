from copy import deepcopy

import pytest

from app.services.transaction_ops.accounting_treatments import investigation_batches, treatment_batches
from app.services.transaction_ops.sales_credit import build_candidate
from tests.test_accounting_group import group_fixture
from tests.test_sales_credit import inputs


def same_scope_members():
    group, _ = group_fixture(2)
    members = group["accounting_group"]["members"]
    first, second = [m["card"]["accounting_review"] for m in members]
    second.update({key: first[key] for key in ("connector_id", "connection_id")})
    return members


def test_same_treatment_groups_distinct_orders_without_summing_gross_and_tax():
    members = same_scope_members()
    batches = treatment_batches(members)
    assert len(batches) == 1
    assert batches[0]["case_ids"] == [m["case_id"] for m in members]
    assert batches[0]["confirmation_ids"] == [m["confirmation_id"] for m in members]
    assert "total" not in batches[0]


@pytest.mark.parametrize("field", ["currency", "book", "account", "subsidiary", "period", "tax_item", "connection"])
def test_similar_deltas_with_different_accounting_treatments_stay_separate(field):
    members = same_scope_members()
    p = members[1]["card"]["accounting_review"]
    if field == "currency":
        p["source"]["currency"] = "CAD"
    elif field == "book":
        p["accounting_book"] = "2"
    elif field == "account":
        p["tax_account"] = "999"
    elif field == "subsidiary":
        p["scope"]["subsidiary_id"] = "99"
    elif field == "period":
        p["period"]["id"] = "999"
    elif field == "tax_item":
        p["tax_item"]["id"] = "999"
    else:
        p["connection_id"] = "other"
    assert len(treatment_batches(members)) == 2


def test_credit_application_and_tax_changes_are_distinct_and_keep_remaining_ar():
    members = same_scope_members()[:1]
    proposal = build_candidate(**inputs(paid="25"))
    assert proposal is not None
    members.append(
        {"case_id": "credit-case", "confirmation_id": "credit-card", "card": {"accounting_review": proposal}}
    )
    before = deepcopy(members)
    batches = treatment_batches(members)
    assert {b["treatment"]["kind"] for b in batches} == {"invoice_tax", "sales_adjustment_credit"}
    assert members == before
    assert proposal["expected_after"]["invoice_remaining"] == "76.00"


def test_unresolved_refund_cases_share_investigation_without_financial_approval():
    route = {"code": "reconcile_refund_chain", "next_step": "Trace credits, refunds and applications."}
    members = [{"case_id": f"case-{i}", "investigation_routes": [route]} for i in range(3)]
    result = investigation_batches(members)
    assert len(result) == 1 and len(result[0]["case_ids"]) == 3
    assert result[0]["executable"] is False
    assert treatment_batches(members) == []


def test_invoice_discount_is_a_separate_treatment_from_credit_and_tax():
    from tests.test_invoice_discount import unpaid_inputs

    members = same_scope_members()[:1]
    for kind, data in (("discount", unpaid_inputs()), ("credit", inputs(paid="25"))):
        members.append(
            {"case_id": kind, "confirmation_id": kind, "card": {"accounting_review": build_candidate(**data)}}
        )
    assert {b["treatment"]["kind"] for b in treatment_batches(members)} == {
        "invoice_tax",
        "invoice_sales_adjustment",
        "sales_adjustment_credit",
    }
