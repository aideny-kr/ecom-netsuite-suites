"""Tests for BigQuery connector API endpoints — test, create, list, delete."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# These will be imported from the new schemas
from app.schemas.mcp_connector import BigQueryConnectorCreate, BigQueryTestRequest


class TestBigQueryTestConnection:
    """POST /mcp-connectors/bigquery/test"""

    @pytest.mark.asyncio
    async def test_test_connection_success(self):
        from app.api.v1.mcp_connectors import test_bigquery_connection

        request = BigQueryTestRequest(
            project_id="test-project",
            service_account_json={"type": "service_account", "project_id": "test-project"},
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()

        with (
            patch("app.api.v1.mcp_connectors.validate_connection", new_callable=AsyncMock) as mock_validate,
            patch("app.api.v1.mcp_connectors.discover_schema", new_callable=AsyncMock) as mock_discover,
        ):
            mock_validate.return_value = {"valid": True, "error": None}
            mock_discover.return_value = {"datasets": [{"dataset_id": "analytics", "tables": []}]}

            result = await test_bigquery_connection(request, mock_user, mock_db)

        assert result.valid is True
        assert "analytics" in result.datasets

    @pytest.mark.asyncio
    async def test_test_connection_failure(self):
        from app.api.v1.mcp_connectors import test_bigquery_connection

        request = BigQueryTestRequest(
            project_id="bad-project",
            service_account_json={"type": "service_account"},
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()

        with patch("app.api.v1.mcp_connectors.validate_connection", new_callable=AsyncMock) as mock_validate:
            mock_validate.return_value = {"valid": False, "error": "Permission denied"}

            result = await test_bigquery_connection(request, mock_user, AsyncMock())

        assert result.valid is False
        assert "Permission denied" in result.error

    def test_test_connection_missing_project_id(self):
        """project_id is required — Pydantic should reject empty."""
        with pytest.raises(Exception):
            BigQueryTestRequest(
                project_id="",
                service_account_json={"type": "service_account"},
            )

    def test_test_connection_missing_sa_json(self):
        """service_account_json is required."""
        with pytest.raises(Exception):
            BigQueryTestRequest(project_id="test-project")


class TestBigQueryCreateConnector:
    """POST /mcp-connectors/bigquery"""

    @pytest.mark.asyncio
    async def test_create_connector_success(self):
        from app.api.v1.mcp_connectors import create_bigquery_connector

        request = BigQueryConnectorCreate(
            project_id="test-project",
            service_account_json={"type": "service_account", "project_id": "test"},
            default_dataset="analytics",
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()

        # Mock: no existing bigquery connector
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch("app.api.v1.mcp_connectors.validate_connection", new_callable=AsyncMock) as mock_validate,
            patch("app.api.v1.mcp_connectors.discover_schema", new_callable=AsyncMock) as mock_discover,
            patch("app.api.v1.mcp_connectors.encrypt_credentials") as mock_encrypt,
            patch("app.api.v1.mcp_connectors.audit_service") as mock_audit,
        ):
            mock_validate.return_value = {"valid": True, "error": None}
            mock_discover.return_value = {"datasets": [{"dataset_id": "analytics", "tables": []}]}
            mock_encrypt.return_value = "encrypted_blob"
            mock_audit.log_event = AsyncMock()

            await create_bigquery_connector(request, mock_user, mock_db)

        # Verify connector was added to DB
        mock_db.add.assert_called_once()
        connector = mock_db.add.call_args[0][0]
        assert connector.provider == "bigquery"
        assert connector.server_url == "https://bigquery.googleapis.com"
        assert connector.auth_type == "service_account"
        assert connector.status == "active"

    @pytest.mark.asyncio
    async def test_create_connector_encrypts_credentials(self):
        from app.api.v1.mcp_connectors import create_bigquery_connector

        request = BigQueryConnectorCreate(
            project_id="test-project",
            service_account_json={"type": "service_account"},
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch("app.api.v1.mcp_connectors.validate_connection", new_callable=AsyncMock) as mock_val,
            patch("app.api.v1.mcp_connectors.discover_schema", new_callable=AsyncMock) as mock_disc,
            patch("app.api.v1.mcp_connectors.encrypt_credentials") as mock_encrypt,
            patch("app.api.v1.mcp_connectors.audit_service") as mock_audit,
        ):
            mock_val.return_value = {"valid": True, "error": None}
            mock_disc.return_value = {"datasets": []}
            mock_encrypt.return_value = "encrypted_blob"
            mock_audit.log_event = AsyncMock()

            await create_bigquery_connector(request, mock_user, mock_db)
            mock_encrypt.assert_called_once()
            # Verify SA JSON was in the encrypted payload
            call_arg = mock_encrypt.call_args[0][0]
            assert "service_account_json" in call_arg

    @pytest.mark.asyncio
    async def test_create_connector_no_discovered_tools(self):
        """BigQuery tools are local — discovered_tools should be None to avoid
        double-registration as external MCP tools."""
        from app.api.v1.mcp_connectors import create_bigquery_connector

        request = BigQueryConnectorCreate(
            project_id="test-project",
            service_account_json={"type": "service_account"},
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch("app.api.v1.mcp_connectors.validate_connection", new_callable=AsyncMock) as mv,
            patch("app.api.v1.mcp_connectors.discover_schema", new_callable=AsyncMock) as md,
            patch("app.api.v1.mcp_connectors.encrypt_credentials") as me,
            patch("app.api.v1.mcp_connectors.audit_service") as ma,
        ):
            mv.return_value = {"valid": True, "error": None}
            md.return_value = {"datasets": []}
            me.return_value = "encrypted"
            ma.log_event = AsyncMock()

            await create_bigquery_connector(request, mock_user, mock_db)

        connector = mock_db.add.call_args[0][0]
        assert connector.discovered_tools is None

    @pytest.mark.asyncio
    async def test_create_connector_discovers_schema(self):
        from app.api.v1.mcp_connectors import create_bigquery_connector

        request = BigQueryConnectorCreate(
            project_id="test-project",
            service_account_json={"type": "service_account"},
            default_dataset="analytics",
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch("app.api.v1.mcp_connectors.validate_connection", new_callable=AsyncMock) as mv,
            patch("app.api.v1.mcp_connectors.discover_schema", new_callable=AsyncMock) as md,
            patch("app.api.v1.mcp_connectors.encrypt_credentials") as me,
            patch("app.api.v1.mcp_connectors.audit_service") as ma,
        ):
            mv.return_value = {"valid": True, "error": None}
            md.return_value = {
                "datasets": [{"dataset_id": "analytics", "tables": []}, {"dataset_id": "raw", "tables": []}]
            }
            me.return_value = "encrypted"
            ma.log_event = AsyncMock()

            await create_bigquery_connector(request, mock_user, mock_db)

        connector = mock_db.add.call_args[0][0]
        assert "analytics" in connector.metadata_json["datasets_discovered"]
        assert "raw" in connector.metadata_json["datasets_discovered"]

    @pytest.mark.asyncio
    async def test_create_connector_audit_logged(self):
        from app.api.v1.mcp_connectors import create_bigquery_connector

        request = BigQueryConnectorCreate(
            project_id="test-project",
            service_account_json={"type": "service_account"},
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch("app.api.v1.mcp_connectors.validate_connection", new_callable=AsyncMock) as mv,
            patch("app.api.v1.mcp_connectors.discover_schema", new_callable=AsyncMock) as md,
            patch("app.api.v1.mcp_connectors.encrypt_credentials") as me,
            patch("app.api.v1.mcp_connectors.audit_service") as mock_audit,
        ):
            mv.return_value = {"valid": True, "error": None}
            md.return_value = {"datasets": []}
            me.return_value = "encrypted"
            mock_audit.log_event = AsyncMock()

            await create_bigquery_connector(request, mock_user, mock_db)
            mock_audit.log_event.assert_called_once()
            call_kwargs = mock_audit.log_event.call_args.kwargs
            assert call_kwargs["action"] == "mcp_connector.create"
            assert call_kwargs["category"] == "mcp_connector"

    @pytest.mark.asyncio
    async def test_create_connector_duplicate_deactivates_old(self):
        from app.api.v1.mcp_connectors import create_bigquery_connector

        request = BigQueryConnectorCreate(
            project_id="test-project",
            service_account_json={"type": "service_account"},
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()

        # Existing connector
        old_connector = MagicMock()
        old_connector.status = "active"
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = old_connector
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch("app.api.v1.mcp_connectors.validate_connection", new_callable=AsyncMock) as mv,
            patch("app.api.v1.mcp_connectors.discover_schema", new_callable=AsyncMock) as md,
            patch("app.api.v1.mcp_connectors.encrypt_credentials") as me,
            patch("app.api.v1.mcp_connectors.audit_service") as ma,
        ):
            mv.return_value = {"valid": True, "error": None}
            md.return_value = {"datasets": []}
            me.return_value = "encrypted"
            ma.log_event = AsyncMock()

            await create_bigquery_connector(request, mock_user, mock_db)

        # Old connector should be deactivated
        assert old_connector.status == "revoked"


class TestBigQuerySchemas:
    """Validate Pydantic schema constraints."""

    def test_bigquery_test_request_valid(self):
        req = BigQueryTestRequest(
            project_id="my-project",
            service_account_json={"type": "service_account"},
        )
        assert req.project_id == "my-project"

    def test_bigquery_create_with_default_dataset(self):
        req = BigQueryConnectorCreate(
            project_id="my-project",
            service_account_json={"type": "service_account"},
            default_dataset="analytics",
        )
        assert req.default_dataset == "analytics"

    def test_bigquery_create_without_default_dataset(self):
        req = BigQueryConnectorCreate(
            project_id="my-project",
            service_account_json={"type": "service_account"},
        )
        assert req.default_dataset is None
