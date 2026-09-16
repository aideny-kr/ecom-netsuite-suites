from copy import deepcopy

import pytest

from app.services.transaction_ops.resolution_assessment import assess


@pytest.mark.parametrize("tax_only", [False, True])
def test_real_credit_and_order_intents_publish_through_the_assessment_fingerprint(tax_only):
    import json

    from app.services.transaction_ops.credit_reallocation import build_intent, solution_summary
    from app.services.transaction_ops.source_line_alignment import build_intent as align
    from tests.test_credit_reallocation import fixture

    source, review, e, support = fixture()
    if tax_only:
        source.update(total="1720", item_total="1600", payment_total="1720")
        source["line_items"][0]["price"] = "1600"
        support["credit"].update(total="40", subtotal="40", applied="40")
        support["credit"]["line_evidence"]["lines"][0].update(rate="40", amount="40")
        support["refund"]["total"] = "40"
        support["refund_graph"]["amount"] = "40"
        support["refund_graph"]["request_links"][0]["amount"] = "40"
        support["credit_gl"]["rows"][0]["debit"] = "40"
        support["credit_gl"]["rows"][1]["credit"] = "40"
    intent = build_intent("tenant", "case", source, review, e, support)
    assert intent
    e["sections"]["sales_order"]["line_evidence"]["lines"][0].update(
        lineUniqueKey="120", custcol_fw_original_ecom_sku="MEM64"
    )
    order = align(source, e, intent)
    assert order
    e["resolution_intents"] = [solution_summary(intent), order]
    # Exercise the actual JSON boundary before evidence publication. The strict
    # financial digest must accept the plans without rounding or permitting floats.
    result = assess(json.loads(json.dumps(e)), {}, review)
    assert result["status"] == "solution_identified"
    assert result["planned_steps"] == e["resolution_intents"]
    assert len(result["assessment_fingerprint"]) == 64
    assert result["execution_capabilities"]["exact_proposal_available"] is False
    assert result["financial_write_authorized"] is False


def test_known_treatment_is_a_solution_even_when_no_native_executor_is_configured():
    e = evidence()
    e["resolution_intents"] = [{"kind": "credit_tax_reallocation", "approval_basis": "Reallocate existing credit."}]
    result = assess(e, {}, {})
    assert result["status"] == "solution_identified"
    assert result["selected_treatment"] == "credit_tax_reallocation"
    assert result["execution_capabilities"]["exact_proposal_available"] is False
    assert result["execution_capabilities"]["native_amendment_configured"] is False
    assert result["financial_write_authorized"] is False


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


def test_existing_credit_observation_does_not_infer_tax_or_full_allocation():
    e = evidence()
    e["sections"]["related_refund_documents"] = {
        "complete": False,
        "documents": [
            {"record_type": "creditmemo", "id": "20", "total": "440", "applied": "440", "unapplied": "0"},
            {"record_type": "customerrefund", "id": "21", "total": "440"},
        ],
    }
    e["line_comparison"] = {"changes": [{"source_line_id": "55", "unit_price_delta": "-400"}], "unverified": []}
    result = assess(e, {}, {})
    assert result["facts"]["related_credit_refund_records"][0]["id"] == "20"
    assert "taxTotal" not in result["facts"]["related_credit_refund_records"][0]
    assert result["facts"]["related_refund_graph_complete"] is False
    assert result["execution_capabilities"]["exact_proposal_available"] is False
    assert result["facts"]["line_comparison"] == e["line_comparison"]
