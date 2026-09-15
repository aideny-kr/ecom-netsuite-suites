from copy import deepcopy

import pytest

from app.services.transaction_ops.line_evidence import compare_source_lines


def inputs():
    source = {
        "number": "R123",
        "currency": "USD",
        "updated_at": "version1",
        "line_items": [
            {"id": "101", "price": "1200", "quantity": "1", "variant": {"sku": "MEM64", "price": "1600"}}
        ],
    }
    order = {
        "id": "10",
        "tranId": "R123",
        "record_type": "salesorder",
        "currency_code": "USD",
        "line_evidence": {
            "complete": True,
            "lines": [
                {
                    "line": 12,
                    "lineUniqueKey": "key12",
                    "custcol_fw_solidus_line_id": "101",
                    "custcol_fw_item_sku": "MEM64",
                    "rate": "1600",
                    "quantity": "1",
                }
            ],
        },
    }
    invoice = deepcopy(order)
    invoice.update(id="20", record_type="invoice")
    return source, {
        "verified_connection_scope": {"account_id": "123"},
        "sections": {"sales_order": order, "posting_documents": [invoice]},
    }


def test_order_line_price_is_compared_in_both_documents_not_current_catalog_price():
    source, evidence = inputs()
    result = compare_source_lines(source, evidence)
    assert len(result["changes"]) == 2
    assert {r["target_record_type"] for r in result["changes"]} == {"salesorder", "invoice"}
    assert all(r["unit_price_delta"] == "-400" for r in result["changes"])
    assert result["unverified"] == []
    assert "proposal" not in result


@pytest.mark.parametrize(
    "change", ["scope", "currency", "order", "duplicate_source", "duplicate_native", "sku", "incomplete"]
)
def test_ambiguous_lines_are_never_matched_by_sku_or_amount_alone(change):
    source, evidence = inputs()
    order = evidence["sections"]["sales_order"]
    evidence["sections"]["posting_documents"] = []
    if change == "scope":
        evidence.pop("verified_connection_scope")
    elif change == "currency":
        source["currency"] = "CAD"
    elif change == "order":
        source["number"] = "OTHER"
    elif change == "duplicate_source":
        source["line_items"] *= 2
    elif change == "duplicate_native":
        order["line_evidence"]["lines"] *= 2
    elif change == "sku":
        source["line_items"][0]["variant"]["sku"] = "OTHER"
    else:
        order["line_evidence"]["complete"] = False
    result = compare_source_lines(source, evidence)
    assert result["changes"] == []
    assert result["unverified"]


@pytest.mark.parametrize("price", [None, True, 1.2, "NaN", "Infinity", "bad", "1e30", "1e-30"])
def test_unknown_or_nonfinite_prices_remain_unverified(price):
    source, evidence = inputs()
    source["line_items"][0]["price"] = price
    result = compare_source_lines(source, evidence)
    assert result["changes"] == []
    assert result["unverified"]


def test_penny_changes_survive_exact_decimal_comparison():
    source, evidence = inputs()
    source["line_items"][0]["price"] = "1599.99"
    assert all(r["unit_price_delta"] == "-0.01" for r in compare_source_lines(source, evidence)["changes"])


def test_explicit_original_source_sku_corroborates_component_mapping():
    source, evidence = inputs()
    for doc in [evidence["sections"]["sales_order"], *evidence["sections"]["posting_documents"]]:
        line = doc["line_evidence"]["lines"][0]
        line["custcol_fw_original_ecom_sku"] = "MEM64"
        line["custcol_fw_item_sku"] = "ERP-COMPONENT"
    result = compare_source_lines(source, evidence)
    assert len(result["changes"]) == 2
    assert result["unverified"] == []


def test_conflicting_original_source_sku_is_not_overruled_by_component_sku():
    source, evidence = inputs()
    for doc in [evidence["sections"]["sales_order"], *evidence["sections"]["posting_documents"]]:
        doc["line_evidence"]["lines"][0]["custcol_fw_original_ecom_sku"] = "OTHER"
    result = compare_source_lines(source, evidence)
    assert not result["changes"]
    assert result["unverified"]


def test_changed_line_retains_exact_tax_observation_without_certifying_source():
    source, evidence = inputs()
    source["line_items"][0]["adjustments"] = [
        {
            "id": "3",
            "source_type": "Spree::TaxRate",
            "adjustable_type": "Spree::LineItem",
            "adjustable_id": "101",
            "amount": "120",
            "finalized": False,
        }
    ]
    for doc in [evidence["sections"]["sales_order"], *evidence["sections"]["posting_documents"]]:
        doc["line_evidence"]["lines"][0]["custcol_fw_vat_amount"] = "160"
    observation = compare_source_lines(source, evidence)["changes"][0]["tax_observation"]
    assert observation["delta"] == "-40"
    assert observation["source_adjustments"][0]["finalized"] is False
    assert "not native tax allocation" in observation["basis"]


def revision_inputs():
    source, evidence = inputs()
    source.update(
        state="complete",
        requires_review=False,
        adjustments=[],
        included_tax_total="0",
        ship_total="0",
        total="1320",
        tax_total="120",
        item_total="1200",
    )
    for document in [evidence["sections"]["sales_order"], *evidence["sections"]["posting_documents"]]:
        document.update(total="1760", taxTotal="160", subtotal="1600", shippingCost="0", discountTotal="0")
        document["line_evidence"]["lines"][0]["amount"] = "1600"
    return source, evidence


def test_repricing_basis_proves_exact_net_tax_gross_without_inventing_credit_or_policy():
    from app.services.transaction_ops.line_evidence import source_revision_delta

    source, evidence = revision_inputs()
    for record_id in ["10", "20"]:
        proof = source_revision_delta(source, evidence, record_id)
        assert (proof["net_delta"], proof["tax_delta"], proof["gross_delta"]) == ("-400", "-40", "-440")
        assert proof["source_line_ids"] == ["101"]
        assert "not certified" in proof["authority"]
        assert "proposed_fields" not in proof


@pytest.mark.parametrize(
    "change",
    [
        "quantity",
        "unknown_quantity",
        "source_subtotal",
        "target_subtotal",
        "extra_native_line",
        "shipping",
        "discount",
        "scope",
    ],
)
def test_incomplete_or_different_economics_do_not_fit_repricing_basis(change):
    from app.services.transaction_ops.line_evidence import source_revision_delta

    source, evidence = revision_inputs()
    doc = evidence["sections"]["sales_order"]
    if change == "quantity":
        source["line_items"][0]["quantity"] = "2"
    elif change == "unknown_quantity":
        source["line_items"][0]["quantity"] = None
    elif change == "source_subtotal":
        source["item_total"] = "999"
    elif change == "target_subtotal":
        doc["subtotal"] = "999"
    elif change == "extra_native_line":
        doc["line_evidence"]["lines"].append({"line": 2, "amount": "0"})
    elif change == "shipping":
        source["ship_total"] = "10"
    elif change == "discount":
        doc["discountTotal"] = "10"
    else:
        source["currency"] = "CAD"
    assert source_revision_delta(source, evidence, "10") is None
