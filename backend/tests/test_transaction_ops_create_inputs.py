"""Missing-order requests retain exact source units, routing and private proof."""

from copy import deepcopy
from datetime import timedelta

import pytest

from app.services.transaction_ops import netsuite_create as mod
from app.services.transaction_ops.normalization import TransactionMapping
from tests.test_transaction_ops_planner import planning_case


def create_case():
    case = planning_case(inventory=True, assessment=True)
    case.config.mapping_json["netsuite_create"] = {
        "schema_version": 1,
        "external_id_prefix": "",
        "tax_mode": "legacy_tax_codes",
        "transaction_timezone": "America/Los_Angeles",
        "inventory_mode": "line_location",
        "sku_rules": {"FRAME-1": {"netsuite_sku": "NATIVE-1", "quantity_multiplier": 2}},
        "stock_location_ids": {"Warehouse A": "4"},
        "inventory_subsidiary_ids": {"Warehouse A": "3"},
        "shipping_method_ids": {"8": "7"},
    }
    o = case.source["orders"][0]
    o.update(
        completed_at=o["updated_at"],
        requires_review=False,
        credit_sale=False,
        customer_type="consumer",
        order_type="marketplace",
        line_item_type="laptop_config",
        payment_state="paid",
        shipment_state="ready",
        payment_total="120",
        order_total_after_store_credit="120",
        total_applicable_store_credit="0",
        deposit_amount="0",
        email="buyer@example.invalid",
        payments=[{"id": "80", "state": "completed", "amount": "120", "source_type": "StripeGateway::PaymentSource"}],
    )
    address = {
        "name": "Example Buyer",
        "company": "",
        "address1": "1 Example Street",
        "address2": "",
        "city": "Example City",
        "zipcode": "90210",
        "phone": "",
        "country_iso": "US",
        "country": {"iso": "US"},
        "state": {"abbr": "CA"},
        "state_name": "California",
    }
    o["bill_address"], o["ship_address"] = deepcopy(address), deepcopy(address)
    o["shipments"] = [
        {
            "id": "50",
            "order_id": o["id"],
            "state": "ready",
            "cost": "0",
            "adjustments": [],
            "stock_location_name": "Warehouse A",
            "selected_shipping_rate": {"selected": True, "shipping_method_id": "8", "shipping_method_code": ""},
        }
    ]
    o["line_items"][0].update(parent_id=None, inventory_units=[{"id": "501", "shipment_id": "50", "state": "on_hand"}])
    return case


def prepare(case):
    return mod.prepare_create_input(
        case.source,
        TransactionMapping.model_validate(case.config.mapping_json),
        account_id=case.config.netsuite_account_id,
        subsidiary_id="3",
        now=case.now,
    )


def test_create_input_preserves_money_with_explicit_native_quantity_conversion():
    case = create_case()
    request = prepare(case)
    payload = request.payload_json
    assert payload["account_id"] == "6738075-sb1"
    assert payload["order_reference"] == payload["external_id"] == "R123456789"
    assert payload["currency"] == {"symbol": "EUR", "precision": 2}
    assert payload["order_status"] == "A"
    assert payload["lines"][0] == {
        "source_line_id": "11",
        "source_parent_id": None,
        "source_sku": "FRAME-1",
        "netsuite_sku": "NATIVE-1",
        "source_quantity": "1",
        "quantity_multiplier": 2,
        "quantity": "2",
        "rate": "50",
        "amount": "100",
        "tax_amount": "20",
        "tax_code_id": "610",
        "inventory_unit_ids": ["501"],
        "location_id": "4",
        "inventory_subsidiary_id": "3",
    }
    assert payload["shipping_method_id"] == "7"
    assert payload["expected_totals"] == {
        "subtotal": "100",
        "taxtotal": "20",
        "total": "120",
        "shippingcost": "0",
        "handlingcost": "0",
        "discounttotal": "0",
    }
    assert len(request.private_fingerprint) == len(request.source_fingerprint) == 64
    payload["billing_address"]["addr1"] = "mutated"
    assert request.payload_json["billing_address"]["addr1"] == "1 Example Street"


def test_private_address_or_routing_change_invalidates_input_fingerprint():
    case = create_case()
    before = prepare(case)
    case.source["orders"][0]["ship_address"]["address1"] = "2 Example Street"
    after = prepare(case)
    assert before.source_fingerprint == after.source_fingerprint
    assert before.private_fingerprint != after.private_fingerprint
    assert before.payload_json != after.payload_json


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.config.mapping_json["netsuite_create"].update(external_id_prefix="other-"),
        lambda c: c.config.mapping_json["netsuite_create"].pop("transaction_timezone"),
        lambda c: c.config.mapping_json["netsuite_create"].update(transaction_timezone="No/Such_Zone"),
        lambda c: c.config.mapping_json["netsuite_create"]["sku_rules"].clear(),
        lambda c: c.config.mapping_json["netsuite_create"]["stock_location_ids"].clear(),
        lambda c: c.config.mapping_json["netsuite_create"]["shipping_method_ids"].clear(),
        lambda c: c.config.mapping_json.update(line_identity_mode="source_line_id"),
        lambda c: c.config.mapping_json["netsuite_legacy_tax"].update(account_id="9999999"),
        lambda c: c.source["orders"][0].update(requires_review=True),
        lambda c: c.source["orders"][0].update(requires_review="false"),
        lambda c: c.source["orders"][0].update(credit_sale=True),
        lambda c: c.source["orders"][0].update(customer_type="business"),
        lambda c: c.source["orders"][0].update(payment_total="119"),
        lambda c: c.source["orders"][0].update(payment_state="balance_due"),
        lambda c: c.source["orders"][0].update(deposit_amount="1"),
        lambda c: c.source["orders"][0].update(total_applicable_store_credit="1"),
        lambda c: c.source["orders"][0].update(order_total_after_store_credit="119"),
        lambda c: c.source["orders"][0].update(shipment_state="shipped"),
        lambda c: c.source["orders"][0]["payments"][0].update(state="failed"),
        lambda c: c.source["orders"][0]["payments"][0].update(amount="119"),
        lambda c: c.source["orders"][0].pop("email"),
        lambda c: c.source["orders"][0]["ship_address"].pop("country"),
        lambda c: c.source["orders"][0]["ship_address"]["country"].update(iso="NL"),
        lambda c: c.source["orders"][0]["line_items"][0]["inventory_units"][0].update(shipment_id="51"),
        lambda c: c.source["orders"][0]["line_items"][0].update(parent_id="99"),
        lambda c: c.source["orders"][0]["line_items"][0].update(parent_id="11"),
        lambda c: c.source["orders"][0]["shipments"][0]["selected_shipping_rate"].update(selected=False),
        lambda c: c.source["orders"][0]["shipments"][0].update(state="shipped"),
        lambda c: c.source["orders"][0].update(completed_at="2026-09-04T01:00:00"),
        lambda c: c.source.update(read_at=(c.now - timedelta(minutes=16)).isoformat()),
    ],
)
def test_unsupported_missing_ambiguous_or_stale_create_input_never_becomes_an_intent(change):
    case = create_case()
    change(case)
    with pytest.raises((mod.CreateInputError, ValueError)):
        prepare(case)


def test_nonterminating_native_unit_rate_is_not_rounded():
    case = create_case()
    case.config.mapping_json["netsuite_create"]["sku_rules"]["FRAME-1"]["quantity_multiplier"] = 3
    with pytest.raises(mod.CreateInputError, match="unsupported_exact_rate"):
        prepare(case)


def test_transaction_date_uses_configured_business_timezone():
    case = create_case()
    case.source["orders"][0]["completed_at"] = "2026-09-04T01:00:00Z"
    assert prepare(case).payload_json["transaction_date"] == "2026-09-03"


@pytest.mark.parametrize("fields", [{"currency": "USD"}, {"exchange_rate": "1.2"}])
def test_explicit_cross_currency_payment_evidence_blocks_create(fields):
    case = create_case()
    case.source["orders"][0]["payments"][0].update(fields)
    with pytest.raises(mod.CreateInputError, match="create_payment_unproven"):
        prepare(case)


@pytest.mark.parametrize("currency,precision", [("JPY", 0), ("EUR", 2), ("KWD", 3)])
def test_create_currency_and_precision_are_kept_explicit(currency, precision):
    case = create_case()
    case.source["orders"][0]["currency"] = currency
    case.config.mapping_json["currency_minor_units"] = {currency: precision}
    assert prepare(case).payload_json["currency"] == {"symbol": currency, "precision": precision}


def test_three_decimal_currency_does_not_round_native_line_money():
    case = create_case()
    case.config.mapping_json["currency_minor_units"] = {"KWD": 3}
    order = case.source["orders"][0]
    order.update(
        currency="KWD",
        item_total="1.125",
        total="1.350",
        tax_total="0.225",
        additional_tax_total="0.225",
        adjustment_total="0.225",
        payment_total="1.350",
        order_total_after_store_credit="1.350",
    )
    order["line_items"][0].update(price="1.125", total="1.350")
    order["line_items"][0]["adjustments"][0]["amount"] = "0.225"
    order["payments"][0]["amount"] = "1.350"
    line = prepare(case).payload_json["lines"][0]
    assert (line["amount"], line["tax_amount"], line["rate"]) == ("1.125", "0.225", "0.5625")


def test_inventory_owner_can_differ_only_in_explicit_cross_subsidiary_mode():
    case = create_case()
    create = case.config.mapping_json["netsuite_create"]
    create.update(inventory_mode="cross_subsidiary", inventory_subsidiary_ids={"Warehouse A": "1"})
    request = prepare(case).payload_json
    assert request["inventory_mode"] == "cross_subsidiary"
    assert request["subsidiary_id"] == "3"
    assert request["lines"][0]["inventory_subsidiary_id"] == "1"
    create["inventory_mode"] = "line_location"
    with pytest.raises(mod.CreateInputError, match="create_inventory_scope_unproven"):
        prepare(case)


def test_inventory_owner_is_required_even_when_location_id_is_known():
    case = create_case()
    case.config.mapping_json["netsuite_create"].update(inventory_mode="cross_subsidiary", inventory_subsidiary_ids={})
    with pytest.raises(mod.CreateInputError, match="create_identity_unproven"):
        prepare(case)


def test_nested_sync_shipment_without_redundant_order_id_retains_inventory_ownership():
    case = create_case()
    del case.source["orders"][0]["shipments"][0]["order_id"]
    assert prepare(case).payload_json["lines"][0]["inventory_unit_ids"] == ["501"]


@pytest.mark.parametrize("order_id", ["999", "R987654321", "R123456789-SPLIT", None])
def test_explicit_conflicting_shipment_order_identity_is_not_ignored(order_id):
    case = create_case()
    case.source["orders"][0]["shipments"][0]["order_id"] = order_id
    with pytest.raises(mod.CreateInputError):
        prepare(case)


def test_sync_shipment_owner_can_be_the_exact_full_order_reference():
    case = create_case()
    case.source["orders"][0]["shipments"][0]["order_id"] = "R123456789"
    assert prepare(case).payload_json["order_reference"] == "R123456789"
