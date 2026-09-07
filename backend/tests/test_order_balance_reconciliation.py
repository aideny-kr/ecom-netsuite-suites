"""Order reconciliation must prove all totals without confusing unknown and zero."""

from copy import deepcopy

import pytest

from app.services.transaction_ops.order_reconciliation import reconcile_order


def evidence():
    source = {
        "source": "framework",
        "scope": "order",
        "page_complete": True,
        "read_at": "2026-09-06T06:00:00Z",
        "orders": [
            {
                "number": "R123456789",
                "currency": "GBP",
                "business_entity": "UK",
                "total": "120.00",
                "included_tax_total": "20.00",
                "additional_tax_total": "0.00",
            }
        ],
    }
    target = {
        "provider": "netsuite",
        "scope": {"account_id": "123-sb1", "subsidiary_id": "5"},
        "observed_at": "2026-09-06T06:00:01Z",
        "lookup": {"complete": True, "count": 1},
        "orders": [
            {
                "order_reference": "R123456789",
                "header_complete": True,
                "header": {
                    "id": "77",
                    "subsidiary": {"id": "5"},
                    "currency": {"id": "2"},
                    "total": "120.00",
                    "taxTotal": "20.00",
                },
                "currency_metadata": {"id": "2", "symbol": "GBP", "currencyPrecision": 2},
            }
        ],
    }
    config = {
        "netsuite_account_id": "123_SB1",
        "subsidiary_id": "5",
        "mapping_json": {"business_entity_subsidiaries": {"UK": "5"}},
    }
    refund = {"complete": True, "order_reference": "R123456789", "currency": "GBP", "amount": "0.00"}
    return source, target, config, {"source": deepcopy(refund), "target": deepcopy(refund)}


def test_order_total_tax_and_refunds_all_agree():
    source, target, config, refunds = evidence()
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched"
    assert set(result["amounts"]) == {"order_total", "tax", "refunds"}
    assert all(row["delta"] == "0.00" for row in result["amounts"].values())


def test_unverified_target_provider_cannot_prove_a_match():
    source, target, config, refunds = evidence()
    target["provider"] = "model_guess"
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "incomplete"


@pytest.mark.parametrize(
    "entity,expected", [(None, "matched"), ("legacy", "incomplete"), ({"id": "legacy"}, "incomplete")]
)
def test_only_explicit_null_uses_a_configured_legacy_entity(entity, expected):
    source, target, config, refunds = evidence()
    source["orders"][0]["business_entity"] = entity
    config["mapping_json"]["business_entity_subsidiaries"] = {"legacy": "5"}
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == expected


def test_partial_refunds_are_compared_separately_from_gross_order_total():
    source, target, config, refunds = evidence()
    refunds["source"]["amount"] = "60.00"
    refunds["target"]["amount"] = "40.00"
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "difference"
    assert result["amounts"]["refunds"]["delta"] == "20.00"
    assert result["amounts"]["order_total"]["delta"] == "0.00"


def test_vat_is_not_added_to_the_already_tax_inclusive_order_total():
    source, target, config, refunds = evidence()
    target["orders"][0]["header"].update(total="119.99", taxTotal="19.99")
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["amounts"]["tax"]["delta"] == "0.01"
    assert result["amounts"]["order_total"]["delta"] == "0.01"


@pytest.mark.parametrize("missing", [None, {"source": {"complete": False}, "target": {"complete": False}}])
def test_unknown_refunds_cannot_become_zero_or_a_match(missing):
    source, target, config, _ = evidence()
    result = reconcile_order(source, target, config, refunds=missing)
    assert result["status"] == "incomplete"
    assert result["amounts"]["refunds"] == {"source": None, "target": None, "delta": None}


@pytest.mark.parametrize("kind", ["currency", "subsidiary", "reference", "account", "incomplete_lookup", "duplicate"])
def test_identity_and_coverage_fail_closed(kind):
    source, target, config, refunds = evidence()
    expected = "incomplete"
    if kind == "currency":
        target["orders"][0]["currency_metadata"]["symbol"] = "EUR"
        expected = "currency_mismatch"
    elif kind == "subsidiary":
        target["orders"][0]["header"]["subsidiary"]["id"] = "6"
    elif kind == "reference":
        target["orders"][0]["order_reference"] = "R000000000"
    elif kind == "account":
        target["scope"]["account_id"] = "999"
    elif kind == "incomplete_lookup":
        target["lookup"]["complete"] = False
    else:
        target["orders"].append(deepcopy(target["orders"][0]))
        target["lookup"]["count"] = 2
        expected = "ambiguous"
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == expected
    assert all(v["delta"] is None for v in result["amounts"].values())


def test_complete_empty_lookup_proves_missing_order():
    source, target, config, refunds = evidence()
    target.update(orders=[], lookup={"complete": True, "count": 0})
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "missing_in_netsuite"


@pytest.mark.parametrize("value", [None, True, 120.0, "NaN", "120.001"])
def test_unknown_or_invalid_money_cannot_match(value):
    source, target, config, refunds = evidence()
    source["orders"][0]["total"] = value
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "incomplete"
    assert result["amounts"]["order_total"]["delta"] is None


def test_refund_from_another_order_or_currency_cannot_cover_this_order():
    source, target, config, refunds = evidence()
    refunds["source"].update(currency="EUR", order_reference="R000000000")
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "incomplete"
