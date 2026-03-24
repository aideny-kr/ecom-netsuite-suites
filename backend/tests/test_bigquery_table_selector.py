"""Tests for BigQuery table selector — endpoint, seeder filtering, tool filtering."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.mcp_connector import BigQueryTableSelection

SAMPLE_SCHEMA = {
    "datasets": [
        {
            "dataset_id": "reporting",
            "tables": [
                {"table_id": "orders", "columns": [{"name": "id", "type": "STRING", "description": None}]},
                {"table_id": "customers", "columns": [{"name": "id", "type": "STRING", "description": None}]},
            ],
        },
        {
            "dataset_id": "raw",
            "tables": [
                {"table_id": "events", "columns": [{"name": "id", "type": "STRING", "description": None}]},
            ],
        },
    ],
}


class TestBigQueryTableSelection:
    def test_schema_valid(self):
        req = BigQueryTableSelection(selected_tables={"reporting": ["orders", "customers"]})
        assert "reporting" in req.selected_tables

    def test_schema_empty(self):
        req = BigQueryTableSelection(selected_tables={})
        assert req.selected_tables == {}


class TestSeederFiltering:
    @pytest.mark.asyncio
    async def test_seed_all_when_no_filter(self):
        from app.services.bigquery_schema_seeder import seed_bigquery_schema

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())
        count = await seed_bigquery_schema(mock_db, uuid.uuid4(), SAMPLE_SCHEMA)
        assert count == 3  # All tables

    @pytest.mark.asyncio
    async def test_seed_filtered_by_selected_tables(self):
        from app.services.bigquery_schema_seeder import seed_bigquery_schema

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())
        count = await seed_bigquery_schema(
            mock_db, uuid.uuid4(), SAMPLE_SCHEMA, selected_tables={"reporting": ["orders"]}
        )
        assert count == 1  # Only orders

    @pytest.mark.asyncio
    async def test_seed_empty_filter_seeds_none(self):
        from app.services.bigquery_schema_seeder import seed_bigquery_schema

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock())
        count = await seed_bigquery_schema(mock_db, uuid.uuid4(), SAMPLE_SCHEMA, selected_tables={})
        assert count == 0


class TestUpdateTablesEndpoint:
    @pytest.mark.asyncio
    async def test_update_tables_success(self):
        from app.api.v1.mcp_connectors import update_bigquery_table_selection

        request = BigQueryTableSelection(selected_tables={"reporting": ["orders"]})
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()

        mock_connector = MagicMock()
        mock_connector.provider = "bigquery"
        mock_connector.tenant_id = mock_user.tenant_id
        mock_connector.encrypted_credentials = "encrypted"
        mock_connector.metadata_json = {"project_id": "test", "default_dataset": "reporting"}

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = mock_connector
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch("app.api.v1.mcp_connectors.decrypt_credentials") as mock_dec,
            patch("app.api.v1.mcp_connectors.discover_schema", new_callable=AsyncMock) as mock_disc,
            patch("app.api.v1.mcp_connectors.audit_service") as mock_audit,
            patch("app.api.v1.mcp_connectors.seed_bigquery_schema", new_callable=AsyncMock) as mock_seed,
        ):
            mock_dec.return_value = {"service_account_json": {}, "project_id": "test"}
            mock_disc.return_value = SAMPLE_SCHEMA
            mock_audit.log_event = AsyncMock()
            mock_seed.return_value = 1

            await update_bigquery_table_selection(str(uuid.uuid4()), request, mock_user, mock_db)

        assert mock_connector.metadata_json["selected_tables"] == {"reporting": ["orders"]}
        mock_seed.assert_called_once()


class TestGetSchemaEndpoint:
    @pytest.mark.asyncio
    async def test_get_schema_returns_datasets(self):
        from app.api.v1.mcp_connectors import get_bigquery_schema

        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()

        mock_connector = MagicMock()
        mock_connector.provider = "bigquery"
        mock_connector.tenant_id = mock_user.tenant_id
        mock_connector.encrypted_credentials = "encrypted"
        mock_connector.metadata_json = {"project_id": "test", "selected_tables": {"reporting": ["orders"]}}

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = mock_connector
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch("app.api.v1.mcp_connectors.decrypt_credentials") as mock_dec,
            patch("app.api.v1.mcp_connectors.discover_schema", new_callable=AsyncMock) as mock_disc,
        ):
            mock_dec.return_value = {"service_account_json": {}, "project_id": "test"}
            mock_disc.return_value = SAMPLE_SCHEMA

            result = await get_bigquery_schema(str(uuid.uuid4()), mock_user, mock_db)

        assert "datasets" in result
        # Check selected flag
        for ds in result["datasets"]:
            for tbl in ds["tables"]:
                if ds["dataset_id"] == "reporting" and tbl["table_id"] == "orders":
                    assert tbl["selected"] is True
                else:
                    assert tbl["selected"] is False
