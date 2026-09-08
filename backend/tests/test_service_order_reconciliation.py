"""A zero total is insufficient: prove the complete service/RMA offset."""

from copy import deepcopy

import pytest

from app.services.transaction_ops.order_reconciliation import reconcile_order
from tests.test_order_balance_reconciliation import evidence


def service_evidence():
    source, target, config, refunds = evidence()
    source["orders"][0].update(
        id="10",
        order_type="service",
        total="0",
        item_total="80",
        ship_total="10",
        included_tax_total="0",
        additional_tax_total="2",
        tax_total="2",
        adjustment_total="-90",
        payment_total="0",
        deposit_amount="0",
        total_applicable_store_credit="0",
        order_total_after_store_credit="0",
        payments=[],
        adjustments=[
            dict(
                id="21",
                adjustable_id="10",
                adjustable_type="Spree::Order",
                source_type=None,
                label="RMA",
                finalized=True,
                amount="-92",
            )
        ],
        line_items=[
            dict(
                id="11",
                quantity="2",
                price="40",
                total="80",
                adjustments=[
                    dict(
                        id="22",
                        adjustable_id="11",
                        adjustable_type="Spree::LineItem",
                        source_type="Spree::TaxRate",
                        finalized=True,
                        amount="0",
                    )
                ],
            )
        ],
        shipments=[
            dict(
                id="12",
                cost="10",
                adjustments=[
                    dict(
                        id="23",
                        adjustable_id="12",
                        adjustable_type="Spree::Shipment",
                        source_type="Spree::TaxRate",
                        finalized=True,
                        amount="2",
                    )
                ],
            )
        ],
    )
    target["orders"][0]["header"].update(
        total="0",
        subtotal="80",
        taxTotal="0",
        shippingCost="0",
        discountTotal="-80",
        discountRate="-100",
        discountItem={"id": "99"},
        custbody_fw_solidus_order_total="0",
        custbody_fw_solidus_tax_amount="2",
    )
    return source, target, config, refunds


def test_fully_offset_service_shipping_tax_matches_with_original_evidence():
    source, target, config, refunds = service_evidence()
    before = deepcopy((source, target, refunds))
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched"
    assert result["reason"] == "verified_adjustments_agree"
    assert result["amounts"]["tax"] == {"source": "0.00", "target": "0.00", "delta": "0.00"}
    assert result["original_amounts"]["tax"] == {"source": "2.00", "target": "0.00", "delta": "2.00"}
    proof = result["adjustments"][0]
    assert proof["kind"] == "service_order_full_offset"
    assert proof["source_adjustment_id"] == "21"
    assert proof["source_adjustment_amount"] == "-92.00"
    assert (source, target, refunds) == before


@pytest.mark.parametrize(
    "change",
    [
        lambda s, t, r: s.update(order_type="sale"),
        lambda s, t, r: s.pop("order_type"),
        lambda s, t, r: s["adjustments"][0].update(amount="-90"),
        lambda s, t, r: s["adjustments"][0].update(finalized=False),
        lambda s, t, r: s["adjustments"][0].update(eligible=False),
        lambda s, t, r: s["adjustments"][0].update(adjustable_id="999"),
        lambda s, t, r: s["adjustments"][0].update(label="Promotion", source_type="Spree::PromotionAction"),
        lambda s, t, r: s["adjustments"].append(deepcopy(s["adjustments"][0])),
        lambda s, t, r: s.update(adjustment_total="-89"),
        lambda s, t, r: s.update(total_applicable_store_credit="2"),
        lambda s, t, r: s.update(payment_total="2"),
        lambda s, t, r: s.update(payments=[{"amount": "2"}]),
        lambda s, t, r: s["line_items"][0].update(total="79"),
        lambda s, t, r: s["line_items"][0]["adjustments"][0].update(amount="1"),
        lambda s, t, r: s["shipments"][0]["adjustments"][0].update(amount="1"),
        lambda s, t, r: s["shipments"][0]["adjustments"][0].update(finalized=False),
        lambda s, t, r: s.pop("shipments"),
        lambda s, t, r: t.update(discountTotal="-79"),
        lambda s, t, r: t.update(discountRate="-99"),
        lambda s, t, r: t.update(shippingCost="1"),
        lambda s, t, r: t.update(handlingCost="1"),
        lambda s, t, r: t.update(custbody_fw_solidus_tax_amount="1"),
        lambda s, t, r: t.update(taxTotal="1"),
    ],
)
def test_zero_order_does_not_hide_unproven_tax(change):
    source, target, config, refunds = service_evidence()
    change(source["orders"][0], target["orders"][0]["header"], refunds)
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] != "matched"
    assert "adjustments" not in result


def test_service_adjustment_cannot_hide_missing_or_mismatched_refunds():
    source, target, config, refunds = service_evidence()
    refunds["target"]["complete"] = False
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "incomplete"
    refunds["target"].update(complete=True, amount="1")
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "difference"


def test_shipping_vat_already_in_price_is_offset_once_by_service_rma():
    source, target, config, refunds = service_evidence()
    source["orders"][0].update(included_tax_total="2", additional_tax_total="0")
    source["orders"][0]["adjustments"][0]["amount"] = "-90"
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched"
    assert result["amounts"]["tax"]["source"] == "0.00"
    assert result["original_amounts"]["tax"]["source"] == "2.00"


def test_inclusive_tax_cannot_exceed_the_shipping_price():
    source, target, config, refunds = service_evidence()
    source["orders"][0].update(included_tax_total="12", additional_tax_total="0", tax_total="12")
    source["orders"][0]["adjustments"][0]["amount"] = "-90"
    source["orders"][0]["shipments"][0]["adjustments"][0]["amount"] = "12"
    target["orders"][0]["header"]["custbody_fw_solidus_tax_amount"] = "12"
    assert reconcile_order(source, target, config, refunds=refunds)["status"] == "difference"


def test_full_manual_service_offset_is_proven_by_amounts_not_a_free_text_label():
    source, target, config, refunds = service_evidence()
    source["orders"][0]["adjustments"][0]["label"] = "Manual service adjustment"
    result = reconcile_order(source, target, config, refunds=refunds)
    assert result["status"] == "matched"
    assert "Manual service adjustment" not in str(result)
