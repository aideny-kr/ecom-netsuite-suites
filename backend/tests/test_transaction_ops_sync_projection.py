"""Only create preparation explicitly requests customer/shipping business fields."""

import pytest

from app.services.transaction_ops.source_projection import ProjectionError, project_order


def order():
    return {
        "id": 10,
        "number": "R123456789",
        "email": "buyer@example.test",
        "auth_token": "secret",
        "user": {"email": "buyer@example.test", "password": "secret"},
        "bill_address": {
            "firstname": "Example",
            "lastname": "Buyer",
            "address1": "Example Street",
            "city": "Example City",
            "zipcode": "00000",
            "country": {"iso": "NL", "admin_token": "secret"},
            "state": {"abbr": "NH"},
            "token": "secret",
        },
        "ship_address": {"country": {"iso": "NL"}},
        "shipments": [
            {
                "id": 20,
                "cost": "0.00",
                "shipping_rates": [
                    {
                        "id": 5,
                        "selected": True,
                        "cost": "0.00",
                        "shipping_method_id": 7,
                        "shipping_method": {"id": 7, "name": "Standard", "secret": "secret"},
                    }
                ],
            }
        ],
        "line_items": [{"id": 11, "sku": "SKU-1", "variant": {"sku": "SKU-1"}, "quantity": 1, "price": "10.00"}],
        "payments": [{"id": 12, "amount": "10.00", "source": {"card_number": "secret"}}],
    }


def test_routine_projection_keeps_contact_and_shipping_identity_private():
    projected = project_order(order())
    assert "email" not in projected and "bill_address" not in projected
    assert "shipping_rates" not in projected["shipments"][0]


def test_explicit_sync_projection_preserves_required_business_fields_without_credentials():
    projected = project_order(order(), include_sync_data=True)
    assert projected["email"] == "buyer@example.test"
    assert projected["bill_address"]["country"] == {"iso": "NL"}
    assert projected["shipments"][0]["shipping_rates"][0]["shipping_method"] == {"id": "7", "name": "Standard"}
    assert "secret" not in str(projected)
    assert "user" not in projected and "source" not in projected["payments"][0]


@pytest.mark.parametrize("key,value", [("email", {"raw": "bad"}), ("bill_address", "bad")])
def test_malformed_required_sync_fields_fail_without_truncation(key, value):
    with pytest.raises(ProjectionError):
        project_order({**order(), key: value}, include_sync_data=True)


def live_sync_order():
    payload = order()
    payload.update(batch_name="Batch 1", business_entity="Framework Inc", requires_review=False)
    payload["bill_address"] = {
        "name": "Example Buyer",
        "company": "Example Ltd",
        "address1": "Example Street",
        "country_iso": "US",
        "country_id": 1,
        "country": {"id": 1, "iso": "US"},
        "state_name": "California",
        "state_text": "CA",
        "state": {"id": 2, "abbr": "CA"},
        "vat_id": None,
        "reverse_charge_status": "not_applicable",
        "token": "secret",
    }
    payload["line_items"][0].update(
        batch_id=9,
        inventory_units=[{"id": 51, "shipment_id": 20, "variant_id": 8, "state": "on_hand", "serial_number": "secret"}],
    )
    payload["shipments"][0].update(
        stock_location_name="Panurgy",
        selected_shipping_rate={
            "id": 5,
            "name": "Standard",
            "cost": "0",
            "selected": True,
            "shipping_method_id": 7,
            "shipping_method_code": "STD",
            "token": "secret",
        },
        shipping_methods=[{"id": 7, "name": "Standard", "code": "STD", "api_key": "secret"}],
    )
    return payload


def test_live_sync_contract_retains_private_address_and_inventory_routing():
    projected = project_order(live_sync_order(), include_sync_data=True)
    assert projected["bill_address"]["name"] == "Example Buyer"
    assert projected["bill_address"]["company"] == "Example Ltd"
    assert projected["bill_address"]["country_iso"] == "US"
    assert projected["bill_address"]["state_text"] == "CA"
    assert projected["bill_address"]["reverse_charge_status"] == "not_applicable"
    assert projected["batch_name"] == "Batch 1"
    assert projected["line_items"][0]["batch_id"] == "9"
    assert projected["line_items"][0]["inventory_units"] == [
        {"id": "51", "shipment_id": "20", "variant_id": "8", "state": "on_hand"}
    ]
    assert projected["shipments"][0]["stock_location_name"] == "Panurgy"
    assert projected["shipments"][0]["selected_shipping_rate"]["shipping_method_code"] == "STD"
    assert projected["shipments"][0]["shipping_methods"] == [{"id": "7", "name": "Standard", "code": "STD"}]
    assert "secret" not in str(projected)


def test_routine_read_excludes_new_private_sync_fields():
    projected = project_order(live_sync_order())
    assert "batch_name" not in projected
    assert "inventory_units" not in projected["line_items"][0]
    assert "batch_id" not in projected["line_items"][0]
    assert "stock_location_name" not in projected["shipments"][0]
    assert "shipping_methods" not in projected["shipments"][0]
    assert "bill_address" not in projected and "secret" not in str(projected)


@pytest.mark.parametrize("value", ["not-a-list", [{"id": 1}] * 501, [{"id": {"token": "secret"}}]])
def test_private_inventory_evidence_is_bounded_and_validated(value):
    payload = live_sync_order()
    payload["line_items"][0]["inventory_units"] = value
    with pytest.raises(ProjectionError):
        project_order(payload, include_sync_data=True)
