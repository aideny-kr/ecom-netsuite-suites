"""Tests for the Google Sheets connector API endpoints."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.mcp_connector import SheetsConnectorCreate, SheetsTestRequest

_VALID_SA = {
    "type": "service_account",
    "client_email": "sa@proj.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
}


class TestSheetsSharedDriveSchema:
    def test_test_request_accepts_shared_drive_id(self):
        from app.schemas.mcp_connector import SheetsTestRequest
        r = SheetsTestRequest(
            service_account_json=_VALID_SA,
            shared_drive_id="0ACabcdEFGH1234567890",
        )
        assert r.shared_drive_id == "0ACabcdEFGH1234567890"

    def test_test_request_defaults_shared_drive_id_to_none(self):
        from app.schemas.mcp_connector import SheetsTestRequest
        r = SheetsTestRequest(service_account_json=_VALID_SA)
        assert r.shared_drive_id is None

    def test_test_request_coerces_empty_string_to_none(self):
        from app.schemas.mcp_connector import SheetsTestRequest
        r = SheetsTestRequest(service_account_json=_VALID_SA, shared_drive_id="")
        assert r.shared_drive_id is None

    def test_test_request_rejects_invalid_format(self):
        from app.schemas.mcp_connector import SheetsTestRequest
        with pytest.raises(ValueError, match="shared_drive_id"):
            SheetsTestRequest(service_account_json=_VALID_SA, shared_drive_id="too-short")

    def test_create_request_accepts_shared_drive_id(self):
        from app.schemas.mcp_connector import SheetsConnectorCreate
        r = SheetsConnectorCreate(
            service_account_json=_VALID_SA,
            shared_drive_id="0ACabcdEFGH1234567890",
        )
        assert r.shared_drive_id == "0ACabcdEFGH1234567890"


def _make_mock_db():
    """Return a db mock that matches real SQLAlchemy async session behaviour.

    db.add / db.add_all are synchronous in SQLAlchemy — using AsyncMock for
    them causes RuntimeWarnings.  Only execute, commit, refresh, rollback and
    flush are coroutines.
    """
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    # db.add / db.add_all stay as synchronous MagicMock (the default)
    return db


class TestSheetsTestConnection:
    """POST /mcp-connectors/google-sheets/test"""

    @pytest.mark.asyncio
    async def test_valid_credentials_returns_true(self):
        from app.api.v1.mcp_connectors import test_sheets_connection

        request = SheetsTestRequest(service_account_json=_VALID_SA)
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = _make_mock_db()

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

        request = SheetsTestRequest(service_account_json={"type": "bad", "client_email": "x@y.com", "private_key": "pk"})
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = _make_mock_db()

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

        request = SheetsTestRequest(service_account_json=_VALID_SA)
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()

        with patch(
            "app.api.v1.mcp_connectors.validate_sheets_connection",
            new=AsyncMock(return_value={"valid": False}),
        ):
            result = await test_sheets_connection(request, mock_user, _make_mock_db())

        assert result.valid is False
        assert result.error is None


class TestSheetsCreateConnector:
    """POST /mcp-connectors/google-sheets"""

    @pytest.mark.asyncio
    async def test_creates_connector_on_valid_credentials(self):
        from app.api.v1.mcp_connectors import create_sheets_connector

        request = SheetsConnectorCreate(service_account_json=_VALID_SA)
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = _make_mock_db()

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

        request = SheetsConnectorCreate(service_account_json={"type": "bad", "client_email": "x@y.com", "private_key": "pk"})
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()

        with patch(
            "app.api.v1.mcp_connectors.validate_sheets_connection",
            new=AsyncMock(return_value={"valid": False, "error": "bad creds"}),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await create_sheets_connector(request, mock_user, _make_mock_db())

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_encrypts_credentials(self):
        from app.api.v1.mcp_connectors import create_sheets_connector

        sa_json = _VALID_SA
        request = SheetsConnectorCreate(service_account_json=sa_json)
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = _make_mock_db()
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
            service_account_json=_VALID_SA,
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = _make_mock_db()

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

        request = SheetsConnectorCreate(service_account_json=_VALID_SA)
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = _make_mock_db()
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

        request = SheetsConnectorCreate(service_account_json=_VALID_SA)
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = _make_mock_db()
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
            service_account_json={**_VALID_SA, "client_email": email}
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = _make_mock_db()
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


    @pytest.mark.asyncio
    async def test_response_does_not_leak_encrypted_credentials(self):
        """Critical: the 201 response must NOT expose encrypted_credentials.

        Two-part check:
        1. The endpoint stores encrypted_credentials on the ORM object (so the
           field IS present and populated before serialization).
        2. McpConnectorResponse — the response_model — does NOT declare
           encrypted_credentials, so FastAPI strips it before sending to the
           client.  We assert the whitelist directly on the schema.
        """
        from app.api.v1.mcp_connectors import create_sheets_connector
        from app.schemas.mcp_connector import McpConnectorResponse

        request = SheetsConnectorCreate(
            service_account_json={
                "type": "service_account",
                "client_email": "sa@x.iam.gserviceaccount.com",
                "private_key": "...",
            }
        )
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()
        mock_db = _make_mock_db()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch(
                "app.api.v1.mcp_connectors.validate_sheets_connection",
                new=AsyncMock(return_value={"valid": True}),
            ),
            patch(
                "app.api.v1.mcp_connectors.encrypt_credentials",
                return_value=b"SUPER_SECRET_ENCRYPTED_BLOB",
            ),
            patch("app.api.v1.mcp_connectors.audit_service.log_event", new=AsyncMock()),
        ):
            connector = await create_sheets_connector(request, mock_user, mock_db)

        # Part 1: the ORM object DOES carry the encrypted blob (proves the fix
        # is needed — without response_model the raw object would expose it).
        assert connector.encrypted_credentials == b"SUPER_SECRET_ENCRYPTED_BLOB"

        # Part 2: McpConnectorResponse is the whitelist — encrypted_credentials
        # must NOT be a declared field, so FastAPI will never include it in the
        # HTTP response body.
        response_field_names = set(McpConnectorResponse.model_fields.keys())
        assert "encrypted_credentials" not in response_field_names


_VALID_SA = {
    "type": "service_account",
    "client_email": "sa@proj.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
}


class TestSheetsSchemas:
    """Validate Pydantic schema constraints."""

    def test_test_request_valid(self):
        req = SheetsTestRequest(service_account_json=_VALID_SA)
        assert req.service_account_json["type"] == "service_account"

    def test_connector_create_default_label(self):
        req = SheetsConnectorCreate(service_account_json=_VALID_SA)
        assert req.label == "Google Sheets"

    def test_connector_create_custom_label(self):
        req = SheetsConnectorCreate(service_account_json=_VALID_SA, label="My Sheets")
        assert req.label == "My Sheets"

    def test_connector_create_empty_label_rejected(self):
        with pytest.raises(Exception):
            SheetsConnectorCreate(service_account_json=_VALID_SA, label="")

    def test_empty_service_account_json_rejected(self):
        """Empty dict must fail at schema boundary, not surface as 500 from service layer."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="service_account_json"):
            SheetsTestRequest(service_account_json={})
        with pytest.raises(ValidationError, match="service_account_json"):
            SheetsConnectorCreate(service_account_json={})

    def test_missing_client_email_rejected(self):
        from pydantic import ValidationError

        bad = {"type": "service_account", "private_key": "..."}
        with pytest.raises(ValidationError, match="client_email"):
            SheetsTestRequest(service_account_json=bad)
        with pytest.raises(ValidationError, match="client_email"):
            SheetsConnectorCreate(service_account_json=bad)

    def test_missing_private_key_rejected(self):
        from pydantic import ValidationError

        bad = {"type": "service_account", "client_email": "sa@x.iam.gserviceaccount.com"}
        with pytest.raises(ValidationError, match="private_key"):
            SheetsTestRequest(service_account_json=bad)
        with pytest.raises(ValidationError, match="private_key"):
            SheetsConnectorCreate(service_account_json=bad)


class TestSheetsEndpointErrorHandling:
    """Endpoint must convert service-layer ValueError to HTTP 400, not 500."""

    @pytest.mark.asyncio
    async def test_test_endpoint_wraps_value_error_as_400(self):
        """If validate_sheets_connection raises ValueError unexpectedly, endpoint returns 400."""
        from fastapi import HTTPException

        from app.api.v1.mcp_connectors import test_sheets_connection

        request = SheetsTestRequest(service_account_json=_VALID_SA)
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()

        with patch(
            "app.api.v1.mcp_connectors.validate_sheets_connection",
            new=AsyncMock(side_effect=ValueError("credentials required")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await test_sheets_connection(request, mock_user, _make_mock_db())

        assert exc_info.value.status_code == 400
        assert "credentials" in str(exc_info.value.detail).lower()

    @pytest.mark.asyncio
    async def test_create_endpoint_wraps_value_error_as_400(self):
        from fastapi import HTTPException

        from app.api.v1.mcp_connectors import create_sheets_connector

        request = SheetsConnectorCreate(service_account_json=_VALID_SA)
        mock_user = MagicMock()
        mock_user.tenant_id = uuid.uuid4()
        mock_user.id = uuid.uuid4()

        with patch(
            "app.api.v1.mcp_connectors.validate_sheets_connection",
            new=AsyncMock(side_effect=ValueError("credentials required")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await create_sheets_connector(request, mock_user, _make_mock_db())

        assert exc_info.value.status_code == 400
