"""Fixed read capabilities: tenant binding, exact amounts, bounded keyset pages."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.transaction_ops import metabase_reader as reader

NOW = datetime(2026, 9, 8, 6, tzinfo=timezone.utc)
BINDING = {
    "connector_id": str(uuid4()),
    "timestamp_storage": "utc_naive",
    "server_url": "https://analytics.example/api/metabase-mcp",
    "database_id": 2,
    "database_name": "Solidus (Reporting Copy)",
    "schema_name": "public",
    "orders_table_id": 6,
    "refunds_table_id": 86,
    "payments_table_id": 134,
}


def result(fields, rows, table="orders", **overrides):
    return {
        "status": "completed",
        "database_id": 2,
        "cached": False,
        "started_at": NOW.isoformat(),
        "row_count": len(rows),
        "continuation_token": None,
        "data": {"cols": [{"name": field, "table_id": BINDING[table + "_table_id"]} for field in fields], "rows": rows},
        **overrides,
    }


def order_row(id=1, **changes):
    return dict(
        id=id,
        number=f"R{id:09d}",
        total=Decimal("123.120001"),
        additional_tax_total=Decimal("1.120001"),
        included_tax_total=Decimal("0"),
        currency="USD",
        business_entity="Framework Inc",
        business_entity_slug="framework_inc",
        completed_at="2026-09-07T00:00:00Z",
        updated_at="2026-09-07T12:00:00Z",
        **changes,
    )


@pytest.fixture
def transport(monkeypatch):
    loader = AsyncMock(return_value=object())
    query = AsyncMock()
    monkeypatch.setattr(reader, "_connector", loader)
    monkeypatch.setattr(reader, "call_external_mcp_tool", query)
    return loader, query


@pytest.mark.asyncio
async def test_fixed_order_query_exact_values_and_no_secret_or_arbitrary_sql(transport):
    row = order_row()
    transport[1].return_value = result(reader.ORDER_FIELDS, [[row[f] for f in reader.ORDER_FIELDS]])
    sample = await reader.read_order(AsyncMock(), uuid4(), BINDING, row["number"], now=NOW)
    assert sample["orders"][0]["total"] == "123.120001"
    assert sample["page_complete"] is True
    call = transport[1].call_args
    assert call.args[1] == "query" and call.kwargs["parse_decimal"] is True
    stage = call.args[2]["query"]["stages"][0]
    assert stage["source-table"] == ["Solidus (Reporting Copy)", "public", "spree_orders"]
    assert stage["limit"] == 2
    assert "native" not in str(stage) and "email" not in str(stage)
    assert sample["replica_freshness"] == "unverified"  # query time isn't replication lag


@pytest.mark.asyncio
async def test_keyset_page_uses_sentinel_not_metabase_2000_row_exhaustion(transport):
    rows = [order_row(id=i) for i in range(1, 22)]
    transport[1].return_value = result(reader.ORDER_FIELDS, [[r[f] for f in reader.ORDER_FIELDS] for r in rows])
    page = await reader.read_order_page(AsyncMock(), uuid4(), BINDING, NOW.replace(day=7, hour=0), NOW, now=NOW)
    assert len(page["orders"]) == 20 and page["next_after_id"] == 20
    assert page["scan_complete"] is False
    stage = transport[1].call_args.args[2]["query"]["stages"][0]
    assert stage["limit"] == 21 and stage["order-by"][0][0] == "asc"
    assert "updated_at" in str(stage["filters"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "failed"},
        {"database_id": 100},
        {"cached": True},
        {"row_count": 2},
        {"continuation_token": "budget-is-not-completeness"},
        {"started_at": "2026-09-01T00:00:00Z"},
    ],
)
async def test_wrong_database_stale_cached_partial_or_failed_result_stays_incomplete(transport, overrides):
    row = order_row()
    transport[1].return_value = result(reader.ORDER_FIELDS, [[row[f] for f in reader.ORDER_FIELDS]], **overrides)
    with pytest.raises(reader.ReplicaReadError):
        await reader.read_order(AsyncMock(), uuid4(), BINDING, row["number"], now=NOW)


@pytest.mark.asyncio
async def test_refunds_keep_individual_identity_payment_link_and_completion_state(transport):
    rows = [
        [7, 8, Decimal("100.01"), "ch_refund_7", "2026-09-07T00:00:00Z", "2026-09-07T01:00:00Z", None, 1],
        [9, 8, Decimal("2.00"), None, "2026-09-07T00:00:00Z", "2026-09-07T01:00:00Z", None, 0],
    ]
    transport[1].return_value = result(reader.REFUND_FIELDS, rows, "refunds")
    data = await reader.read_payment_refunds(AsyncMock(), uuid4(), BINDING, [8], now=NOW)
    assert data["refunds"][0]["amount"] == "100.01"
    assert data["refunds"][0]["id"] == 7 and data["refunds"][0]["payment_id"] == 8
    assert data["refunds"][0]["completed"] is True and data["refunds"][1]["completed"] is False


@pytest.mark.asyncio
async def test_no_float_rounding_or_duplicate_rows(transport):
    row = order_row()
    row["total"] = 123.120001
    transport[1].return_value = result(reader.ORDER_FIELDS, [[row[f] for f in reader.ORDER_FIELDS]])
    with pytest.raises(reader.ReplicaReadError):
        await reader.read_order(AsyncMock(), uuid4(), BINDING, row["number"], now=NOW)
    row = order_row()
    transport[1].return_value = result(reader.ORDER_FIELDS, [[row[f] for f in reader.ORDER_FIELDS]] * 2)
    with pytest.raises(reader.ReplicaReadError):
        await reader.read_order(AsyncMock(), uuid4(), BINDING, row["number"], now=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["R123' OR 1=1", "", "https://evil.example", "R000000001;DELETE"])
async def test_invalid_reference_fails_before_external_access(transport, reference):
    with pytest.raises(reader.ReplicaReadError):
        await reader.read_order(AsyncMock(), uuid4(), BINDING, reference, now=NOW)
    transport[0].assert_not_awaited()
    transport[1].assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_utc_naive_contract_ignores_metabase_display_offset(transport):
    row = order_row()
    row["updated_at"] = "2026-09-07T18:02:41.347883-07:00"
    transport[1].return_value = result(reader.ORDER_FIELDS, [[row[f] for f in reader.ORDER_FIELDS]])
    sample = await reader.read_order(AsyncMock(), uuid4(), BINDING, row["number"], now=NOW)
    assert sample["orders"][0]["updated_at"] == "2026-09-07T18:02:41.347883+00:00"


@pytest.mark.asyncio
async def test_binding_requires_explicit_verified_timestamp_storage(transport):
    binding = {**BINDING}
    binding.pop("timestamp_storage", None)
    with pytest.raises(reader.ReplicaReadError, match="binding"):
        await reader.read_order(AsyncMock(), uuid4(), binding, "R000000001", now=NOW)
    transport[1].assert_not_awaited()


@pytest.mark.asyncio
async def test_same_tenant_connector_is_required_before_query(db, admin_user, admin_user_b, monkeypatch):
    from app.models.mcp_connector import McpConnector

    user, _ = admin_user
    c = McpConnector(
        tenant_id=user.tenant_id,
        provider="custom",
        label="Replica",
        server_url="https://analytics.example/api/metabase-mcp",
        auth_type="oauth2",
        status="active",
        is_enabled=True,
        encrypted_credentials="opaque",
    )
    db.add(c)
    await db.flush()
    binding = reader.ReplicaBinding.model_validate({**BINDING, "connector_id": str(c.id)})
    query = AsyncMock()
    monkeypatch.setattr(reader, "call_external_mcp_tool", query)
    assert (await reader._connector(db, user.tenant_id, binding)).id == c.id
    with pytest.raises(reader.ReplicaReadError, match="unavailable"):
        await reader.read_order(db, admin_user_b[0].tenant_id, binding, "R000000001", now=NOW)
    c.is_enabled = False
    await db.flush()
    with pytest.raises(reader.ReplicaReadError, match="unavailable"):
        await reader.read_order(db, user.tenant_id, binding, "R000000001", now=NOW)
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_payments_link_to_exact_order_without_reading_payment_details(transport):
    transport[1].return_value = result(
        reader.PAYMENT_FIELDS, [[12, 9, "completed", "2026-09-07T00:00:00Z", "2026-09-07T00:00:00Z"]], "payments"
    )
    data = await reader.read_order_payments(AsyncMock(), uuid4(), BINDING, 9, now=NOW)
    assert data["payments"][0]["order_id"] == 9
    assert data["payments"][0]["id"] == 12
    assert data["scan_complete"] is True
    assert "source_id" not in str(transport[1].call_args.args[2])


@pytest.mark.asyncio
async def test_refund_change_scan_includes_old_order_activity_and_sentinel(transport):
    rows = [
        [n, 8, Decimal("1"), "refund-" + str(n), "2025-01-01T00:00:00Z", "2026-09-07T05:00:00Z", None, 1]
        for n in range(1, 102)
    ]
    transport[1].return_value = result(reader.REFUND_FIELDS, rows, "refunds")
    data = await reader.read_refund_page(AsyncMock(), uuid4(), BINDING, NOW.replace(day=7, hour=0), NOW, now=NOW)
    assert len(data["refunds"]) == 100 and data["next_after_id"] == 100
    assert data["scan_complete"] is False
    filters = transport[1].call_args.args[2]["query"]["stages"][0]["filters"]
    assert "updated_at" in str(filters) and "completed_at" not in str(filters)


@pytest.mark.asyncio
async def test_refund_activity_resolves_only_owned_payment_and_order_lineage(transport):
    refund = [7, 8, Decimal("100.01"), "rf7", "2026-01-01T00:00:00Z", "2026-09-07T01:00:00Z", None, 1]
    payment = [8, 9, "completed", "2026-01-01T00:00:00Z", "2026-09-07T01:00:00Z"]
    order = order_row(id=9)
    transport[1].side_effect = [
        result(reader.REFUND_FIELDS, [refund], "refunds"),
        result(reader.PAYMENT_FIELDS, [payment], "payments"),
        result(reader.ORDER_FIELDS, [[order[f] for f in reader.ORDER_FIELDS]]),
    ]
    page = await reader.read_changed_refund_orders(
        AsyncMock(), uuid4(), BINDING, NOW.replace(day=7, hour=0), NOW, now=NOW
    )
    assert page["orders"][0]["number"] == "R000000009"
    assert page["refunds"][0]["order_reference"] == "R000000009"
    assert page["next_after_id"] is None
    assert transport[1].await_count == 3


@pytest.mark.asyncio
async def test_missing_refund_parent_never_advances_a_complete_window(transport):
    refund = [7, 8, Decimal("100.01"), "rf7", "2026-01-01T00:00:00Z", "2026-09-07T01:00:00Z", None, 1]
    transport[1].side_effect = [
        result(reader.REFUND_FIELDS, [refund], "refunds"),
        result(reader.PAYMENT_FIELDS, [], "payments"),
    ]
    with pytest.raises(reader.ReplicaReadError, match="lineage"):
        await reader.read_changed_refund_orders(AsyncMock(), uuid4(), BINDING, NOW.replace(day=7, hour=0), NOW, now=NOW)


@pytest.mark.asyncio
async def test_binding_pins_endpoint_even_if_saved_connector_is_repointed(db, admin_user):
    from app.models.mcp_connector import McpConnector

    actor = admin_user[0]
    connector = McpConnector(
        tenant_id=actor.tenant_id,
        provider="custom",
        label="Replica",
        server_url="https://replacement.example/api/metabase-mcp",
        auth_type="oauth2",
        status="active",
        is_enabled=True,
        encrypted_credentials="opaque",
    )
    db.add(connector)
    await db.flush()
    binding = reader.ReplicaBinding.model_validate({**BINDING, "connector_id": str(connector.id)})
    with pytest.raises(reader.ReplicaReadError, match="unavailable"):
        await reader._connector(db, actor.tenant_id, binding)
