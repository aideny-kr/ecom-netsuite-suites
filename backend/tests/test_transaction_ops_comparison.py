"""Money/evidence contracts for Framework -> NetSuite investigations."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext

import pytest
from pydantic import ValidationError

from app.schemas.transaction_ops import TransactionLookup, TransactionSnapshot
from app.services.transaction_ops.comparison import compare_transactions

NOW = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)


def snapshot(system="framework", **changes):
    data = {
        "system": system,
        "account_id": "store-1" if system == "framework" else "6738075_SB1",
        "record_id": "order-1" if system == "framework" else "123",
        "record_type": "order" if system == "framework" else "salesorder",
        "order_reference": "R100000001-EXACT",
        "subsidiary_id": "5",
        "currency": "GBP",
        "currency_minor_unit": 2,
        "amount_basis": "transaction",
        "status": "confirmed",
        "updated_at": NOW - timedelta(hours=1),
        "observed_at": NOW,
        "authoritative": True,
        "total": "120.00",
        "subtotal": "100.00",
        "tax": "20.00",
        "shipping": "0.00",
        "shipping_tax": "0.00",
        "discount": "0.00",
        "lines_complete": True,
        "tax_complete": True,
        "lines": [{"key": "sku-1", "quantity": "1", "net": "100.00", "tax": "20.00"}],
        "tax_details": [
            {"key": "GB:VAT:standard", "basis": "100", "rate": "0.20", "amount": "20", "rounding": "half_up"}
        ],
    }
    data.update(changes)
    return TransactionSnapshot.model_validate(data)


def lookup(**changes):
    data = {
        "source_system": "framework",
        "source_account_id": "store-1",
        "source_record_id": "order-1",
        "order_reference": "R100000001-EXACT",
        "target_account_id": "6738075_SB1",
        "target_subsidiary_id": "5",
        "target_record_type": "salesorder",
        "complete": True,
        "authoritative": True,
        "observed_at": NOW,
        "error_order_reference": "R100000001-EXACT",
        "celigo_error_id": "error-1",
        "error_observed_at": NOW,
        "error_is_open": True,
        "error_scope": {
            "connection_id": "celigo-connection",
            "flow_id": "flow",
            "step_id": "import",
            "target_account_id": "6738075_SB1",
            "target_subsidiary_id": "5",
            "target_record_type": "salesorder",
            "operation": "create",
            "kind": "duplicate_transaction",
        },
    }
    data.update(changes)
    return TransactionLookup.model_validate(data)


def compare(source=None, records=None, evidence=None):
    return compare_transactions(
        source or snapshot(),
        [snapshot("netsuite")] if records is None else records,
        evidence or lookup(),
        now=NOW,
    )


def codes(result):
    return {finding.code for finding in result.findings}


def test_incomplete_tax_evidence_still_reports_observed_header_mismatch_without_repair():
    result = compare(records=[snapshot("netsuite", total="121", tax_complete=False)])
    assert result.recommended_action == "gather_evidence"
    assert next(d for d in result.differences if d.field == "total").delta == Decimal("-1")
    assert "amount_mismatch" in codes(result)


def test_proven_absence_is_visible_even_when_tax_metadata_still_needs_collection():
    result = compare(source=snapshot(tax_complete=False), records=[])
    assert result.recommended_action == "gather_evidence"
    assert "missing_transaction" in codes(result)


def test_included_tax_uses_unrounded_gross_basis_and_combined_included_rate():
    source = snapshot(
        total="0.03",
        subtotal="0.02",
        tax="0.01",
        lines=[{"key": "sku-1", "quantity": "1", "net": "0.02", "tax": "0.01"}],
        tax_details=[
            {
                "key": "VAT",
                "basis": "0.02",
                "rate": "0.20",
                "amount": "0.01",
                "rounding": "half_up",
                "included_gross_basis": "0.03",
                "included_rate_total": "0.20",
            }
        ],
    )
    assert compare(source=source, records=[]).recommended_action == "propose_missing_sync"


@pytest.mark.parametrize(
    "changes",
    [
        {"included_gross_basis": "120"},
        {"included_rate_total": "0.2"},
        {"included_gross_basis": "120", "included_rate_total": "-1"},
        {"included_gross_basis": "120", "included_rate_total": "0.1"},
    ],
)
def test_included_calculation_requires_paired_valid_basis_and_total_rate(changes):
    with pytest.raises(ValidationError):
        snapshot(tax_details=[{"key": "VAT", "basis": "100", "rate": "0.2", "amount": "20", **changes}])


def test_exact_detailed_match_can_propose_false_alarm_but_never_authorizes_write():
    result = compare()
    assert result.recommended_action == "propose_false_alarm_resolution"
    assert result.requires_human_approval is True
    assert result.differences == ()
    assert result.evidence_fingerprint


def test_match_without_linked_error_has_no_action():
    assert compare(evidence=lookup(celigo_error_id=None, error_order_reference=None)).recommended_action == "no_action"


@pytest.mark.parametrize("changes", [{"error_order_reference": "R100000001-OTHER"}, {"error_order_reference": None}])
def test_error_is_not_correlated_by_prefix(changes):
    result = compare(evidence=lookup(**changes))
    assert result.recommended_action == "human_review"
    assert "error_identity_unproven" in codes(result)


def test_missing_requires_exhaustive_lookup_and_creates_only_a_proposal():
    result = compare(records=[])
    assert result.recommended_action == "propose_missing_sync"
    assert result.requires_human_approval
    assert "missing_transaction" in codes(result)


@pytest.mark.parametrize(
    "changes", [{"complete": False}, {"authoritative": False}, {"observed_at": NOW - timedelta(hours=1)}]
)
def test_failed_partial_or_stale_lookup_does_not_prove_missing(changes):
    result = compare(records=[], evidence=lookup(**changes))
    assert result.recommended_action == "gather_evidence"
    assert "missing_transaction" not in codes(result)


@pytest.mark.parametrize("state", ["draft", "cancelled", "refunded", "unknown"])
def test_non_importable_source_is_not_synced(state):
    assert compare(source=snapshot(status=state), records=[]).recommended_action == "human_review"


@pytest.mark.parametrize(
    "field,value",
    [
        ("currency", "USD"),
        ("subsidiary_id", "2"),
        ("account_id", "6738075"),
        ("amount_basis", "base"),
        ("record_type", "invoice"),
    ],
)
def test_numeric_equality_cannot_hide_currency_or_target_mismatch(field, value):
    result = compare(records=[snapshot("netsuite", **{field: value})])
    assert result.recommended_action == "human_review"
    assert not result.differences  # Never subtract incomparable amounts.


def test_duplicate_matches_are_not_automatically_paired():
    result = compare(records=[snapshot("netsuite"), snapshot("netsuite", record_id="456")])
    assert result.recommended_action == "human_review"
    assert "ambiguous_match" in codes(result)


@pytest.mark.parametrize(
    "change",
    [
        {"total": "121"},
        {"tax": "21", "total": "121"},
        {"lines": [{"key": "sku-1", "quantity": "1", "net": "99", "tax": "21"}]},
    ],
)
def test_amount_tax_and_line_differences_are_retained(change):
    result = compare(records=[snapshot("netsuite", **change)])
    assert result.recommended_action == "propose_amount_correction"
    assert result.differences
    assert result.requires_human_approval


def test_offsetting_tax_and_subtotal_difference_is_not_false_alarm():
    result = compare(records=[snapshot("netsuite", subtotal="99", tax="21")])
    assert result.recommended_action == "propose_amount_correction"
    assert {d.field for d in result.differences} >= {"subtotal", "tax"}


def test_three_decimal_currency_difference_is_not_rounded_away():
    result = compare(
        source=snapshot(currency="KWD", currency_minor_unit=3, shipping="0.001", total="120.001"),
        records=[snapshot("netsuite", currency="KWD", currency_minor_unit=3, total="120.002")],
    )
    assert next(d for d in result.differences if d.field == "total").delta == Decimal("-0.001")


def test_zero_decimal_currency_and_zero_amounts_are_valid():
    values = dict(
        currency="JPY",
        currency_minor_unit=0,
        total="0",
        subtotal="0",
        tax="0",
        lines=[{"key": "sku-1", "quantity": "1", "net": "0", "tax": "0"}],
        tax_details=[],
    )
    assert (
        compare(source=snapshot(**values), records=[snapshot("netsuite", **values)]).recommended_action
        == "propose_false_alarm_resolution"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"tax": None},
        {"lines_complete": False},
        {"tax_complete": False},
        {"authoritative": False},
        {"observed_at": NOW - timedelta(minutes=16)},
    ],
)
def test_unknown_or_stale_detail_never_yields_repair_or_false_alarm(changes):
    result = compare(source=snapshot(**changes))
    assert result.recommended_action == "gather_evidence"


@pytest.mark.parametrize(
    "changes", [{"source_record_id": "other"}, {"source_account_id": "other"}, {"order_reference": "R100000001"}]
)
def test_lookup_is_bound_to_full_source_identity(changes):
    assert compare(evidence=lookup(**changes)).recommended_action == "human_review"


@pytest.mark.parametrize("value", [1.1, float("nan"), float("inf"), "NaN", "Infinity", True, "not money"])
def test_money_rejects_binary_float_and_nonfinite_values(value):
    with pytest.raises(ValidationError):
        snapshot(total=value)


def test_nested_line_and_tax_money_use_the_same_validation():
    with pytest.raises(ValidationError):
        snapshot(lines=[{"key": "a", "quantity": "1", "net": 1.1, "tax": "0"}])
    with pytest.raises(ValidationError):
        snapshot(tax_details=[{"key": "VAT", "basis": "1", "rate": float("nan"), "amount": "1"}])


@pytest.mark.parametrize(
    "changes", [{"observed_at": NOW.replace(tzinfo=None)}, {"updated_at": NOW.replace(tzinfo=None)}]
)
def test_naive_evidence_timestamps_are_rejected(changes):
    with pytest.raises(ValidationError):
        snapshot(**changes)


def test_future_observation_does_not_pass_freshness():
    assert compare(source=snapshot(observed_at=NOW + timedelta(hours=1))).recommended_action == "gather_evidence"


def test_duplicate_line_keys_are_rejected_instead_of_collapsed():
    with pytest.raises(ValidationError):
        snapshot(lines=[{"key": "a", "quantity": "1", "net": "1", "tax": "0"}] * 2)


def test_new_observation_preserves_work_fingerprint_but_changed_amount_does_not():
    a = compare()
    b = compare(source=snapshot(observed_at=NOW - timedelta(seconds=1)))
    c = compare(source=snapshot(total="121"))
    assert a.evidence_fingerprint == b.evidence_fingerprint
    assert a.evidence_fingerprint != c.evidence_fingerprint


def test_same_total_but_different_tax_jurisdiction_requires_review():
    result = compare(
        records=[
            snapshot(
                "netsuite",
                tax_details=[
                    {"key": "FR:VAT:standard", "basis": "100", "rate": "0.20", "amount": "20", "rounding": "half_up"}
                ],
            )
        ]
    )
    assert result.recommended_action == "human_review"
    assert "tax_structure_mismatch" in codes(result)


@pytest.mark.parametrize(
    "changes", [{"error_observed_at": None}, {"error_observed_at": NOW - timedelta(hours=1)}, {"error_is_open": False}]
)
def test_stale_or_already_closed_error_cannot_be_resolved(changes):
    assert compare(evidence=lookup(**changes)).recommended_action == "gather_evidence"


@pytest.mark.parametrize(
    "changes",
    [{"total": "900"}, {"tax_details": []}, {"lines": [{"key": "sku-1", "quantity": "1", "net": "999", "tax": "20"}]}],
)
def test_inconsistent_source_is_not_copied_to_netsuite(changes):
    assert compare(source=snapshot(**changes)).recommended_action in {"human_review", "gather_evidence"}


@pytest.mark.parametrize("missing", [True, False])
def test_negative_source_total_requires_review_even_with_consistent_arithmetic(missing):
    amounts = dict(
        total="-16.00",
        subtotal="284.00",
        tax="0.00",
        discount="300.00",
        lines=[{"key": "sku-1", "quantity": "1", "net": "284.00", "tax": "0.00"}],
        tax_details=[{"key": "zero", "basis": "284", "rate": "0", "amount": "0", "rounding": "half_up"}],
    )
    result = compare(source=snapshot(**amounts), records=[] if missing else [snapshot("netsuite", **amounts)])
    assert result.recommended_action == "human_review"
    assert "source_state_requires_review" in codes(result)


def test_line_tax_must_reconcile_with_header_and_shipping_tax():
    bad = dict(lines=[{"key": "sku-1", "quantity": "1", "net": "100", "tax": "999"}])
    result = compare(source=snapshot(**bad), records=[snapshot("netsuite", **bad)])
    assert result.recommended_action == "human_review"
    assert "source_tax_inconsistent" in codes(result)


def test_contradictory_tax_basis_rate_and_amount_are_not_a_false_alarm():
    bad = dict(
        tax_details=[{"key": "GB:VAT:standard", "basis": "100", "rate": "0.99", "amount": "20", "rounding": "half_up"}]
    )
    result = compare(source=snapshot(**bad), records=[snapshot("netsuite", **bad)])
    assert result.recommended_action == "human_review"
    assert "source_tax_calculation_inconsistent" in codes(result)


def test_unknown_tax_rounding_policy_is_not_guessed():
    source = snapshot(tax_details=[{"key": "GB:VAT:standard", "basis": "100", "rate": "0.2", "amount": "20"}])
    assert compare(source=source).recommended_action == "gather_evidence"


def test_derived_delta_may_exceed_valid_operand_bound():
    source = snapshot(
        total="900000000000000000000000",
        subtotal="900000000000000000000000",
        tax="0",
        lines=[{"key": "sku-1", "quantity": "1", "net": "900000000000000000000000", "tax": "0"}],
        tax_details=[],
    )
    result = compare(source=source, records=[snapshot("netsuite", total="-900000000000000000000000")])
    assert next(d for d in result.differences if d.field == "total").delta == Decimal("1800000000000000000000000")


def test_fingerprint_normalizes_decimal_precision_and_timestamp_offsets():
    a = compare()
    b = compare(
        source=snapshot(total="120.000", updated_at=(NOW - timedelta(hours=1)).astimezone(timezone(timedelta(hours=8))))
    )
    assert a.evidence_fingerprint == b.evidence_fingerprint


@pytest.mark.parametrize(
    "change",
    [{"kind": "unknown"}, {"target_record_type": "invoice"}, {"target_account_id": "6738075"}, {"operation": "update"}],
)
def test_other_operations_error_cannot_be_resolved_from_order_equality(change):
    scope = lookup().error_scope.model_dump()
    scope.update(change)
    assert compare(evidence=lookup(error_scope=scope)).recommended_action == "human_review"


def test_error_without_verified_operation_scope_is_not_false_alarm():
    assert compare(evidence=lookup(error_scope=None)).recommended_action == "human_review"


def rounded_snapshot(system="framework", *, rounding="half_up"):
    return snapshot(
        system,
        total="0.06",
        subtotal="0.05",
        tax="0.01",
        lines=[{"key": "sku-1", "quantity": "1", "net": "0.05", "tax": "0.01"}],
        tax_details=[
            {"key": "GB:VAT:standard", "basis": "0.05", "rate": "0.10", "amount": "0.01", "rounding": rounding}
        ],
    )


def test_target_tax_calculation_cannot_be_contradictory_even_when_amounts_match():
    result = compare(source=rounded_snapshot(), records=[rounded_snapshot("netsuite", rounding="half_even")])
    assert result.recommended_action == "human_review"
    assert "target_tax_calculation_inconsistent" in codes(result)


def test_fractional_yen_is_not_proposed_for_sync():
    source = snapshot(
        currency="JPY",
        currency_minor_unit=0,
        total="100.25",
        subtotal="100.25",
        tax="0",
        lines=[{"key": "sku-1", "quantity": "1", "net": "100.25", "tax": "0"}],
        tax_details=[],
    )
    result = compare(source=source, records=[])
    assert result.recommended_action == "human_review"
    assert "currency_precision_violation" in codes(result)


def test_ambient_inexact_traps_do_not_crash_valid_tax_rounding():
    source, target = rounded_snapshot(), rounded_snapshot("netsuite")
    with localcontext() as context:
        context.traps[Inexact] = True
        result = compare(source=source, records=[target])
    assert result.recommended_action == "propose_false_alarm_resolution"
