"""Tests for BigQuery tool executors — mock service, verify tool behavior."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_context(tenant_id=None):
    return {
        "tenant_id": str(tenant_id or uuid.uuid4()),
        "db": AsyncMock(),
        "actor_id": str(uuid.uuid4()),
        "correlation_id": "test",
    }


def _mock_connector():
    c = MagicMock()
    c.encrypted_credentials = "encrypted_data"
    c.metadata_json = {"project_id": "test-project", "default_dataset": "analytics"}
    return c


class TestBigquerySqlTool:

    @pytest.mark.asyncio
    async def test_execute_returns_result(self):
        from app.mcp.tools.bigquery_tools import bigquery_sql_execute

        ctx = _make_context()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = _mock_connector()
        ctx["db"].execute = AsyncMock(return_value=mock_result)

        with patch("app.mcp.tools.bigquery_tools.decrypt_credentials") as mock_decrypt, \
             patch("app.mcp.tools.bigquery_tools.execute_query", new_callable=AsyncMock) as mock_exec:
            mock_decrypt.return_value = {"service_account_json": {}, "project_id": "test"}
            mock_exec.return_value = {"columns": ["x"], "rows": [[1]], "row_count": 1}

            result = await bigquery_sql_execute({"query": "SELECT 1"}, ctx)

        assert result["columns"] == ["x"]
        assert result["row_count"] == 1

    @pytest.mark.asyncio
    async def test_no_connector_returns_error(self):
        from app.mcp.tools.bigquery_tools import bigquery_sql_execute

        ctx = _make_context()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        ctx["db"].execute = AsyncMock(return_value=mock_result)

        result = await bigquery_sql_execute({"query": "SELECT 1"}, ctx)
        assert "error" in result or result.get("error") is True

    @pytest.mark.asyncio
    async def test_passes_max_rows(self):
        from app.mcp.tools.bigquery_tools import bigquery_sql_execute

        ctx = _make_context()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = _mock_connector()
        ctx["db"].execute = AsyncMock(return_value=mock_result)

        with patch("app.mcp.tools.bigquery_tools.decrypt_credentials") as mock_decrypt, \
             patch("app.mcp.tools.bigquery_tools.execute_query", new_callable=AsyncMock) as mock_exec:
            mock_decrypt.return_value = {"service_account_json": {}, "project_id": "test"}
            mock_exec.return_value = {"columns": [], "rows": [], "row_count": 0}

            await bigquery_sql_execute({"query": "SELECT 1", "max_rows": 500}, ctx)
            mock_exec.assert_called_once()
            assert mock_exec.call_args.kwargs.get("max_rows") == 500 or mock_exec.call_args[1].get("max_rows") == 500

    @pytest.mark.asyncio
    async def test_default_max_rows_1000(self):
        from app.mcp.tools.bigquery_tools import bigquery_sql_execute

        ctx = _make_context()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = _mock_connector()
        ctx["db"].execute = AsyncMock(return_value=mock_result)

        with patch("app.mcp.tools.bigquery_tools.decrypt_credentials") as mock_decrypt, \
             patch("app.mcp.tools.bigquery_tools.execute_query", new_callable=AsyncMock) as mock_exec:
            mock_decrypt.return_value = {"service_account_json": {}, "project_id": "test"}
            mock_exec.return_value = {"columns": [], "rows": [], "row_count": 0}

            await bigquery_sql_execute({"query": "SELECT 1"}, ctx)
            call_kwargs = mock_exec.call_args.kwargs if mock_exec.call_args.kwargs else {}
            assert call_kwargs.get("max_rows", 1000) == 1000


class TestBigquerySchemaAndCostTools:

    @pytest.mark.asyncio
    async def test_schema_tool_execute(self):
        from app.mcp.tools.bigquery_tools import bigquery_schema_execute

        ctx = _make_context()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = _mock_connector()
        ctx["db"].execute = AsyncMock(return_value=mock_result)

        with patch("app.mcp.tools.bigquery_tools.decrypt_credentials") as mock_decrypt, \
             patch("app.mcp.tools.bigquery_tools.discover_schema", new_callable=AsyncMock) as mock_disc:
            mock_decrypt.return_value = {"service_account_json": {}, "project_id": "test"}
            mock_disc.return_value = {"datasets": []}

            result = await bigquery_schema_execute({}, ctx)
        assert "datasets" in result

    @pytest.mark.asyncio
    async def test_cost_estimate_tool_execute(self):
        from app.mcp.tools.bigquery_tools import bigquery_cost_estimate_execute

        ctx = _make_context()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = _mock_connector()
        ctx["db"].execute = AsyncMock(return_value=mock_result)

        with patch("app.mcp.tools.bigquery_tools.decrypt_credentials") as mock_decrypt, \
             patch("app.mcp.tools.bigquery_tools.estimate_query_cost", new_callable=AsyncMock) as mock_est:
            mock_decrypt.return_value = {"service_account_json": {}, "project_id": "test"}
            mock_est.return_value = {"estimated_bytes": 1000, "estimated_cost_usd": 0.000005}

            result = await bigquery_cost_estimate_execute({"query": "SELECT 1"}, ctx)
        assert "estimated_bytes" in result

    @pytest.mark.asyncio
    async def test_tool_decrypts_credentials(self):
        from app.mcp.tools.bigquery_tools import bigquery_sql_execute

        ctx = _make_context()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = _mock_connector()
        ctx["db"].execute = AsyncMock(return_value=mock_result)

        with patch("app.mcp.tools.bigquery_tools.decrypt_credentials") as mock_decrypt, \
             patch("app.mcp.tools.bigquery_tools.execute_query", new_callable=AsyncMock) as mock_exec:
            mock_decrypt.return_value = {"service_account_json": {}, "project_id": "test"}
            mock_exec.return_value = {"columns": [], "rows": [], "row_count": 0}

            await bigquery_sql_execute({"query": "SELECT 1"}, ctx)
            mock_decrypt.assert_called_once_with("encrypted_data")
