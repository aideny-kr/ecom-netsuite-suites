"""Rounding tolerance (Aiden, 2026-10-07): per config, off by default, at most 5 minor units.

49 open Framework cases differed by 1 to 5 cents, mostly VAT rounding (header vs line).
A config may accept such a difference as rounding when the order total AND the tax are both
within the tolerance and refunds agree exactly. The match is an explicit, recorded adjustment
(`rounding_tolerance`); the original cents stay in `original_amounts`.
"""

import pytest
from pydantic import ValidationError

from app.services.transaction_ops.normalization import TransactionMapping
from app.services.transaction_ops.order_reconciliation import reconcile_order
from tests.test_order_balance_reconciliation import evidence


def _case(total="120.01", tax="20.01", *, units=5, precision=2, refund_target="0.00"):
    source, target, config, refunds = evidence()
    header = target["orders"][0]["header"]
    header.update(total=total, taxTotal=tax)
    target["orders"][0]["currency_metadata"]["currencyPrecision"] = precision
    refunds["target"]["amount"] = refund_target
    if units is not None:
        config["mapping_json"]["rounding_tolerance"] = {"schema_version": 1, "minor_units": units}
    return reconcile_order(source, target, config, refunds=refunds)


def test_without_a_tolerance_a_cent_still_differs():
    result = _case(units=None)
    assert result["status"] == "difference"
    assert result["amounts"]["order_total"]["delta"] == "-0.01"


@pytest.mark.parametrize(
    "total, tax", [("120.01", "20.01"), ("119.99", "19.99"), ("120.05", "20.00"), ("120.00", "20.05")]
)
def test_a_difference_within_the_tolerance_matches_as_recorded_rounding(total, tax):
    result = _case(total, tax)
    assert (result["status"], result["reason"]) == ("matched", "within_rounding_tolerance")
    # The case rule (case_service) requires zero deltas; the real cents stay recorded.
    assert all(row["delta"] == "0.00" for row in result["amounts"].values())
    original = result["original_amounts"]
    assert (
        original["order_total"]["target"] == f"{float(total):.2f}" and original["tax"]["target"] == f"{float(tax):.2f}"
    )
    (rounding,) = [a for a in result["adjustments"] if a["kind"] == "rounding_tolerance"]
    assert rounding["minor_units"] == 5 and rounding["tolerance"] == "0.05"
    assert rounding["order_total_delta"] == original["order_total"]["delta"]
    assert rounding["tax_delta"] == original["tax"]["delta"]


@pytest.mark.parametrize(
    "total, tax, why",
    [
        ("120.06", "20.00", "total beyond the tolerance"),
        ("120.01", "31.91", "tax split differs by far more than rounding (the TWD-like cases)"),
        ("120.00", "20.06", "tax beyond the tolerance"),
    ],
)
def test_a_difference_beyond_the_tolerance_still_differs(total, tax, why):
    assert _case(total, tax)["status"] == "difference", why


def test_refunds_must_agree_exactly():
    result = _case(refund_target="0.01")
    assert result["status"] == "difference"


def test_the_tolerance_counts_minor_units_of_the_currency():
    # A zero-decimal currency: 5 minor units are 5 whole units.
    assert _case("123", "23", precision=0)["status"] == "matched"
    assert _case("126", "20", precision=0)["status"] == "difference"


def test_a_smaller_configured_tolerance_is_respected():
    assert _case("120.01", "20.01", units=1)["status"] == "matched"
    assert _case("120.02", "20.00", units=1)["status"] == "difference"


@pytest.mark.parametrize(
    "value",
    [
        {"schema_version": 1, "minor_units": 6},
        {"schema_version": 1, "minor_units": 0},
        {"schema_version": 2, "minor_units": 1},
        {"minor_units": 1},
        "5",
    ],
)
def test_an_invalid_tolerance_never_widens_a_match(value):
    source, target, config, refunds = evidence()
    target["orders"][0]["header"].update(total="120.01", taxTotal="20.01")
    config["mapping_json"]["rounding_tolerance"] = value
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "difference"


def test_the_mapping_declares_the_tolerance_and_bounds_it():
    base = {"reference_field": "tranid"}
    assert TransactionMapping.model_validate(base).rounding_tolerance is None
    ok = TransactionMapping.model_validate({**base, "rounding_tolerance": {"schema_version": 1, "minor_units": 5}})
    assert ok.rounding_tolerance.minor_units == 5
    with pytest.raises(ValidationError):
        TransactionMapping.model_validate({**base, "rounding_tolerance": {"schema_version": 1, "minor_units": 6}})


def test_an_exact_match_is_untouched_by_a_configured_tolerance():
    result = _case("120.00", "20.00")
    assert (result["status"], result["reason"]) == ("matched", "all_amounts_agree")
    assert "adjustments" not in result
