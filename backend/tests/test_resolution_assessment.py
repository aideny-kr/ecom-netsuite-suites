from copy import deepcopy

import pytest

from app.services.transaction_ops.resolution_assessment import assess


def evidence(paid="0", remaining="100", complete=True, links=None):
    return {
        "sections": {
            "posting_documents": [{"id": "10", "total": "100", "amountPaid": paid, "amountRemaining": remaining}],
            "invoice_applications": {"complete": complete, "links": [] if links is None else links},
        },
        "assessment": {"root_cause": "not_verified"},
        "blockers": ["tax_regime_not_verified"],
    }


@pytest.mark.parametrize(
    "paid,remaining,complete,links,state",
    [
        ("0", "100", True, [], "fully_unpaid_no_applications"),
        ("0", "100", False, [], "not_established"),
        ("0", "100", True, [{"id": "credit"}], "not_established"),
        (None, "100", True, [], "not_established"),
        ("NaN", "100", True, [], "not_established"),
        ("25", "75", True, [], "partially_paid"),
        ("100", "0", True, [], "payment_observed_no_remaining_receivable"),
    ],
)
def test_payment_and_application_facts_are_not_assumed(paid, remaining, complete, links, state):
    result = assess(evidence(paid, remaining, complete, links), {}, {})
    assert result["facts"]["payment_state"] == state
    assert result["selected_treatment"] is None
    assert result["status"] == "investigation_required"
    assert result["financial_write_authorized"] is False


def test_only_adapter_candidate_establishes_a_supported_proposal():
    e = evidence()
    saved = deepcopy(e)
    candidate = {
        "kind": "invoice_sales_adjustment",
        "approval_basis": "Exact finalized source adjustment",
        "profile": {"id": "configured"},
    }
    result = assess(e, {"balance": {"status": "difference"}}, {}, candidate, references=[{"audit_id": "reference"}])
    assert e == saved
    assert result["selected_treatment"] == "invoice_sales_adjustment"
    assert result["facts"]["root_cause"] == "not_verified"
    assert result["unresolved_evidence"] == ["tax_regime_not_verified"]
    assert [x["kind"] for x in result["alternatives"] if x["status"] == "supported_exact_proposal"] == [
        "invoice_sales_adjustment"
    ]
    assert result["financial_write_authorized"] is False
    assert result["references"] == [{"audit_id": "reference"}]
    assert result["assessment_fingerprint"] != assess(e, {}, {}, candidate)["assessment_fingerprint"]


def test_existing_correction_is_not_automatically_a_resolved_case():
    e = evidence()
    e["commercial_credit_resolution"] = {"credit_id": "20"}
    result = assess(e, {}, {})
    assert result["existing_correction_observed"]
    assert result["status"] == "investigation_required"
    assert "full_order_tax_refund_reconciliation" in result["verification_required"]


def test_multiple_invoices_do_not_collapse_to_one_payment_state():
    e = evidence()
    e["sections"]["posting_documents"].append({"id": "11"})
    assert assess(e, {}, {})["facts"]["payment_state"] == "not_established"
