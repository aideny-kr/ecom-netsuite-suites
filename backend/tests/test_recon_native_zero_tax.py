"""A tax-free sales order whose record API header leaves out taxTotal (class C24, 2026-10-25).

NetSuite's record API drops an empty summary money field, so a tax-free Inc order arrives with
no taxTotal and its tax stayed a missing metric although SuiteQL transaction.taxtotal is 0 and
the totals and refunds match. Absence is not zero: the zero counts only when the header's own
arithmetic leaves no room for tax, NetSuite's SuiteQL figures for the same record say zero tax
at the same total, and the source charged no tax either. Every other shape keeps tax missing.
"""

from copy import deepcopy

import pytest

from app.services.transaction_ops.order_reconciliation import reconcile_order

REFERENCE = "R900000401"


def evidence():
    source = {
        "source": "framework",
        "scope": "order",
        "page_complete": True,
        "read_at": "2026-10-25T06:00:00Z",
        "orders": [
            {
                "number": REFERENCE,
                "currency": "USD",
                "business_entity": "Framework Inc",
                "total": "500.00",
                "included_tax_total": "0.00",
                "additional_tax_total": "0.00",
            }
        ],
    }
    target = {
        "provider": "netsuite",
        "scope": {"account_id": "6738075", "subsidiary_id": "1"},
        "observed_at": "2026-10-25T06:00:01Z",
        "lookup": {"complete": True, "count": 1},
        "orders": [
            {
                "record_id": "900401",
                "order_reference": REFERENCE,
                "header_complete": True,
                # No taxTotal key: the record API left it out.
                "header": {
                    "id": "900401",
                    "subsidiary": {"id": "1"},
                    "currency": {"id": "1"},
                    "subtotal": "500.00",
                    "total": "500.00",
                },
                # From the SuiteQL identity row (wire types: id and amounts are JSON numbers).
                "native_header": {"id": "900401", "taxtotal": "0", "total": "500"},
                "currency_metadata": {"id": "1", "symbol": "USD", "currencyPrecision": 2},
            }
        ],
    }
    config = {
        "netsuite_account_id": "6738075",
        "subsidiary_id": "1",
        "mapping_json": {"business_entity_subsidiaries": {"Framework Inc": "1"}},
    }
    refund = {"complete": True, "order_reference": REFERENCE, "currency": "USD", "amount": "0.00"}
    return source, target, config, {"source": deepcopy(refund), "target": deepcopy(refund)}


def test_an_absent_tax_total_is_zero_when_native_tax_and_the_header_arithmetic_prove_it():
    source, target, config, refunds = evidence()
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched" and result["reason"] == "verified_adjustments_agree"
    assert result["missing_metrics"] == []
    assert result["target_tax_basis"] == "native_taxtotal"
    assert result["amounts"]["tax"] == {"source": "0.00", "target": "0.00", "delta": "0.00"}
    (adjustment,) = result["adjustments"]
    assert adjustment == {
        "kind": "native_zero_tax",
        "status": "verified",
        "basis": (
            "the record API left out taxTotal; NetSuite's SuiteQL tax total for the same record is zero at "
            "the same total, and the header's own arithmetic leaves no room for tax"
        ),
        "record_id": "900401",
        "native_taxtotal": "0.00",
        "native_total": "500.00",
        "arithmetic": {
            "total": "500.00",
            "subtotal": "500.00",
            "discountTotal": None,
            "shippingCost": None,
            "handlingCost": None,
        },
    }
    # The header never showed a tax figure; the record of what was compared says so.
    assert result["original_amounts"]["tax"] == {"source": "0.00", "target": None, "delta": None}


def test_the_signed_discount_is_added_as_it_is_with_shipping():
    source, target, config, refunds = evidence()
    target["orders"][0]["header"].update(discountTotal="-50.00", shippingCost="25.00", subtotal="525.00")
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched"
    assert result["adjustments"][0]["arithmetic"] == {
        "total": "500.00",
        "subtotal": "525.00",
        "discountTotal": "-50.00",
        "shippingCost": "25.00",
        "handlingCost": None,
    }


def _header(target):
    return target["orders"][0]["header"]


def _native(target):
    return target["orders"][0]["native_header"]


@pytest.mark.parametrize(
    "change",
    [
        # A hidden 5.00: the header total matches the source, but the subtotal leaves room for tax.
        lambda s, t: _header(t).update(subtotal="495.00"),
        # NetSuite's own tax total is not zero; the arithmetic holds, so this is the only reason.
        lambda s, t: _native(t).update(taxtotal="5.00"),
        # The native row belongs to another record.
        lambda s, t: _native(t).update(id="900402"),
        lambda s, t: _native(t).pop("id"),
        # SuiteQL drops a null column: no native tax figure was read.
        lambda s, t: _native(t).pop("taxtotal"),
        lambda s, t: t["orders"][0].pop("native_header"),
        lambda s, t: t["orders"][0].update(native_header="0"),
        lambda s, t: _native(t).update(taxtotal=True),
        lambda s, t: _native(t).update(taxtotal="zero"),
        # The native total is not the header's total (an edit between the two reads).
        lambda s, t: _native(t).update(total="505.00"),
        lambda s, t: _native(t).pop("total"),
        # Present but null is malformed evidence, not a zero (refund_adjustments precedent).
        lambda s, t: _header(t).update(taxTotal=None),
        lambda s, t: _header(t).update(shippingCost=None),
        lambda s, t: _header(t).update(discountTotal=None),
        lambda s, t: _header(t).update(handlingCost=None),
        # abs(discountTotal) would let this through: 100.00 + |-10.00| = 110.00 hides 20.00 of tax.
        lambda s, t: (
            s["orders"][0].update(total="110.00"),
            _header(t).update(subtotal="100.00", discountTotal="-10.00", total="110.00"),
            _native(t).update(total="110.00"),
        ),
        # Only the discount may be negative, and every present amount must be exact at the precision.
        lambda s, t: _header(t).update(subtotal="525.00", shippingCost="-25.00"),
        lambda s, t: _header(t).update(subtotal="499.995", shippingCost="0.005"),
        lambda s, t: _header(t).pop("subtotal"),
        lambda s, t: _header(t).update(subtotal="abc"),
    ],
)
def test_tax_stays_missing_unless_the_zero_is_proven(change):
    source, target, config, refunds = evidence()
    change(source, target)
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "incomplete" and result["missing_metrics"] == ["tax"]
    assert result["amounts"]["tax"]["target"] is None
    assert result["amounts"]["order_total"]["delta"] == "0.00"
    assert "adjustments" not in result and "target_tax_basis" not in result


def test_a_taxed_source_never_compares_against_a_zero_no_record_showed():
    source, target, config, refunds = evidence()
    source["orders"][0]["additional_tax_total"] = "5.00"
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "incomplete" and result["missing_metrics"] == ["tax"]
    assert result["amounts"]["tax"] == {"source": "5.00", "target": None, "delta": None}


@pytest.mark.parametrize("native_tax", ["0", "5.00"])
def test_a_tax_total_the_record_api_returned_keeps_the_plain_comparison(native_tax):
    source, target, config, refunds = evidence()
    _header(target)["taxTotal"] = "0.00"
    _native(target)["taxtotal"] = native_tax
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched" and result["reason"] == "all_amounts_agree"
    assert "adjustments" not in result and "target_tax_basis" not in result


def test_the_native_figures_are_stored_with_the_record_they_cleared():
    # A reconciled case never reopens (migration 117), so the evidence behind a clear stays with it.
    from datetime import datetime, timezone

    from app.services.transaction_ops.normalization import TransactionMapping
    from app.services.transaction_ops.runner import build_report
    from tests.test_transaction_ops_netsuite_actions import target as target_example
    from tests.test_transaction_ops_runner import source_order

    now = datetime.now(timezone.utc)
    mapping = {"reference_field": "tranid", "currency_minor_units": {"EUR": 2}}
    mapping["business_entity_subsidiaries"] = {"legacy": "3"}
    source = source_order()
    source["read_at"] = now.isoformat()
    source["orders"][0].update(number="R123456789", currency="EUR", updated_at=now.isoformat())
    order = target_example()
    native = {"id": order["record_id"], "taxtotal": "0", "total": "110"}
    order["native_header"] = native
    targets = {
        "provider": "netsuite",
        "orders": [order],
        "lookup": {"complete": True},
        "observed_at": now.isoformat(),
        "scope": {"account_id": "6738075-sb1", "subsidiary_id": "3"},
    }
    config = {"netsuite_account_id": "6738075_SB1", "subsidiary_id": "3", "record_type": "salesorder"}
    report = build_report(source, targets, config, TransactionMapping.model_validate(mapping), now=now)
    assert report["netsuite_provenance"]["records"][0]["native_header"] == native
