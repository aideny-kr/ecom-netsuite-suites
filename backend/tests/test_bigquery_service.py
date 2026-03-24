"""Tests for BigQuery service — query execution, schema discovery, cost estimation."""

from unittest.mock import MagicMock, patch

import pytest


class TestExecuteQuery:

    @pytest.mark.asyncio
    async def test_returns_columns_and_rows(self):
        from app.services.bigquery_service import execute_query

        mock_row1 = MagicMock()
        mock_row1.values.return_value = ["Alice", 100]
        mock_row2 = MagicMock()
        mock_row2.values.return_value = ["Bob", 200]
        mock_row3 = MagicMock()
        mock_row3.values.return_value = ["Charlie", 300]

        mock_job = MagicMock()
        mock_job.total_bytes_processed = 5000
        mock_job.cache_hit = False

        mock_result = MagicMock()
        field1 = MagicMock()
        field1.name = "name"
        field1.field_type = "STRING"
        field2 = MagicMock()
        field2.name = "amount"
        field2.field_type = "INTEGER"
        mock_result.schema = [field1, field2]
        mock_result.total_rows = 3
        mock_result.__iter__ = lambda self: iter([mock_row1, mock_row2, mock_row3])

        with patch("app.services.bigquery_service._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.query.return_value = mock_job
            mock_job.result.return_value = mock_result
            mock_get_client.return_value = mock_client

            result = await execute_query(
                credentials={"type": "service_account"},
                project_id="test-project",
                query="SELECT name, amount FROM users",
            )

        assert result["columns"] == ["name", "amount"]
        assert result["rows"] == [["Alice", 100], ["Bob", 200], ["Charlie", 300]]
        assert result["row_count"] == 3
        assert result["bytes_processed"] == 5000
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_truncates_at_max_rows(self):
        from app.services.bigquery_service import execute_query

        rows = [MagicMock() for _ in range(1500)]
        for i, r in enumerate(rows):
            r.values.return_value = [i]

        mock_job = MagicMock()
        mock_job.total_bytes_processed = 10000
        mock_job.cache_hit = False

        mock_result = MagicMock()
        mock_result.schema = [MagicMock(name="id", field_type="INTEGER")]
        mock_result.total_rows = 1500
        mock_result.__iter__ = lambda self: iter(rows)

        with patch("app.services.bigquery_service._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.query.return_value = mock_job
            mock_job.result.return_value = mock_result
            mock_get_client.return_value = mock_client

            result = await execute_query(
                credentials={"type": "service_account"},
                project_id="test-project",
                query="SELECT id FROM big_table",
                max_rows=1000,
            )

        assert len(result["rows"]) == 1000
        assert result["truncated"] is True

    @pytest.mark.asyncio
    async def test_rejects_insert(self):
        from app.services.bigquery_service import execute_query
        with pytest.raises(ValueError, match="[Rr]ead.only"):
            await execute_query({"type": "service_account"}, "p", "INSERT INTO t VALUES (1)")

    @pytest.mark.asyncio
    async def test_rejects_update(self):
        from app.services.bigquery_service import execute_query
        with pytest.raises(ValueError, match="[Rr]ead.only"):
            await execute_query({"type": "service_account"}, "p", "UPDATE t SET x=1")

    @pytest.mark.asyncio
    async def test_rejects_delete(self):
        from app.services.bigquery_service import execute_query
        with pytest.raises(ValueError, match="[Rr]ead.only"):
            await execute_query({"type": "service_account"}, "p", "DELETE FROM t WHERE id=1")

    @pytest.mark.asyncio
    async def test_rejects_drop(self):
        from app.services.bigquery_service import execute_query
        with pytest.raises(ValueError, match="[Rr]ead.only"):
            await execute_query({"type": "service_account"}, "p", "DROP TABLE t")

    @pytest.mark.asyncio
    async def test_rejects_create(self):
        from app.services.bigquery_service import execute_query
        with pytest.raises(ValueError, match="[Rr]ead.only"):
            await execute_query({"type": "service_account"}, "p", "CREATE TABLE t (id INT)")

    @pytest.mark.asyncio
    async def test_rejects_merge(self):
        from app.services.bigquery_service import execute_query
        with pytest.raises(ValueError, match="[Rr]ead.only"):
            await execute_query({"type": "service_account"}, "p", "MERGE INTO t USING s ON t.id=s.id")

    @pytest.mark.asyncio
    async def test_rejects_truncate(self):
        from app.services.bigquery_service import execute_query
        with pytest.raises(ValueError, match="[Rr]ead.only"):
            await execute_query({"type": "service_account"}, "p", "TRUNCATE TABLE t")

    @pytest.mark.asyncio
    async def test_case_insensitive_reject(self):
        from app.services.bigquery_service import execute_query
        with pytest.raises(ValueError, match="[Rr]ead.only"):
            await execute_query({"type": "service_account"}, "p", "insert INTO t VALUES (1)")

    @pytest.mark.asyncio
    async def test_allows_select(self):
        from app.services.bigquery_service import execute_query

        mock_job = MagicMock()
        mock_job.total_bytes_processed = 100
        mock_job.cache_hit = False
        mock_result = MagicMock()
        mock_result.schema = [MagicMock(name="x", field_type="INTEGER")]
        mock_result.total_rows = 1
        mock_result.__iter__ = lambda self: iter([MagicMock(values=lambda: [1])])
        mock_job.result.return_value = mock_result

        with patch("app.services.bigquery_service._get_client") as m:
            m.return_value.query.return_value = mock_job
            result = await execute_query({"type": "service_account"}, "p", "SELECT 1")
        assert "columns" in result

    @pytest.mark.asyncio
    async def test_allows_with_cte(self):
        from app.services.bigquery_service import execute_query

        mock_job = MagicMock()
        mock_job.total_bytes_processed = 100
        mock_job.cache_hit = False
        mock_result = MagicMock()
        mock_result.schema = [MagicMock(name="x", field_type="INTEGER")]
        mock_result.total_rows = 1
        mock_result.__iter__ = lambda self: iter([MagicMock(values=lambda: [1])])
        mock_job.result.return_value = mock_result

        with patch("app.services.bigquery_service._get_client") as m:
            m.return_value.query.return_value = mock_job
            result = await execute_query({"type": "service_account"}, "p", "WITH cte AS (SELECT 1 AS x) SELECT * FROM cte")
        assert "columns" in result

    @pytest.mark.asyncio
    async def test_sets_max_bytes_billed(self):
        from app.services.bigquery_service import execute_query

        mock_job = MagicMock()
        mock_job.total_bytes_processed = 100
        mock_job.cache_hit = False
        mock_result = MagicMock()
        mock_result.schema = [MagicMock(name="x", field_type="INTEGER")]
        mock_result.total_rows = 0
        mock_result.__iter__ = lambda self: iter([])
        mock_job.result.return_value = mock_result

        with patch("app.services.bigquery_service._get_client") as m:
            m.return_value.query.return_value = mock_job
            await execute_query({"type": "service_account"}, "p", "SELECT 1", max_bytes_billed=1_000_000_000)
            call_args = m.return_value.query.call_args
            job_config = call_args.kwargs.get("job_config") or call_args[1].get("job_config")
            assert job_config.maximum_bytes_billed == 1_000_000_000

    @pytest.mark.asyncio
    async def test_reports_cache_hit(self):
        from app.services.bigquery_service import execute_query

        mock_job = MagicMock()
        mock_job.total_bytes_processed = 0
        mock_job.cache_hit = True
        mock_result = MagicMock()
        mock_result.schema = [MagicMock(name="x", field_type="INTEGER")]
        mock_result.total_rows = 1
        mock_result.__iter__ = lambda self: iter([MagicMock(values=lambda: [1])])
        mock_job.result.return_value = mock_result

        with patch("app.services.bigquery_service._get_client") as m:
            m.return_value.query.return_value = mock_job
            result = await execute_query({"type": "service_account"}, "p", "SELECT 1")
        assert result["cache_hit"] is True


class TestDiscoverSchema:

    @pytest.mark.asyncio
    async def test_returns_datasets(self):
        from app.services.bigquery_service import discover_schema

        mock_ds1 = MagicMock()
        mock_ds1.dataset_id = "analytics"
        mock_ds2 = MagicMock()
        mock_ds2.dataset_id = "raw"

        mock_table = MagicMock()
        mock_table.table_id = "orders"

        with patch("app.services.bigquery_service._get_client") as m:
            m.return_value.list_datasets.return_value = [mock_ds1, mock_ds2]
            m.return_value.list_tables.return_value = [mock_table]
            result = await discover_schema({"type": "service_account"}, "p")

        assert len(result["datasets"]) == 2

    @pytest.mark.asyncio
    async def test_single_dataset_with_columns(self):
        from app.services.bigquery_service import discover_schema

        mock_table = MagicMock()
        mock_table.table_id = "orders"

        mock_schema_field = MagicMock()
        mock_schema_field.name = "order_id"
        mock_schema_field.field_type = "INTEGER"
        mock_schema_field.description = "Primary key"

        mock_full_table = MagicMock()
        mock_full_table.schema = [mock_schema_field]

        with patch("app.services.bigquery_service._get_client") as m:
            m.return_value.list_tables.return_value = [mock_table]
            m.return_value.get_table.return_value = mock_full_table
            result = await discover_schema({"type": "service_account"}, "p", dataset="analytics")

        assert len(result["datasets"]) == 1
        tables = result["datasets"][0]["tables"]
        assert len(tables) >= 1
        assert tables[0]["columns"][0]["name"] == "order_id"
        assert tables[0]["columns"][0]["type"] == "INTEGER"


class TestValidateConnection:

    @pytest.mark.asyncio
    async def test_success(self):
        from app.services.bigquery_service import validate_connection

        mock_job = MagicMock()
        mock_job.result.return_value = MagicMock()

        with patch("app.services.bigquery_service._get_client") as m:
            m.return_value.query.return_value = mock_job
            result = await validate_connection({"type": "service_account"}, "p")

        assert result["valid"] is True
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_failure(self):
        from app.services.bigquery_service import validate_connection

        with patch("app.services.bigquery_service._get_client") as m:
            m.return_value.query.side_effect = Exception("Permission denied")
            result = await validate_connection({"type": "service_account"}, "p")

        assert result["valid"] is False
        assert "Permission denied" in result["error"]


class TestEstimateQueryCost:

    @pytest.mark.asyncio
    async def test_estimate_returns_bytes_and_cost(self):
        from app.services.bigquery_service import estimate_query_cost

        mock_job = MagicMock()
        mock_job.total_bytes_processed = 500_000_000  # 500 MB

        with patch("app.services.bigquery_service._get_client") as m:
            m.return_value.query.return_value = mock_job
            result = await estimate_query_cost({"type": "service_account"}, "p", "SELECT * FROM big_table")

        assert result["estimated_bytes"] == 500_000_000
        assert abs(result["estimated_cost_usd"] - 0.0025) < 0.001  # ~$5/TB

    @pytest.mark.asyncio
    async def test_1tb_costs_5_dollars(self):
        from app.services.bigquery_service import estimate_query_cost

        mock_job = MagicMock()
        mock_job.total_bytes_processed = 1_000_000_000_000  # 1 TB

        with patch("app.services.bigquery_service._get_client") as m:
            m.return_value.query.return_value = mock_job
            result = await estimate_query_cost({"type": "service_account"}, "p", "SELECT * FROM huge_table")

        assert result["estimated_cost_usd"] == 5.0


class TestServiceAccountCredentials:

    @pytest.mark.asyncio
    async def test_uses_service_account_info(self):
        from app.services.bigquery_service import _get_client

        sa_json = {"type": "service_account", "project_id": "test"}
        with patch("app.services.bigquery_service.service_account.Credentials.from_service_account_info") as mock_creds:
            mock_creds.return_value = MagicMock()
            with patch("app.services.bigquery_service.bigquery.Client"):
                _get_client(sa_json, "test")
                mock_creds.assert_called_once()
