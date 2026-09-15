"""Approved transaction writes retain exact intent and fail closed on drift."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.services.transaction_ops import netsuite_actions as mod


def source(**changes):
    result = {
        "system": "framework",
        "account_id": "frame.work",
        "record_type": "order",
        "record_id": "123",
        "order_reference": "R123456789",
        "subsidiary_id": "3",
        "currency": "EUR",
        "currency_minor_unit": 2,
        "amount_basis": "transaction",
        "status": "confirmed",
        "authoritative": True,
        "lines_complete": True,
        "tax_complete": True,
        "updated_at": "2026-09-04T00:00:00Z",
        "total": "122.00",
        "subtotal": "100.00",
        "tax": "20.00",
        "shipping": "2.00",
        "shipping_tax": "0",
        "discount": "0",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "lines": [{"key": "line:11", "quantity": "2", "net": "100.00", "tax": "20.00"}],
        "tax_details": [
            {"key": "line:11:tax:610", "basis": "100", "rate": "0.2", "amount": "20", "rounding": "half_up"}
        ],
    }
    return result | changes


def target():
    return {
        "record_type": "salesOrder",
        "record_id": "63",
        "order_reference": "R123456789",
        "complete": True,
        "version": "2026-09-04T12:00:00Z",
        "header": {
            "entity": {"id": "40"},
            "subsidiary": {"id": "3"},
            "currency": {"id": "4"},
            "tranDate": "2026-09-04",
            "exchangeRate": "1.1",
            "orderStatus": {"id": "B"},
            "lastModifiedDate": "2026-09-04T12:00:00Z",
            "total": "110",
            "subtotal": "90",
            "taxTotal": "18",
            "shippingCost": "2",
            "handlingCost": "0",
            "discountTotal": "0",
            "custbody_fw_solidus_order_total": "110",
        },
        "currency_metadata": {"id": "4", "symbol": "EUR", "currencyPrecision": 2},
        "periods": {
            "complete": True,
            "items": [
                {
                    "id": "99",
                    "closed": "F",
                    "alllocked": "F",
                    "arlocked": "F",
                    "aplocked": "F",
                    "isadjust": "F",
                    "startdate": "2026-09-01",
                    "enddate": "2026-09-30",
                }
            ],
        },
        "lines": [
            {
                "line": 7,
                "lineUniqueKey": "12345",
                "item": {"id": "600"},
                "quantity": "2",
                "quantityFulfilled": "0",
                "quantityBilled": "0",
                "isClosed": False,
                "rate": "45",
                "amount": "90",
                "custcol_fw_solidus_line_id": 11,
                "custcol_fw_vat_amount": "18",
                "taxCode": {"id": "610"},
                "taxRate1": "20",
            }
        ],
        "tax_details": [],
    }


def test_correction_uses_original_line_id_and_only_explicit_money_changes():
    plan = mod.prepare_correction(target(), source())
    assert plan.action == "correct_amounts"
    assert plan.before_json["record_id"] == "63"
    assert plan.before_json["entity"] == "40"
    assert plan.before_json["lines"][0]["line"] == "7"
    assert plan.after_json["line_changes"] == [
        {"line": "7", "fields": {"rate": "50", "amount": "100", "custcol_fw_vat_amount": "20"}}
    ]
    assert plan.after_json["body_changes"] == {"custbody_fw_solidus_order_total": "122"}
    assert plan.after_json["expected_totals"] == {
        "total": "122",
        "subtotal": "100",
        "taxtotal": "20",
        "shippingcost": "2",
        "discounttotal": "0",
    }
    assert "replaceAll" not in str(plan.after_json)
    assert "exchangerate" not in plan.after_json["body_changes"]


def test_prepared_intent_cannot_be_mutated_through_returned_json():
    plan = mod.prepare_correction(target(), source())
    before = plan.before_json
    before["lines"][0]["amount"] = "999"
    assert plan.before_json["lines"][0]["amount"] == "90"
    with pytest.raises((AttributeError, TypeError)):
        plan.action = "sync_missing_order"


@pytest.mark.parametrize(
    "change",
    [
        {"currency": "USD"},
        {"subsidiary_id": "4"},
        {"status": "fulfilled"},
        {"tax_complete": False},
        {"lines_complete": False},
        {"authoritative": False},
        {"amount_basis": "base"},
        {"total": 122.0},
    ],
)
def test_correction_rejects_incomplete_or_cross_scope_source(change):
    with pytest.raises(mod.NetSuiteActionError):
        mod.prepare_correction(target(), source(**change))


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantityFulfilled", "1"),
        ("quantityBilled", "1"),
        ("isClosed", True),
        ("quantity", "3"),
        ("quantityBilled", None),
    ],
)
def test_correction_never_changes_fulfilled_billed_closed_or_different_quantity(field, value):
    evidence = target()
    evidence["lines"][0][field] = value
    with pytest.raises(mod.NetSuiteActionError):
        mod.prepare_correction(evidence, source())


@pytest.mark.parametrize("flag", ["closed", "alllocked", "arlocked", "aplocked", "isadjust"])
def test_correction_requires_one_open_nonadjustment_period(flag):
    evidence = target()
    evidence["periods"]["items"][0][flag] = "T"
    with pytest.raises(mod.NetSuiteActionError, match="period"):
        mod.prepare_correction(evidence, source())


def test_correction_refuses_added_removed_ambiguous_and_nonterminating_rate_lines():
    for evidence in (target(), target()):
        evidence["lines"].append(deepcopy(evidence["lines"][0]))
        with pytest.raises(mod.NetSuiteActionError):
            mod.prepare_correction(evidence, source())
    evidence = target()
    evidence["lines"][0]["quantity"] = "3"
    with pytest.raises(mod.NetSuiteActionError, match="rate"):
        mod.prepare_correction(evidence, source(lines=[{"key": "line:11", "quantity": "3", "net": "100", "tax": "20"}]))


def test_guard_endpoint_is_an_exact_saved_account_script_and_deployment():
    url = "https://6738075.restlets.api.netsuite.com/app/site/hosting/restlet.nl?script=customscript_ecom_tx_ops_guard&deploy=customdeploy_ecom_tx_ops_guard"
    assert mod.validate_guard_url(url, "6738075") == url
    for bad in (
        url.replace("6738075.", "evil."),
        url + "&redirect=https://evil.example",
        url.replace("https", "http"),
        url.replace("customscript_ecom_tx_ops_guard", "customscript_other"),
        url.replace(".com/", ".com:444/"),
    ):
        with pytest.raises(mod.NetSuiteActionError):
            mod.validate_guard_url(bad, "6738075")


def test_amounts_must_retain_decimal_precision():
    evidence = target()
    evidence["lines"][0]["rate"] = Decimal("45.123456789012")
    plan = mod.prepare_correction(evidence, source())
    assert plan.before_json["lines"][0]["rate"] == "45.123456789012"


@pytest.mark.parametrize("change", [{"account_id": "different-store"}, {"order_reference": "prefix-only"}])
def test_correction_requires_exact_framework_source_identity(change):
    evidence = target()
    if "order_reference" in change:
        evidence["order_reference"] = change["order_reference"]
    with pytest.raises(mod.NetSuiteActionError):
        mod.prepare_correction(evidence, source(**change))


def test_zero_source_tax_without_a_tax_rule_cannot_retain_a_nonzero_destination_rate():
    evidence = target()
    with pytest.raises(mod.NetSuiteActionError, match="tax"):
        mod.prepare_correction(
            evidence,
            source(
                total="102",
                tax="0",
                tax_details=[],
                lines=[{"key": "line:11", "quantity": "2", "net": "100", "tax": "0"}],
            ),
        )
