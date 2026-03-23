"""Tests for BigQuery schema RAG seeder."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest


SAMPLE_SCHEMA = {
    "datasets": [
        {
            "dataset_id": "analytics",
            "tables": [
                {
                    "table_id": "orders",
                    "columns": [
                        {"name": "order_id", "type": "STRING", "description": "Unique order ID"},
                        {"name": "total", "type": "FLOAT64", "description": "Order total"},
                    ],
                },
                {
                    "table_id": "customers",
                    "columns": [
                        {"name": "customer_id", "type": "STRING", "description": "Customer ID"},
                    ],
                },
            ],
        },
        {
            "dataset_id": "raw",
            "tables": [
                {
                    "table_id": "events",
                    "columns": [
                        {"name": "event_id", "type": "STRING", "description": None},
                        {"name": "timestamp", "type": "TIMESTAMP", "description": None},
                        {"name": "payload", "type": "JSON", "description": None},
                    ],
                },
            ],
        },
    ],
}


class TestBigQuerySchemaSeeder:

    @pytest.mark.asyncio
    async def test_seed_creates_chunks(self):
        from app.services.bigquery_schema_seeder import seed_bigquery_schema

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())

        count = await seed_bigquery_schema(
            db=mock_db,
            tenant_id=uuid.uuid4(),
            schema=SAMPLE_SCHEMA,
        )
        # 3 tables total across 2 datasets
        assert count == 3
        assert mock_db.add.call_count == 3

    @pytest.mark.asyncio
    async def test_seed_partition_id(self):
        from app.services.bigquery_schema_seeder import seed_bigquery_schema

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())

        await seed_bigquery_schema(
            db=mock_db,
            tenant_id=uuid.uuid4(),
            schema=SAMPLE_SCHEMA,
        )

        for call in mock_db.add.call_args_list:
            chunk = call[0][0]
            assert chunk.partition_id == "bi/schema-docs"

    @pytest.mark.asyncio
    async def test_seed_chunk_has_table_name(self):
        from app.services.bigquery_schema_seeder import seed_bigquery_schema

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())

        await seed_bigquery_schema(
            db=mock_db,
            tenant_id=uuid.uuid4(),
            schema=SAMPLE_SCHEMA,
        )

        texts = [call[0][0].raw_text for call in mock_db.add.call_args_list]
        assert any("analytics.orders" in t for t in texts)
        assert any("analytics.customers" in t for t in texts)
        assert any("raw.events" in t for t in texts)

    @pytest.mark.asyncio
    async def test_seed_chunk_has_columns(self):
        from app.services.bigquery_schema_seeder import seed_bigquery_schema

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())

        await seed_bigquery_schema(
            db=mock_db,
            tenant_id=uuid.uuid4(),
            schema=SAMPLE_SCHEMA,
        )

        texts = [call[0][0].raw_text for call in mock_db.add.call_args_list]
        orders_text = next(t for t in texts if "analytics.orders" in t)
        assert "order_id" in orders_text
        assert "total" in orders_text

    @pytest.mark.asyncio
    async def test_seed_chunk_has_column_types(self):
        from app.services.bigquery_schema_seeder import seed_bigquery_schema

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())

        await seed_bigquery_schema(
            db=mock_db,
            tenant_id=uuid.uuid4(),
            schema=SAMPLE_SCHEMA,
        )

        texts = [call[0][0].raw_text for call in mock_db.add.call_args_list]
        orders_text = next(t for t in texts if "analytics.orders" in t)
        assert "STRING" in orders_text
        assert "FLOAT64" in orders_text

    @pytest.mark.asyncio
    async def test_seed_source_type(self):
        from app.services.bigquery_schema_seeder import seed_bigquery_schema

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())

        await seed_bigquery_schema(
            db=mock_db,
            tenant_id=uuid.uuid4(),
            schema=SAMPLE_SCHEMA,
        )

        for call in mock_db.add.call_args_list:
            chunk = call[0][0]
            assert chunk.source_type == "bigquery_schema"

    @pytest.mark.asyncio
    async def test_seed_empty_schema(self):
        from app.services.bigquery_schema_seeder import seed_bigquery_schema

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())

        count = await seed_bigquery_schema(
            db=mock_db,
            tenant_id=uuid.uuid4(),
            schema={"datasets": []},
        )
        assert count == 0
        assert mock_db.add.call_count == 0
