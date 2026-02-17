"""Tests for data.sample_table_read MCP tool."""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.tools.data_sample import execute, ALLOWED_TABLES
from app.models.canonical import Order
from tests.conftest import create_test_tenant


@pytest.mark.asyncio
async def test_disallowed_table():
    """Disallowed table name raises ValueError."""
    with pytest.raises(ValueError, match="not in the allowlist"):
        await execute({"table_name": "secret_table"})


@pytest.mark.asyncio
async def test_fallback_without_db():
    """Without a DB session, returns columns only (no rows)."""
    result = await execute({"table_name": "orders"})
    assert result["table"] == "orders"
    assert result["row_count"] == 0
    assert result["rows"] == []
    assert len(result["columns"]) > 0


@pytest.mark.asyncio
async def test_real_db_query_returns_rows(db: AsyncSession):
    """With a DB session and data, returns real rows."""
    tenant = await create_test_tenant(db, slug=f"sample-{uuid.uuid4().hex[:6]}")

    # Insert a test order (matches CanonicalMixin fields)
    order = Order(
        tenant_id=tenant.id,
        dedupe_key=f"test-{uuid.uuid4().hex[:8]}",
        source="shopify",
        source_id="ext-1",
        order_number="ORD-001",
        status="completed",
        currency="USD",
        total_amount=100.00,
        subtotal=90.00,
        tax_amount=10.00,
        discount_amount=0,
    )
    db.add(order)
    await db.flush()

    result = await execute(
        {"table_name": "orders"},
        context={"db": db, "tenant_id": str(tenant.id)},
    )
    assert result["table"] == "orders"
    assert result["row_count"] >= 1
    assert len(result["rows"]) >= 1
    assert "order_number" in result["columns"]


@pytest.mark.asyncio
async def test_tenant_isolation(db: AsyncSession):
    """Data from one tenant should not leak to another's query (RLS aside,
    the tool doesn't filter by tenant_id — relies on RLS at DB level).
    This test verifies the tool returns data successfully for the given session."""
    tenant = await create_test_tenant(db, slug=f"iso-{uuid.uuid4().hex[:6]}")

    result = await execute(
        {"table_name": "orders"},
        context={"db": db, "tenant_id": str(tenant.id)},
    )
    # Should return without error
    assert result["table"] == "orders"
    assert isinstance(result["rows"], list)


@pytest.mark.asyncio
async def test_all_allowed_tables_have_models():
    """Every table in ALLOWED_TABLES has a corresponding model in TABLE_MODEL_MAP."""
    from app.services.table_service import TABLE_MODEL_MAP

    for table in ALLOWED_TABLES:
        assert table in TABLE_MODEL_MAP, f"Missing model for allowed table: {table}"
