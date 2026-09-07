"""Transactions must search actual records and never cross tenant boundaries."""

from decimal import Decimal

import pytest

from app.models.canonical import Order


async def seed_orders(db, tenant_id, references):
    rows = [
        Order(
            tenant_id=tenant_id,
            dedupe_key=f"solidus:{reference}",
            source="solidus",
            source_id=reference,
            order_number=reference,
            currency="USD",
            total_amount=Decimal("1724.00"),
            subtotal=Decimal("1600.00"),
            tax_amount=Decimal("124.00"),
            discount_amount=Decimal("0.00"),
            status="complete",
        )
        for reference in references
    ]
    db.add_all(rows)
    await db.flush()
    return rows


async def test_order_search_filters_rows_and_total(client, db, admin_user):
    user, headers = admin_user
    await seed_orders(db, user.tenant_id, ["R100120031", "R100120032"])
    response = await client.get("/api/v1/tables/orders", headers=headers, params={"search": "120031"})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [row["order_number"] for row in response.json()["items"]] == ["R100120031"]
    assert response.json()["items"][0]["total_amount"] == "1724.000000"


@pytest.mark.parametrize("search", ["%", "_", "no-such-order"])
async def test_search_is_literal_and_does_not_match_every_row(client, db, admin_user, search):
    user, headers = admin_user
    await seed_orders(db, user.tenant_id, ["R100120031", "R100120032"])
    response = await client.get("/api/v1/tables/orders", headers=headers, params={"search": search})
    assert response.status_code == 200
    assert response.json()["total"] == 0 and response.json()["items"] == []


async def test_order_search_is_explicitly_tenant_scoped(client, db, admin_user, admin_user_b):
    user, headers = admin_user
    await seed_orders(db, user.tenant_id, ["R100120031"])
    await seed_orders(db, admin_user_b[0].tenant_id, ["R100120031", "R100120032"])
    response = await client.get("/api/v1/tables/orders", headers=headers, params={"search": "R10012003"})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert all(row["tenant_id"] == str(user.tenant_id) for row in response.json()["items"])


@pytest.mark.parametrize("column", ["__table__", "metadata", "unknown_column", "raw_data"])
async def test_sort_fields_are_validated_columns(client, admin_user, column):
    response = await client.get("/api/v1/tables/orders", headers=admin_user[1], params={"sort_by": column})
    assert response.status_code == 422


async def test_filtered_export_matches_the_table(client, db, admin_user, admin_user_b):
    user, headers = admin_user
    await seed_orders(db, user.tenant_id, ["R100120031", "R100120032"])
    await seed_orders(db, admin_user_b[0].tenant_id, ["R100120031"])
    response = await client.get("/api/v1/tables/orders/export/csv", headers=headers, params={"search": "120031"})
    assert response.status_code == 200
    assert "R100120032" not in response.text
    assert len(response.text.strip().splitlines()) == 2


async def test_export_refuses_truncation_and_allows_a_narrower_filter(client, db, admin_user, monkeypatch):
    monkeypatch.setattr("app.services.table_service.MAX_EXPORT_ROWS", 2, raising=False)
    user, headers = admin_user
    await seed_orders(db, user.tenant_id, ["R100120031", "R100120032", "R200120033"])
    response = await client.get("/api/v1/tables/orders/export/csv", headers=headers)
    assert response.status_code == 422
    assert "narrow" in response.json()["detail"].lower()
    filtered = await client.get("/api/v1/tables/orders/export/csv", headers=headers, params={"search": "R100"})
    assert filtered.status_code == 200
    assert len(filtered.text.strip().splitlines()) == 3
