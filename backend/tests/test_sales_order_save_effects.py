from copy import deepcopy

import pytest

from app.services.transaction_ops.sales_order_alignment import snapshot
from app.services.transaction_ops.sales_order_save_effects import comparison_snapshot


def records():
    before = {
        "id": "90",
        "total": "1593.0",
        "subtotal": "1593.0",
        "totalCostEstimate": "1243.75",
        "estGrossProfit": "349.25",
        "estGrossProfitPercent": "21.924",
        "custbody_esc_last_modified_date": "2026-09-05",
        "lastModifiedDate": "2026-09-05T13:07:00Z",
        "custbody_other": "protected",
        "item": {"items": [{"line": 1, "quantity": "1", "amount": "1593"}]},
    }
    after = deepcopy(before)
    after.update(
        total="1425.93",
        discountTotal="-167.07",
        discountRate="-167.07",
        discountItem={"id": "1471"},
        estGrossProfit="182.18",
        estGrossProfitPercent="12.7762",
        custbody_esc_last_modified_date="2026-09-13",
        lastModifiedDate="2026-09-14T02:42:00Z",
    )
    return before, after


def valid(before, after, account="6738075"):
    baseline = snapshot(before, amendable=True)
    observed = comparison_snapshot(after, baseline, account)
    return observed["save_effects_valid"] and observed.get("comparison_native_digest") == baseline["native_digest"]


def test_expected_save_outputs_are_verified_but_still_stale_preapproval():
    before, after = records()
    original = deepcopy(after)
    assert valid(before, after)
    assert snapshot(before, amendable=True)["native_digest"] != snapshot(after, amendable=True)["native_digest"]
    assert after == original


@pytest.mark.parametrize(
    "key,value",
    [
        ("estGrossProfit", "182.19"),
        ("estGrossProfitPercent", "12.7763"),
        ("totalCostEstimate", "1243.76"),
        ("custbody_other", "changed"),
        ("custbody_esc_last_modified_date", "2026-09-14"),
        ("custbody_esc_last_modified_date", "garbage"),
        ("lastModifiedDate", "2026-09-14T02:42:00"),
        ("estGrossProfit", "NaN"),
        ("estGrossProfitPercent", "Infinity"),
        ("total", "0"),
    ],
)
def test_wrong_derived_or_unrelated_changes_fail(key, value):
    before, after = records()
    after[key] = value
    assert not valid(before, after)


def test_changed_cost_cannot_be_hidden_by_consistent_recalculated_profit():
    before, after = records()
    after.update(totalCostEstimate="1243.76", estGrossProfit="182.17", estGrossProfitPercent="12.7755")
    assert not valid(before, after)


def test_stale_derived_outputs_are_not_treated_as_unchanged_evidence():
    before, after = records()
    after.update(estGrossProfit=before["estGrossProfit"], estGrossProfitPercent=before["estGrossProfitPercent"])
    assert not valid(before, after)
    before, after = records()
    after["custbody_esc_last_modified_date"] = before["custbody_esc_last_modified_date"]
    assert not valid(before, after)


@pytest.mark.parametrize("field", ["estGrossProfit", "estGrossProfitPercent", "custbody_esc_last_modified_date"])
def test_added_or_removed_save_fields_fail(field):
    before, after = records()
    after.pop(field)
    assert not valid(before, after)
    before.pop(field)
    after[field] = "1"
    assert not valid(before, after)


def test_scope_nested_fields_and_item_quantities_remain_protected():
    before, after = records()
    assert not valid(before, after, "other-account")
    after["item"]["items"][0]["quantity"] = "2"
    assert not valid(before, after)
    before, after = records()
    after["item"]["items"][0]["estGrossProfit"] = "20"
    assert not valid(before, after)


def test_legacy_opaque_hash_cannot_accept_save_effect_changes():
    before, after = records()
    baseline = snapshot(before, amendable=True)
    baseline.pop("save_effects")
    assert not comparison_snapshot(after, baseline, "6738075")["save_effects_valid"]


@pytest.mark.asyncio
async def test_recovery_baseline_must_reproduce_approved_native_hash():
    from app.services.transaction_ops.sales_order_alignment import verify_after

    before, _ = records()
    approved = snapshot(before, amendable=True)
    approved.pop("save_effects")
    before["custbody_other"] = "tampered"
    with pytest.raises(ValueError, match="does not match approved"):
        await verify_after(None, None, {"scope": {}, "connection_id": "x", "before": approved}, before_raw=before)
