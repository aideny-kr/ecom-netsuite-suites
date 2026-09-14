"""Final source tax assessments are an explicit alternative to rate recalculation."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.services.transaction_ops.normalization import SourceTaxRule, normalize_framework_order
from tests.test_transaction_ops_legacy_tax import config
from tests.test_transaction_ops_normalization import evidence


def assessment_rule(**changes):
    return {"calculation": "source_assessment", "included": True, "netsuite_tax_id": "15", **changes}


def assessment_case():
    raw = evidence()
    raw["orders"][0]["line_items"][0]["adjustments"][0]["updated_at"] = "2026-09-04T09:00:00Z"
    return raw, config("line_tax_amount", tax_rules={"7": assessment_rule()})


def normalized(raw, policy):
    return normalize_framework_order(raw, mapping=policy, account_id="frame.work", subsidiary_id="1")


def test_assessment_rule_is_explicit_and_cannot_claim_an_invented_rate():
    rule = SourceTaxRule.model_validate(assessment_rule())
    assert rule.calculation == "source_assessment" and rule.rate is None and rule.rounding is None
    for extra in ({"rate": "0.2"}, {"rounding": "half_up"}):
        with pytest.raises(ValidationError):
            SourceTaxRule.model_validate(assessment_rule(**extra))
    with pytest.raises(ValidationError):
        SourceTaxRule.model_validate({"included": True, "netsuite_tax_id": "15"})


def test_finalized_assessment_retains_adjustment_identity_clock_and_exact_amounts():
    raw, policy = assessment_case()
    source = normalized(raw, policy)
    assert source.tax_complete and source.tax == 20 and source.subtotal == 100
    tax = source.tax_details[0]
    assert tax.calculation == "source_assessment" and tax.rate is None and tax.rounding is None
    assert tax.included_gross_basis is None and tax.included_rate_total is None
    assert tax.key == "line:11:tax:15:source_rate:7:adjustment:99"
    assert tax.allocation_key == "line:11:tax:15"
    assert tax.assessment.model_dump() == {
        "authority": "framework_finalized_adjustment",
        "source_tax_id": "7",
        "adjustment_id": "99",
        "finalized": True,
        "updated_at": datetime(2026, 9, 4, 9, tzinfo=timezone.utc),
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"finalized": False, "eligible": True},
        {"finalized": "true"},
        {"finalized": 1},
        {"updated_at": None},
        {"updated_at": "2026-09-04T09:00:00"},
        {"updated_at": "2026-09-04T10:30:00Z"},
        {"adjustable_id": "12"},
        {"adjustable_type": "Spree::Shipment"},
    ],
)
def test_assessment_requires_finalized_literal_true_exact_owner_and_a_proven_clock(changes):
    raw, policy = assessment_case()
    raw["orders"][0]["line_items"][0]["adjustments"][0].update(changes)
    assert not normalized(raw, policy).tax_complete


def test_assessment_requires_explicit_native_allocation_profile():
    raw, policy = assessment_case()
    for replacement in (
        None,
        policy.netsuite_legacy_tax.model_copy(update={"tax_code_id": "16"}),
        policy.netsuite_legacy_tax.model_copy(update={"subsidiary_id": "2"}),
    ):
        assert not normalized(raw, policy.model_copy(update={"netsuite_legacy_tax": replacement})).tax_complete


def test_assessment_cannot_reuse_an_adjustment_identity_across_source_lines():
    raw, policy = assessment_case()
    order = raw["orders"][0]
    second = deepcopy(order["line_items"][0])
    second["id"] = "12"
    second["adjustments"][0]["adjustable_id"] = "12"
    order["line_items"].append(second)
    order.update(total="240", item_total="240", tax_total="40", included_tax_total="40")
    assert not normalized(raw, policy).tax_complete


def test_mixed_assessed_and_statutory_included_components_need_more_evidence():
    raw, policy = assessment_case()
    item = raw["orders"][0]["line_items"][0]
    item["adjustments"].append({**item["adjustments"][0], "id": "100", "source_id": "8", "amount": "0"})
    values = policy.model_dump()
    values["tax_rules"]["8"] = {"rate": "0", "rounding": "half_up", "included": True, "netsuite_tax_id": "15"}
    assert not normalized(raw, type(policy).model_validate(values)).tax_complete


def test_assessed_source_and_exact_native_allocations_can_reconcile_without_a_rate():
    from tests.test_transaction_ops_legacy_tax import compare, raw_target, target

    raw, policy = assessment_case()
    assert (
        compare(normalized(raw, policy), target(raw_target("line_tax_amount"), policy)).recommended_action
        == "no_action"
    )


@pytest.mark.parametrize("changed", ["missing", "future"])
def test_comparison_revalidates_assessment_proof_even_when_completeness_is_claimed(changed):
    from datetime import timedelta

    from tests.test_transaction_ops_legacy_tax import compare, raw_target, target

    raw, policy = assessment_case()
    source = normalized(raw, policy)
    tax = source.tax_details[0]
    proof = (
        None
        if changed == "missing"
        else tax.assessment.model_copy(update={"updated_at": source.updated_at + timedelta(seconds=1)})
    )
    source = source.model_copy(update={"tax_details": (tax.model_copy(update={"assessment": proof}),)})
    result = compare(source, target(raw_target("line_tax_amount"), policy))
    assert result.recommended_action == "gather_evidence"
    assert "source_assessment_unproven" in {f.code for f in result.findings}


def assessed_action_source(mode="line_tax_amount"):
    from tests.test_transaction_ops_legacy_actions import legacy_source

    source = legacy_source(mode)
    source["tax_details"][0].update(
        key="line:11:tax:610:source_rate:7:adjustment:99",
        allocation_key="line:11:tax:610",
        calculation="source_assessment",
        rate=None,
        rounding=None,
        assessment={
            "authority": "framework_finalized_adjustment",
            "source_tax_id": "7",
            "adjustment_id": "99",
            "finalized": True,
            "updated_at": source["updated_at"],
        },
    )
    return source


@pytest.mark.parametrize("mode", ["aggregate_header", "line_tax_amount"])
def test_legacy_correction_uses_proven_assessed_amount_without_inventing_statutory_rate(mode):
    from tests.test_transaction_ops_legacy_actions import prepare

    result = prepare(src=assessed_action_source(mode), mode=mode)
    assert result.after_json["expected_totals"]["taxtotal"] == "20"
    assert result.after_json["line_changes"][0]["fields"]["custcol_fw_vat_amount"] == "20"


def test_generic_native_rate_correction_cannot_accept_assessed_amounts():
    from app.services.transaction_ops.netsuite_actions import NetSuiteActionError, prepare_correction
    from tests.test_transaction_ops_netsuite_actions import target

    with pytest.raises(NetSuiteActionError, match="assessment_requires_native_profile"):
        prepare_correction(target(), assessed_action_source())


def test_source_assessment_cannot_silently_replace_a_native_statutory_tax_contract():
    from app.schemas.transaction_ops import TransactionTax
    from tests.test_transaction_ops_legacy_tax import compare, raw_target, target

    raw, policy = assessment_case()
    dst = target(raw_target("line_tax_amount"), policy)
    dst = dst.model_copy(
        update={
            "tax_details": (
                TransactionTax(key="line:11:tax:15", basis="100", rate="0.2", amount="20", rounding="half_up"),
            )
        }
    )
    result = compare(normalized(raw, policy), dst)
    assert result.recommended_action == "human_review"
    assert "tax_structure_mismatch" in {f.code for f in result.findings}


@pytest.mark.parametrize("invalid", [1, True, {"amount": "20"}, "unknown"])
def test_malformed_adjustment_collection_stays_incomplete(invalid):
    raw, policy = assessment_case()
    raw["orders"][0]["line_items"][0]["adjustments"] = invalid
    assert not normalized(raw, policy).tax_complete


def test_canonical_assessment_cannot_smuggle_a_rate_via_model_copy():
    from decimal import Decimal

    from tests.test_transaction_ops_legacy_tax import compare, raw_target, target

    raw, policy = assessment_case()
    src = normalized(raw, policy)
    src = src.model_copy(update={"tax_details": (src.tax_details[0].model_copy(update={"rate": Decimal("0.2")}),)})
    assert compare(src, target(raw_target("line_tax_amount"), policy)).recommended_action == "gather_evidence"


def test_assessed_correction_rechecks_currency_precision_at_the_action_boundary():
    from app.services.transaction_ops.netsuite_actions import NetSuiteActionError
    from tests.test_transaction_ops_legacy_actions import prepare

    source = assessed_action_source()
    source.update(subtotal="100.005", tax="20.001", total="122.006")
    source["lines"][0].update(net="100.005", tax="20.001")
    source["tax_details"][0].update(basis="100.005", amount="20.001")
    with pytest.raises(NetSuiteActionError, match="source_currency_precision"):
        prepare(src=source, mode="line_tax_amount")


def zero_shipping_assessment_case():
    from tests.test_transaction_ops_legacy_tax import raw_target, target

    raw, policy = assessment_case()
    raw["orders"][0]["shipments"][0]["adjustments"] = [
        {
            "id": "100",
            "source_type": "Spree::TaxRate",
            "source_id": "7",
            "adjustable_type": "Spree::Shipment",
            "adjustable_id": "5",
            "amount": "0",
            "finalized": True,
            "updated_at": "2026-09-04T09:00:00Z",
        }
    ]
    return normalized(raw, policy), target(raw_target("line_tax_amount"), policy)


def test_zero_basis_shipping_assessment_needs_no_synthetic_native_shipping_tax_record():
    from tests.test_transaction_ops_legacy_tax import compare

    src, dst = zero_shipping_assessment_case()
    assert src.tax_complete and len(src.tax_details) == 2 and len(dst.tax_details) == 1
    assert compare(src, dst).recommended_action == "no_action"


def test_zero_shipping_tax_exception_requires_zero_source_basis():
    from decimal import Decimal

    from tests.test_transaction_ops_legacy_tax import compare

    src, dst = zero_shipping_assessment_case()
    src = src.model_copy(
        update={"tax_details": (src.tax_details[0], src.tax_details[1].model_copy(update={"basis": Decimal("1")}))}
    )
    assert compare(src, dst).recommended_action == "human_review"
