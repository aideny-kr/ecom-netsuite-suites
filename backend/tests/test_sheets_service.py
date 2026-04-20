import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.services.sheets_service import (
    create_spreadsheet,
    share_spreadsheet,
    validate_connection,
    write_range,
)


class TestCreateSpreadsheet:
    @pytest.mark.asyncio
    async def test_returns_spreadsheet_id_and_url(self):
        mock_service = MagicMock()
        mock_service.spreadsheets().create().execute.return_value = {
            "spreadsheetId": "abc123",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/abc123",
        }
        with patch("app.services.sheets_service._build_sheets_service", return_value=mock_service):
            result = await create_spreadsheet(
                credentials={"type": "service_account"},
                title="Test Sheet",
            )
        assert result["spreadsheet_id"] == "abc123"
        assert "docs.google.com" in result["url"]

    @pytest.mark.asyncio
    async def test_raises_on_missing_credentials(self):
        with pytest.raises(ValueError, match="credentials"):
            await create_spreadsheet(credentials=None, title="Test")


class TestWriteRange:
    @pytest.mark.asyncio
    async def test_writes_data_and_returns_updated_range(self):
        mock_service = MagicMock()
        mock_service.spreadsheets().values().update().execute.return_value = {
            "updatedRange": "Sheet1!A1:C3",
            "updatedRows": 3,
            "updatedColumns": 3,
        }
        with patch("app.services.sheets_service._build_sheets_service", return_value=mock_service):
            result = await write_range(
                credentials={"type": "service_account"},
                spreadsheet_id="abc123",
                data=[["Name", "Age"], ["Alice", 30], ["Bob", 25]],
            )
        assert result["updated_rows"] == 3

    @pytest.mark.asyncio
    async def test_rejects_empty_data(self):
        with pytest.raises(ValueError, match="data"):
            await write_range(
                credentials={"type": "service_account"},
                spreadsheet_id="abc123",
                data=[],
            )


class TestShareSpreadsheet:
    @pytest.mark.asyncio
    async def test_shares_with_email(self):
        mock_drive = MagicMock()
        mock_drive.permissions().create().execute.return_value = {"id": "perm1"}
        with patch("app.services.sheets_service._build_drive_service", return_value=mock_drive):
            result = await share_spreadsheet(
                credentials={"type": "service_account"},
                spreadsheet_id="abc123",
                email="user@example.com",
            )
        assert result["permission_id"] == "perm1"


class TestValidateConnection:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        mock_service = MagicMock()
        mock_service.spreadsheets().create().execute.return_value = {
            "spreadsheetId": "test123",
        }
        mock_drive = MagicMock()
        mock_drive.files().delete().execute.return_value = None
        with patch("app.services.sheets_service._build_sheets_service", return_value=mock_service), \
             patch("app.services.sheets_service._build_drive_service", return_value=mock_drive):
            result = await validate_connection(credentials={"type": "service_account"})
        assert result["valid"] is True

    @pytest.mark.asyncio
    async def test_returns_valid_when_cleanup_fails(self):
        """Connection is valid if create succeeded, even if delete cleanup fails."""
        mock_service = MagicMock()
        mock_service.spreadsheets().create().execute.return_value = {"spreadsheetId": "test123"}
        mock_drive = MagicMock()
        mock_drive.files().delete().execute.side_effect = Exception("delete failed")
        with patch("app.services.sheets_service._build_sheets_service", return_value=mock_service), \
             patch("app.services.sheets_service._build_drive_service", return_value=mock_drive):
            result = await validate_connection(credentials={"type": "service_account"})
        assert result["valid"] is True

    @pytest.mark.asyncio
    async def test_returns_invalid_when_create_fails(self):
        """Connection is invalid if spreadsheet create fails."""
        mock_service = MagicMock()
        mock_service.spreadsheets().create().execute.side_effect = Exception("API error")
        with patch("app.services.sheets_service._build_sheets_service", return_value=mock_service):
            result = await validate_connection(credentials={"type": "service_account"})
        assert result["valid"] is False
        assert "API error" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_invalid_on_timeout(self):
        """Connection is invalid if validation times out."""
        async def _slow_thread(*args, **kwargs):
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            result = await validate_connection(credentials={"type": "service_account"})
        assert result["valid"] is False
        assert result["error"] == "timeout"


class TestValidateConnectionSharedDrive:
    @pytest.mark.asyncio
    async def test_shared_drive_happy_path(self):
        mock_drive = MagicMock()
        mock_drive.drives().get().execute.return_value = {"id": "0ACabc", "kind": "drive#drive"}
        mock_drive.files().create().execute.return_value = {"id": "file_abc"}
        mock_drive.files().delete().execute.return_value = None

        with patch("app.services.sheets_service._build_drive_service", return_value=mock_drive):
            result = await validate_connection(
                credentials={"type": "service_account"},
                shared_drive_id="0ACabcdEFGH1234567890",
            )

        assert result == {"valid": True}
        # Pre-flight drives.get called with the drive ID
        mock_drive.drives().get.assert_called_with(driveId="0ACabcdEFGH1234567890")
        # files.create body includes parents + spreadsheet mimeType + supportsAllDrives
        create_kwargs = mock_drive.files().create.call_args.kwargs
        assert create_kwargs["body"]["mimeType"] == "application/vnd.google-apps.spreadsheet"
        assert create_kwargs["body"]["parents"] == ["0ACabcdEFGH1234567890"]
        assert create_kwargs["supportsAllDrives"] is True
        # cleanup delete also uses supportsAllDrives
        delete_kwargs = mock_drive.files().delete.call_args.kwargs
        assert delete_kwargs["supportsAllDrives"] is True

    @pytest.mark.asyncio
    async def test_shared_drive_not_found_returns_valid_false(self):
        from googleapiclient.errors import HttpError
        mock_drive = MagicMock()
        # Simulate Drive API 404 on drives.get
        err = HttpError(MagicMock(status=404), b'{"error": "not found"}')
        mock_drive.drives().get().execute.side_effect = err

        with patch("app.services.sheets_service._build_drive_service", return_value=mock_drive):
            result = await validate_connection(
                credentials={"type": "service_account"},
                shared_drive_id="0ACnotexists1234567890",
            )

        assert result["valid"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_shared_drive_uses_sheets_api_branch(self):
        """When shared_drive_id is None, original Sheets API path runs unchanged."""
        mock_sheets = MagicMock()
        mock_sheets.spreadsheets().create().execute.return_value = {"spreadsheetId": "abc"}
        mock_drive = MagicMock()

        with patch("app.services.sheets_service._build_sheets_service", return_value=mock_sheets), \
             patch("app.services.sheets_service._build_drive_service", return_value=mock_drive):
            result = await validate_connection(credentials={"type": "service_account"})

        assert result == {"valid": True}
        # Sheets API was used, not Drive API file creation
        mock_sheets.spreadsheets().create.assert_called()
        # drives().get was NOT used
        mock_drive.drives().get.assert_not_called()
