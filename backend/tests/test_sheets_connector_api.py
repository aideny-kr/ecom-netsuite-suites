"""Tests for the Google Sheets connector API endpoints."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.mcp_connector import SheetsConnectorCreate, SheetsTestRequest


class TestSheetsTestConnection:
    """POST /mcp-connectors/google-sheets/test"""

    @pytest.mark.asyncio
    async def test_valid_credentials_returns_true(self):
        from app.api.v1.mcp_connectors import test_sheets_connection

        request = SheetsTestRequest(service_account_json={"type": "service_account"})
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()

        with patch(
            "app.api.v1.mcp_connectors.validate_sheets_connection",
            new=AsyncMock(return_value={"valid": True}),
        ):
            result = await test_sheets_connection(request, mock_user, mock_db)

        assert result.valid is True
        assert result.error is None

    @pytest.mark.asyncio
    async def test_invalid_credentials_returns_false_with_error(self):
        from app.api.v1.mcp_connectors import test_sheets_connection

        request = SheetsTestRequest(service_account_json={"type": "bad"})
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()

        with patch(
            "app.api.v1.mcp_connectors.validate_sheets_connection",
            new=AsyncMock(return_value={"valid": False, "error": "bad creds"}),
        ):
            result = await test_sheets_connection(request, mock_user, mock_db)

        assert result.valid is False
        assert result.error == "bad creds"

    @pytest.mark.asyncio
    async def test_missing_error_key_returns_none_error(self):
        """If validation dict has no 'error' key, error should be None."""
        from app.api.v1.mcp_connectors import test_sheets_connection

        request = SheetsTestRequest(service_account_json={"type": "service_account"})
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()

        with patch(
            "app.api.v1.mcp_connectors.validate_sheets_connection",
            new=AsyncMock(return_value={"valid": False}),
        ):
            result = await test_sheets_connection(request, mock_user, AsyncMock())

        assert result.valid is False
        assert result.error is None


class TestSheetsCreateConnector:
    """POST /mcp-connectors/google-sheets"""

    @pytest.mark.asyncio
    async def test_creates_connector_on_valid_credentials(self):
        from app.api.v1.mcp_connectors import create_sheets_connector

        request = SheetsConnectorCreate(
            service_account_json={
                "type": "service_account",
                "client_email": "sa@proj.iam.gserviceaccount.com",
            }
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch(
                "app.api.v1.mcp_connectors.validate_sheets_connection",
                new=AsyncMock(return_value={"valid": True}),
            ),
            patch("app.api.v1.mcp_connectors.encrypt_credentials") as mock_encrypt,
            patch("app.api.v1.mcp_connectors.audit_service") as mock_audit,
        ):
            mock_encrypt.return_value = "encrypted_blob"
            mock_audit.log_event = AsyncMock()

            await create_sheets_connector(request, mock_user, mock_db)

        mock_db.add.assert_called_once()
        connector = mock_db.add.call_args[0][0]
        assert connector.provider == "google_sheets"
        assert connector.status == "active"
        assert connector.server_url == "https://sheets.googleapis.com"
        assert connector.auth_type == "service_account"

    @pytest.mark.asyncio
    async def test_rejects_invalid_credentials_with_400(self):
        from fastapi import HTTPException

        from app.api.v1.mcp_connectors import create_sheets_connector

        request = SheetsConnectorCreate(service_account_json={"type": "bad"})
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()

        with patch(
            "app.api.v1.mcp_connectors.validate_sheets_connection",
            new=AsyncMock(return_value={"valid": False, "error": "bad creds"}),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await create_sheets_connector(request, mock_user, AsyncMock())

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_encrypts_credentials(self):
        from app.api.v1.mcp_connectors import create_sheets_connector

        sa_json = {"type": "service_account", "client_email": "sa@proj.iam.gserviceaccount.com"}
        request = SheetsConnectorCreate(service_account_json=sa_json)
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch(
                "app.api.v1.mcp_connectors.validate_sheets_connection",
                new=AsyncMock(return_value={"valid": True}),
            ),
            patch("app.api.v1.mcp_connectors.encrypt_credentials") as mock_encrypt,
            patch("app.api.v1.mcp_connectors.audit_service") as mock_audit,
        ):
            mock_encrypt.return_value = "encrypted_blob"
            mock_audit.log_event = AsyncMock()

            await create_sheets_connector(request, mock_user, mock_db)

        mock_encrypt.assert_called_once()
        call_arg = mock_encrypt.call_args[0][0]
        assert "service_account_json" in call_arg
        assert call_arg["service_account_json"] == sa_json

    @pytest.mark.asyncio
    async def test_revokes_existing_connector(self):
        from app.api.v1.mcp_connectors import create_sheets_connector

        request = SheetsConnectorCreate(
            service_account_json={"type": "service_account"},
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()

        old_connector = MagicMock()
        old_connector.status = "active"
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = old_connector
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch(
                "app.api.v1.mcp_connectors.validate_sheets_connection",
                new=AsyncMock(return_value={"valid": True}),
            ),
            patch("app.api.v1.mcp_connectors.encrypt_credentials") as mock_encrypt,
            patch("app.api.v1.mcp_connectors.audit_service") as mock_audit,
        ):
            mock_encrypt.return_value = "encrypted_blob"
            mock_audit.log_event = AsyncMock()

            await create_sheets_connector(request, mock_user, mock_db)

        assert old_connector.status == "revoked"

    @pytest.mark.asyncio
    async def test_audit_logged(self):
        from app.api.v1.mcp_connectors import create_sheets_connector

        request = SheetsConnectorCreate(service_account_json={"type": "service_account"})
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch(
                "app.api.v1.mcp_connectors.validate_sheets_connection",
                new=AsyncMock(return_value={"valid": True}),
            ),
            patch("app.api.v1.mcp_connectors.encrypt_credentials") as mock_encrypt,
            patch("app.api.v1.mcp_connectors.audit_service") as mock_audit,
        ):
            mock_encrypt.return_value = "encrypted_blob"
            mock_audit.log_event = AsyncMock()

            await create_sheets_connector(request, mock_user, mock_db)

        mock_audit.log_event.assert_called_once()
        call_kwargs = mock_audit.log_event.call_args.kwargs
        assert call_kwargs["action"] == "mcp_connector.create"
        assert call_kwargs["category"] == "mcp_connector"
        assert call_kwargs["payload"]["provider"] == "google_sheets"

    @pytest.mark.asyncio
    async def test_connector_has_no_discovered_tools(self):
        """Sheets tools are registered locally — discovered_tools must be None."""
        from app.api.v1.mcp_connectors import create_sheets_connector

        request = SheetsConnectorCreate(service_account_json={"type": "service_account"})
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch(
                "app.api.v1.mcp_connectors.validate_sheets_connection",
                new=AsyncMock(return_value={"valid": True}),
            ),
            patch("app.api.v1.mcp_connectors.encrypt_credentials") as mock_encrypt,
            patch("app.api.v1.mcp_connectors.audit_service") as mock_audit,
        ):
            mock_encrypt.return_value = "encrypted"
            mock_audit.log_event = AsyncMock()

            await create_sheets_connector(request, mock_user, mock_db)

        connector = mock_db.add.call_args[0][0]
        assert connector.discovered_tools is None

    @pytest.mark.asyncio
    async def test_client_email_stored_in_metadata(self):
        """client_email from service account JSON must be in metadata_json."""
        from app.api.v1.mcp_connectors import create_sheets_connector

        email = "sa@myproject.iam.gserviceaccount.com"
        request = SheetsConnectorCreate(
            service_account_json={"type": "service_account", "client_email": email}
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch(
                "app.api.v1.mcp_connectors.validate_sheets_connection",
                new=AsyncMock(return_value={"valid": True}),
            ),
            patch("app.api.v1.mcp_connectors.encrypt_credentials") as mock_encrypt,
            patch("app.api.v1.mcp_connectors.audit_service") as mock_audit,
        ):
            mock_encrypt.return_value = "encrypted"
            mock_audit.log_event = AsyncMock()

            await create_sheets_connector(request, mock_user, mock_db)

        connector = mock_db.add.call_args[0][0]
        assert connector.metadata_json["client_email"] == email


class TestSheetsSchemas:
    """Validate Pydantic schema constraints."""

    def test_test_request_valid(self):
        req = SheetsTestRequest(service_account_json={"type": "service_account"})
        assert req.service_account_json["type"] == "service_account"

    def test_connector_create_default_label(self):
        req = SheetsConnectorCreate(service_account_json={"type": "service_account"})
        assert req.label == "Google Sheets"

    def test_connector_create_custom_label(self):
        req = SheetsConnectorCreate(
            service_account_json={"type": "service_account"},
            label="My Sheets",
        )
        assert req.label == "My Sheets"

    def test_connector_create_empty_label_rejected(self):
        with pytest.raises(Exception):
            SheetsConnectorCreate(
                service_account_json={"type": "service_account"},
                label="",
            )
