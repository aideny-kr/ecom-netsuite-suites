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
