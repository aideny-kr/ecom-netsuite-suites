"""Financial investigation routing must preserve pennies and existing credits."""

from copy import deepcopy

import pytest

from app.services.transaction_ops.resolution_guidance import investigation_guidance


def report(*, gross=("170.00", "169.99", "0.01"), tax=("27.14", "27.13", "0.01"), refunds=("0", "0", "0")):
    return {
        "balance": {
            "status": "difference",
            "currency": "EUR",
            "target_currency": "EUR",
            "missing_metrics": [],
            "amounts": {
                name: dict(zip(("source", "target", "delta"), values))
                for name, values in (("order_total", gross), ("tax", tax), ("refunds", refunds))
            },
        },
        "targets": [{"status": "fulfilled"}],
    }


def codes(result):
    return [route["code"] for route in result["routes"]]


def test_billed_penny_case_retains_separate_tax_and_gross_routes_without_mutation():
    evidence = report()
    before = deepcopy(evidence)
    result = investigation_guidance(evidence)
    assert codes(result) == ["inspect_posted_documents", "investigate_tax_allocation", "investigate_order_total"]
    assert result["executable"] is False
    assert "do not add the tax variance" in result["routes"][-1]["next_step"]
    assert evidence == before


def test_fully_refunded_vat_credit_with_two_cent_tax_residual_does_not_suggest_more_cash():
    evidence = report(gross=("2046.68", "2046.68", "0"), tax=("0", "0.02", "-0.02"), refunds=("409.32", "409.32", "0"))
    evidence["balance"]["adjustments"] = [{"kind": "tax_reversal", "amount": "409.32"}]
    result = investigation_guidance(evidence)
    assert codes(result) == ["inspect_posted_documents", "investigate_tax_after_credit"]
    assert "without crediting the same amount again" in result["routes"][-1]["next_step"]


@pytest.mark.parametrize("delta,source,target", [("-0.01", "2901.38", "2901.39"), ("0.01", "2901.39", "2901.38")])
def test_refund_pennies_require_request_credit_application_and_processor_investigation(delta, source, target):
    result = investigation_guidance(
        report(gross=("100", "100", "0"), tax=("0", "0", "0"), refunds=(source, target, delta))
    )
    assert codes(result) == ["inspect_posted_documents", "reconcile_refund_chain"]
    assert "does not authorize issuing cash again" in result["routes"][-1]["next_step"]


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", True, 0.01, "1e999", "0.02"])
def test_invalid_or_inconsistent_delta_never_generates_correction_guidance(value):
    evidence = report()
    evidence["balance"]["amounts"]["tax"]["delta"] = value
    assert codes(investigation_guidance(evidence)) == ["collect_comparison_evidence"]


@pytest.mark.parametrize(
    "status,expected",
    [
        ("ambiguous", "establish_order_identity"),
        ("currency_mismatch", "establish_order_identity"),
        ("missing_in_netsuite", "investigate_missing_order"),
        ("incomplete", "collect_comparison_evidence"),
        ("unknown", "collect_comparison_evidence"),
    ],
)
def test_identity_and_incomplete_states_take_priority_over_amounts(status, expected):
    evidence = report()
    evidence["balance"]["status"] = status
    assert codes(investigation_guidance(evidence)) == [expected]


def test_matched_status_cannot_hide_a_penny_or_missing_refund_amount():
    evidence = report()
    evidence["balance"]["status"] = "matched"
    assert codes(investigation_guidance(evidence)) == ["collect_comparison_evidence"]
    evidence = report(gross=("0", "0", "0"), tax=("0", "0", "0"))
    evidence["balance"]["status"] = "matched"
    assert codes(investigation_guidance(evidence)) == ["no_financial_change"]
    evidence["balance"]["amounts"]["refunds"]["source"] = None
    assert codes(investigation_guidance(evidence)) == ["collect_comparison_evidence"]


def test_unknown_lifecycle_is_not_assumed_to_be_editable():
    evidence = report()
    evidence["targets"] = [{"status": "unknown"}]
    result = investigation_guidance(evidence)
    assert codes(result)[0] == "verify_native_lifecycle"
    assert "new human approval" in result["execution_requirements"]
    assert "persist audit before execution" in result["execution_requirements"]


def test_currency_and_missing_metrics_must_be_known():
    evidence = report()
    evidence["balance"]["target_currency"] = "USD"
    assert codes(investigation_guidance(evidence)) == ["collect_comparison_evidence"]
    evidence = report()
    evidence["balance"]["missing_metrics"] = ["refunds"]
    assert codes(investigation_guidance(evidence)) == ["collect_comparison_evidence"]
