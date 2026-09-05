"""Configured legacy allocations never stand in for statutory source tax."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.transaction_ops import TransactionLookup, TransactionSnapshot, TransactionTax
from app.services.transaction_ops.comparison import compare_transactions
from app.services.transaction_ops.normalization import normalize_framework_order, normalize_netsuite_order
from tests.test_transaction_ops_normalization import evidence, mapping, ns_order

NOW = datetime(2026, 9, 4, 11, tzinfo=timezone.utc)


def profile(mode="aggregate_header", **changes):
    return {
        "schema_version": 1,
        "mode": mode,
        "account_id": "6738075",
        "subsidiary_id": "1",
        "tax_code_id": "15",
        **changes,
    }


def config(mode="aggregate_header", **changes):
    return mapping(netsuite_legacy_tax=profile(mode), **changes)


def raw_target(mode="aggregate_header"):
    raw = ns_order()
    raw["header"].update(
        isTaxable=True,
        custbody_fw_solidus_tax_amount="20",
        custbody_fw_solidus_order_total="120",
        shippingTax1Rate="0",
        shippingTax2Rate="0",
    )
    if mode == "line_tax_amount":
        raw["header"].pop("taxItem")
        raw["lines"][0].update(taxCode={"id": "15"}, tax1Amt="20", taxRate1="20.001", isTaxable=True)
    return raw


def source(payload=None, policy=None):
    return normalize_framework_order(
        payload or evidence(), mapping=policy or config(), account_id="frame.work", subsidiary_id="1"
    )


def target(raw=None, policy=None, account_id="6738075"):
    return normalize_netsuite_order(
        raw or raw_target(), mapping=policy or config(), account_id=account_id, observed_at=NOW.isoformat()
    )


def compare(src, dst):
    lookup = TransactionLookup(
        source_system=src.system,
        source_account_id=src.account_id,
        source_record_id=src.record_id,
        order_reference=src.order_reference,
        target_account_id=dst.account_id,
        target_subsidiary_id="1",
        target_record_type="salesorder",
        complete=True,
        authoritative=True,
        observed_at=NOW,
    )
    return compare_transactions(src, [dst], lookup, now=NOW)


@pytest.mark.parametrize("mode", ["aggregate_header", "line_tax_amount"])
def test_explicit_profile_proves_reported_allocation_without_inventing_rate(mode):
    policy = config(mode)
    dst = target(raw_target(mode), policy)
    assert dst.tax_complete and dst.shipping_tax == 0
    tax = dst.tax_details[0]
    assert tax.calculation == "reported_allocation"
    assert tax.rate is None and tax.rounding is None
    assert tax.basis == 100 and tax.amount == 20
    assert compare(source(policy=policy), dst).recommended_action == "no_action"


def test_legacy_profile_is_never_inferred_from_tax_item_or_currency():
    assert not target(raw_target(), mapping()).tax_complete


@pytest.mark.parametrize("changes", [{"account_id": "6738076"}, {"subsidiary_id": "2"}, {"tax_code_id": "16"}])
def test_profile_must_match_exact_account_subsidiary_and_native_code(changes):
    policy = mapping(netsuite_legacy_tax=profile(**changes))
    dst = target(policy=policy)
    assert not dst.tax_complete
    assert compare(source(), dst).recommended_action == "gather_evidence"


def test_profile_account_alias_is_canonicalized():
    policy = mapping(netsuite_legacy_tax=profile(account_id="6738075_SB1"))
    assert policy.netsuite_legacy_tax.account_id == "6738075-sb1"
    assert target(policy=policy, account_id="6738075-sb1").tax_complete


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["header"].pop("custbody_fw_solidus_tax_amount"),
        lambda r: r["header"].update(custbody_fw_solidus_tax_amount="21"),
        lambda r: r["header"].update(isTaxable=False),
        lambda r: r["header"].pop("taxRate"),
        lambda r: r["header"].update(taxRate="-1"),
        lambda r: r["header"].update(taxTotal="21"),
        lambda r: r["lines"][0].pop("custcol_fw_vat_amount"),
        lambda r: r["lines"][0].update(custcol_fw_vat_amount="-20"),
        lambda r: r["header"].update(shippingCost="10", shippingTax1Rate="20"),
        lambda r: (r["header"].update(shippingCost="10"), r["header"].pop("shippingTax1Rate")),
    ],
)
def test_missing_or_contradictory_aggregate_allocation_cannot_resolve_error(mutate):
    raw = raw_target()
    mutate(raw)
    dst = target(raw)
    assert not dst.tax_complete
    assert compare(source(), dst).recommended_action == "gather_evidence"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["lines"][0].pop("tax1Amt"),
        lambda r: r["lines"][0].update(tax1Amt="19"),
        lambda r: r["lines"][0].pop("taxCode"),
        lambda r: r["lines"][0].update(taxCode={"id": "16"}),
        lambda r: r["lines"][0].update(isTaxable=False),
    ],
)
def test_line_amount_profile_requires_native_and_custom_vat_equality(mutate):
    raw = raw_target("line_tax_amount")
    mutate(raw)
    assert not target(raw, config("line_tax_amount")).tax_complete


def test_source_statutory_error_still_vetoes_matching_observed_legacy_amounts():
    policy = config(tax_rules={"7": {"rate": "0.21", "included": True, "rounding": "half_up", "netsuite_tax_id": "15"}})
    result = compare(source(policy=policy), target())
    assert result.recommended_action == "human_review"
    assert "source_tax_calculation_inconsistent" in {f.code for f in result.findings}


def test_observed_target_difference_proposes_repair_in_transaction_currency():
    raw = raw_target()
    raw["header"].update(
        total="121", taxTotal="21", custbody_fw_solidus_tax_amount="21", custbody_fw_solidus_order_total="121"
    )
    raw["lines"][0]["custcol_fw_vat_amount"] = "21"
    result = compare(source(), target(raw))
    assert result.recommended_action == "propose_amount_correction"
    assert next(d for d in result.differences if d.field == "tax").delta == Decimal("-1")
    assert all(not d.field.endswith(".rate") for d in result.differences)


def test_only_netsuite_can_report_an_allocation_without_a_statutory_rate():
    data = source().model_dump()
    data["tax_details"][0].update(
        calculation="reported_allocation", rate=None, rounding=None, included_gross_basis=None, included_rate_total=None
    )
    with pytest.raises(ValidationError):
        TransactionSnapshot.model_validate(data)


@pytest.mark.parametrize("extra", [{"rate": "0.2"}, {"rounding": "half_up"}, {"allocation_key": "line:11:tax:15"}])
def test_reported_allocation_cannot_smuggle_a_rate_policy_or_further_aggregation(extra):
    with pytest.raises(ValidationError):
        TransactionTax.model_validate(
            {"key": "line:11:tax:15", "calculation": "reported_allocation", "basis": "100", "amount": "20", **extra}
        )


def multi_component_source():
    payload = evidence(item_total="100", included_tax_total="0", additional_tax_total="20", adjustment_total="20")
    line = payload["orders"][0]["line_items"][0]
    line["price"] = "100"
    line["adjustments"][0]["amount"] = "5"
    line["adjustments"].append({**line["adjustments"][0], "id": "100", "source_id": "8", "amount": "15"})
    rules = {
        key: {"rate": rate, "included": False, "rounding": "half_up", "netsuite_tax_id": "15"}
        for key, rate in [("7", "0.05"), ("8", "0.15")]
    }
    return payload, config(tax_rules=rules)


def test_multiple_statutory_components_remain_distinct_and_match_one_observed_allocation():
    payload, policy = multi_component_source()
    src = source(payload, policy)
    assert src.tax_complete and len(src.tax_details) == 2
    assert len({t.key for t in src.tax_details}) == 2
    assert {t.allocation_key for t in src.tax_details} == {"line:11:tax:15"}
    assert compare(src, target()).recommended_action == "no_action"
    payload["orders"][0]["line_items"][0]["adjustments"].reverse()
    assert (
        compare(source(payload, policy), target()).evidence_fingerprint == compare(src, target()).evidence_fingerprint
    )


def test_component_error_cannot_hide_behind_matching_aggregate_amount():
    payload, policy = multi_component_source()
    adjustments = payload["orders"][0]["line_items"][0]["adjustments"]
    adjustments[0]["amount"], adjustments[1]["amount"] = "6", "14"
    assert compare(source(payload, policy), target()).recommended_action == "human_review"


def test_different_component_bases_cannot_be_collapsed_to_one_taxable_event():
    payload, policy = multi_component_source()
    src = source(payload, policy)
    data = src.model_dump()
    data["tax_details"][1].update(basis="200", rate="0.075")
    result = compare(TransactionSnapshot.model_validate(data), target())
    assert result.recommended_action == "human_review"
    assert "tax_structure_mismatch" in {f.code for f in result.findings}


def test_native_suitetax_is_still_validated_even_with_legacy_profile():
    raw = raw_target()
    raw["tax_details"] = [
        {"taxDetailsReference": "L63", "taxCode": {"id": "15"}, "taxBasis": "100", "taxRate": "21", "taxAmount": "20"}
    ]
    dst = target(raw, config(netsuite_tax_rounding="half_up"))
    assert dst.tax_details[0].calculation == "statutory_rate"
    assert compare(source(), dst).recommended_action == "human_review"


def test_reader_retains_native_legacy_tax_fields_needed_for_allocation_proof():
    from app.services.transaction_ops.netsuite_reader import HEADER_FIELDS, _project

    fields = {"custbody_fw_solidus_tax_amount": "20", "shippingTax1Rate": "0", "shippingTax2Rate": "0"}
    assert _project({**fields, "email": "private@example.test"}, HEADER_FIELDS) == fields


def test_explicit_zero_tax_without_adjustments_matches_zero_native_allocation():
    payload = evidence(total="100", item_total="100", tax_total="0", included_tax_total="0")
    line = payload["orders"][0]["line_items"][0]
    line.update(price="100", total="100", adjustments=[])
    raw = raw_target()
    raw["header"].update(
        total="100",
        taxTotal="0",
        taxRate="0",
        custbody_fw_solidus_tax_amount="0",
        custbody_fw_solidus_order_total="100",
    )
    raw["lines"][0]["custcol_fw_vat_amount"] = "0"
    assert compare(source(payload), target(raw)).recommended_action == "no_action"


def test_allocation_cannot_point_at_a_different_line_or_be_supplied_by_destination():
    payload, policy = multi_component_source()
    src = source(payload, policy).model_dump()
    src["tax_details"][0]["allocation_key"] = "line:12:tax:15"
    with pytest.raises(ValidationError):
        TransactionSnapshot.model_validate(src)
    src = source(payload, policy).model_dump()
    src["system"] = "netsuite"
    with pytest.raises(ValidationError):
        TransactionSnapshot.model_validate(src)


def test_reported_allocations_must_still_reconcile_to_target_tax_total():
    raw = target().model_dump()
    raw["tax_details"][0]["amount"] = "19"
    result = compare(source(), TransactionSnapshot.model_validate(raw))
    assert result.recommended_action == "human_review"
    assert "target_tax_allocation_inconsistent" in {f.code for f in result.findings}
