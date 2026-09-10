"""Transactions must search actual records and never cross tenant boundaries."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models.canonical import Order, Payout, PayoutLine


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


async def test_order_date_filter_uses_source_date_and_export_has_identical_bounds(client, db, admin_user):
    user, headers = admin_user
    rows = await seed_orders(db, user.tenant_id, ["R100120031", "R100120032", "R100120033"])
    rows[0].source_created_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows[1].source_created_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    rows[2].source_created_at = None
    await db.flush()
    params = {"date_from": "2026-09-01T00:00:00Z", "date_to": "2026-09-02T00:00:00Z"}
    response = await client.get("/api/v1/tables/orders", headers=headers, params=params)
    assert response.status_code == 200 and response.json()["total"] == 1
    assert response.json()["items"][0]["order_number"] == "R100120031"
    export = await client.get("/api/v1/tables/orders/export/csv", headers=headers, params=params)
    assert len(export.text.strip().splitlines()) == 2 and "R100120032" not in export.text


async def test_raw_provider_payload_is_not_returned_in_transaction_rows(client, db, admin_user):
    user, headers = admin_user
    rows = await seed_orders(db, user.tenant_id, ["R100120031"])
    rows[0].raw_data = {"auth": "source-private-token", "customer": {"email": "private@example.com"}}
    await db.flush()
    response = await client.get("/api/v1/tables/orders", headers=headers)
    assert response.status_code == 200
    assert "raw_data" not in response.json()["items"][0]
    assert "private" not in response.text


@pytest.mark.parametrize(
    "params",
    [
        {"date_from": "2026-09-03T00:00:00Z", "date_to": "2026-09-02T00:00:00Z"},
        {"date_from": "2026-09-03T00:00:00"},
    ],
)
async def test_date_filters_require_ordered_timezone_aware_bounds(client, admin_user, params):
    response = await client.get("/api/v1/tables/orders", headers=admin_user[1], params=params)
    assert response.status_code == 422


async def test_payout_drilldown_and_export_filter_the_same_parent_and_tenant(client, db, admin_user, admin_user_b):
    user, headers = admin_user
    payouts = [
        Payout(
            tenant_id=user.tenant_id,
            source="stripe",
            source_id=f"po_{i}",
            dedupe_key=f"po_{i}",
            amount=10,
            fee_amount=0,
            net_amount=10,
            currency="USD",
            status="paid",
        )
        for i in range(2)
    ]
    db.add_all(payouts)
    await db.flush()
    for i, (tenant_id, payout_id) in enumerate(
        [
            (user.tenant_id, payouts[0].id),
            (user.tenant_id, payouts[1].id),
            (admin_user_b[0].tenant_id, payouts[0].id),
        ]
    ):
        db.add(
            PayoutLine(
                tenant_id=tenant_id,
                payout_id=payout_id,
                source="stripe",
                source_id=f"line_{i}",
                dedupe_key=f"line_{i}",
                line_type="charge",
                amount=10,
                fee=0,
                net=10,
                currency="USD",
            )
        )
    await db.flush()
    params = {"payout_id": str(payouts[0].id)}
    response = await client.get("/api/v1/tables/payout_lines", headers=headers, params=params)
    assert response.status_code == 200 and response.json()["total"] == 1
    assert response.json()["items"][0]["source_id"] == "line_0"
    export = await client.get("/api/v1/tables/payout_lines/export/csv", headers=headers, params=params)
    assert len(export.text.strip().splitlines()) == 2
