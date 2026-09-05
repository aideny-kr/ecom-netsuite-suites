from copy import deepcopy
from decimal import Decimal

import pytest

from app.services.transaction_ops.normalization import (
    TransactionMapping,
    normalize_framework_order,
    normalize_netsuite_order,
)


def evidence(**changes):
    order = {
        "id": "101",
        "number": "R100000001",
        "state": "complete",
        "shipment_state": "shipped",
        "currency": "EUR",
        "updated_at": "2026-09-04T10:00:00Z",
        "total": "120",
        "item_total": "120",
        "ship_total": "0",
        "tax_total": "20",
        "included_tax_total": "20",
        "additional_tax_total": "0",
        "adjustment_total": "0",
        "adjustments": [],
        "line_items": [
            {
                "id": "11",
                "quantity": "1",
                "price": "120",
                "total": "120",
                "adjustments": [
                    {
                        "id": "99",
                        "source_type": "Spree::TaxRate",
                        "source_id": "7",
                        "adjustable_type": "Spree::LineItem",
                        "adjustable_id": "11",
                        "amount": "20",
                        "finalized": True,
                    }
                ],
            }
        ],
        "shipments": [{"id": "5", "cost": "0", "adjustments": []}],
    }
    order.update(changes)
    return {
        "source": "framework",
        "scope": "order",
        "read_at": "2026-09-04T11:00:00Z",
        "page_complete": True,
        "orders": [order],
    }


def mapping(**changes):
    values = {
        "reference_field": "tranid",
        "currency_minor_units": {"EUR": 2},
        "business_entity_subsidiaries": {"legacy": "1"},
        "tax_rules": {"7": {"rate": "0.2", "included": True, "rounding": "half_up", "netsuite_tax_id": "15"}},
    }
    values.update(changes)
    return TransactionMapping.model_validate(values)


def normalized(payload=None, config=None):
    return normalize_framework_order(
        payload or evidence(), mapping=config or mapping(), account_id="frame.work", subsidiary_id="1"
    )


def test_included_vat_preserves_transaction_currency_and_original_line_identity():
    s = normalized()
    assert s.currency == "EUR" and s.amount_basis == "transaction"
    assert s.total == Decimal("120") and s.subtotal == Decimal("100")
    assert s.tax == Decimal("20") and s.discount == 0
    assert s.lines[0].key == "line:11" and s.lines[0].net == 100 and s.lines[0].tax == 20
    assert s.tax_details[0].key == "line:11:tax:15"
    assert s.tax_details[0].included_gross_basis == 120
    assert s.lines_complete and s.tax_complete
    assert s.status == "fulfilled"


def test_additional_tax_is_not_subtracted_from_the_price():
    p = evidence(item_total="100", included_tax_total="0", additional_tax_total="20", adjustment_total="20")
    p["orders"][0]["line_items"][0]["price"] = "100"
    s = normalized(
        p, mapping(tax_rules={"7": {"rate": "0.2", "included": False, "rounding": "half_up", "netsuite_tax_id": "15"}})
    )
    assert s.subtotal == 100 and s.total == 120
    assert s.tax_details[0].included_gross_basis is None


def test_missing_tax_rule_keeps_observed_amounts_but_never_invents_rate():
    s = normalized(config=mapping(tax_rules={}))
    assert s.total == 120 and s.tax == 20 and s.lines[0].tax == 20
    assert s.tax_details[0].rate is None
    assert not s.tax_complete


def test_many_source_rates_to_one_tax_item_preserve_components_without_aborting():
    payload = evidence()
    adjustments = payload["orders"][0]["line_items"][0]["adjustments"]
    adjustments.append({**adjustments[0], "id": "100", "source_id": "8", "amount": "0"})
    rules = mapping().model_dump()["tax_rules"]
    rules["8"] = {**rules["7"], "rate": "0"}
    snapshot = normalized(payload, mapping(tax_rules=rules))
    assert len(snapshot.tax_details) == 2
    assert len({item.key for item in snapshot.tax_details}) == 2
    assert sum(item.amount for item in snapshot.tax_details) == 20
    assert not snapshot.tax_complete
    adjustments.reverse()
    assert sorted(
        (item.key, item.amount) for item in normalized(payload, mapping(tax_rules=rules)).tax_details
    ) == sorted((item.key, item.amount) for item in snapshot.tax_details)


def test_currency_precision_is_explicit_not_a_two_decimal_default():
    assert normalized(evidence(currency="JPY")).currency_minor_unit is None
    assert normalized(evidence(currency="JPY"), mapping(currency_minor_units={"JPY": 0})).currency_minor_unit == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(page_complete=False),
        lambda p: p.update(scope="updated_orders"),
        lambda p: p.update(orders=p["orders"] * 2),
        lambda p: p["orders"][0].update(total=120.0),
        lambda p: p["orders"][0].update(line_items=p["orders"][0]["line_items"] * 2),
    ],
)
def test_incomplete_ambiguous_or_lossy_inputs_are_rejected(mutation):
    p = deepcopy(evidence())
    mutation(p)
    with pytest.raises(ValueError):
        normalized(p)


def test_unfinalized_tax_is_not_complete_evidence():
    p = evidence()
    p["orders"][0]["line_items"][0]["adjustments"][0]["finalized"] = False
    assert not normalized(p).tax_complete


def test_unknown_adjustments_do_not_become_zero_discount():
    p = evidence()
    p["orders"][0]["adjustments"] = [{"id": "100", "source_type": "Promotion", "amount": "-10"}]
    s = normalized(p)
    assert s.discount is None and not s.tax_complete


def test_tax_adjustment_must_belong_to_its_source_line():
    p = evidence()
    p["orders"][0]["line_items"][0]["adjustments"][0]["adjustable_id"] = "different"
    assert not normalized(p).tax_complete


def test_missing_adjustments_array_cannot_claim_zero_tax():
    p = evidence()
    del p["orders"][0]["shipments"][0]["adjustments"]
    assert not normalized(p).tax_complete


def test_missing_line_adjustments_keep_line_tax_and_net_unknown():
    p = evidence()
    del p["orders"][0]["line_items"][0]["adjustments"]
    s = normalized(p)
    assert s.lines[0].tax is None and s.lines[0].net is None


def test_explicit_included_flag_cannot_contradict_mapping():
    p = evidence()
    p["orders"][0]["line_items"][0]["adjustments"][0]["included"] = False
    assert not normalized(p).tax_complete


def test_eligible_tax_is_recognized_when_not_yet_finalized():
    p = evidence()
    p["orders"][0]["line_items"][0]["adjustments"][0].update(finalized=False, eligible=True)
    assert normalized(p).tax_complete


def test_header_tax_contradiction_remains_visible_and_blocks_completeness():
    s = normalized(evidence(tax_total="21"))
    assert s.tax == 21 and s.lines[0].tax == 20 and not s.tax_complete


def test_mapping_rejects_sql_and_implied_currency_subsidiary_policies():
    with pytest.raises(ValueError):
        mapping(reference_field="tranid OR 1=1")
    with pytest.raises(ValueError):
        mapping(currency_minor_units={"JPY": "2"})


def test_new_mapping_defaults_to_detection_and_requires_explicit_create_identity():
    assert mapping().action_mode == "detect_only"
    assert mapping().netsuite_create is None
    with pytest.raises(ValueError):
        mapping(netsuite_create={"schema_version": 1, "tax_mode": "legacy_tax_codes"})
    valid = mapping(
        action_mode="propose_actions",
        netsuite_create={"schema_version": 1, "external_id_prefix": "framework_", "tax_mode": "legacy_tax_codes"},
    )
    assert valid.netsuite_create.external_id_prefix == "framework_"


def test_source_business_entity_must_match_explicit_subsidiary_mapping():
    assert normalized().subsidiary_id == "1"
    assert normalized(evidence(business_entity={"id": "europe"})).subsidiary_id is None
    assert normalized(config=mapping(business_entity_subsidiaries={})).subsidiary_id is None


def ns_order():
    return {
        "record_type": "salesOrder",
        "record_id": "20",
        "order_reference": "R100000001",
        "complete": True,
        "version": "2026-09-04T10:00:00Z",
        "header": {
            "id": "20",
            "currency": {"id": "4", "refName": "Euro"},
            "subsidiary": {"id": "1"},
            "status": {"id": "G"},
            "subtotal": "100",
            "total": "120",
            "taxTotal": "20",
            "discountTotal": "0",
            "shippingCost": "0",
            "exchangeRate": "1.055585",
            "taxRate": "20.001",
            "taxItem": {"id": "15"},
        },
        "currency_metadata": {"id": "4", "symbol": "EUR", "currencyPrecision": 2},
        "lines": [
            {
                "line": 63,
                "custcol_fw_solidus_line_id": "11",
                "quantity": "1",
                "amount": "100",
                "custcol_fw_vat_amount": "20",
                "taxDetailsReference": "L63",
            }
        ],
        "tax_details": None,
    }


def target(raw=None, config=None):
    return normalize_netsuite_order(
        raw or ns_order(), mapping=config or mapping(), account_id="6738075", observed_at="2026-09-04T11:00:00Z"
    )


def test_netsuite_preserves_transaction_amounts_without_multiplying_exchange_rate():
    s = target()
    assert s.total == 120 and s.tax == 20 and s.subtotal == 100
    assert s.currency == "EUR" and s.currency_minor_unit == 2
    assert s.lines[0].key == "line:11" and s.lines[0].tax == 20
    assert not s.tax_complete  # Legacy SOLIDUS effective rate isn't a per-line statutory rate.


def test_netsuite_currency_name_does_not_stand_in_for_iso_metadata():
    raw = ns_order()
    raw["currency_metadata"] = None
    assert target(raw).currency is None


def test_netsuite_complete_suitetax_detail_maps_by_original_framework_line():
    raw = ns_order()
    raw["tax_details"] = [
        {"taxDetailsReference": "L63", "taxCode": {"id": "15"}, "taxBasis": "100", "taxRate": "20", "taxAmount": "20"}
    ]
    s = target(raw, mapping(netsuite_tax_rounding="half_up"))
    assert s.tax_complete and s.shipping_tax == 0
    assert s.tax_details[0].key == "line:11:tax:15" and s.tax_details[0].rate == Decimal("0.2")


def test_netsuite_missing_cross_system_line_identity_is_incomplete_not_an_arbitrary_pair():
    raw = ns_order()
    del raw["lines"][0]["custcol_fw_solidus_line_id"]
    s = target(raw)
    assert not s.lines_complete and s.lines[0].key == "netsuite_line:63"


@pytest.mark.parametrize(
    "identity, expected", [(11, "line:11"), (True, "netsuite_line:63"), ("  ", "netsuite_line:63")]
)
def test_netsuite_source_line_identity_accepts_integers_without_coercing_boolean(identity, expected):
    raw = ns_order()
    raw["lines"][0]["custcol_fw_solidus_line_id"] = identity
    assert target(raw).lines[0].key == expected


def test_netsuite_zero_tax_requires_explicit_line_evidence():
    raw = ns_order()
    raw["header"].update(taxTotal="0", total="100", isTaxable=False)
    raw["lines"][0]["custcol_fw_vat_amount"] = "0"
    assert target(raw).tax_complete
    del raw["lines"][0]["custcol_fw_vat_amount"]
    assert not target(raw).tax_complete
