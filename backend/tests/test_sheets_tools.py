import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.mcp.tools.sheets_tools import sheets_create_execute, sheets_write_range_execute


_CONTEXT = {
    "tenant_id": "ce3dfaad-626f-4992-84e9-500c8291ca0a",
    "actor_id": "1e864ab2-2310-47f8-b50d-1424e407ae03",
    "db": AsyncMock(),
    "correlation_id": "test",
}


class TestSheetsCreateExecute:
    @pytest.mark.asyncio
    async def test_returns_spreadsheet_url(self):
        with patch("app.mcp.tools.sheets_tools._get_sheets_connector") as mock_conn, \
             patch("app.mcp.tools.sheets_tools.create_spreadsheet") as mock_create, \
             patch("app.mcp.tools.sheets_tools.share_spreadsheet") as mock_share, \
             patch("app.mcp.tools.sheets_tools.decrypt_credentials") as mock_decrypt, \
             patch("app.mcp.tools.sheets_tools._get_user_email") as mock_email:
            mock_conn.return_value = MagicMock(encrypted_credentials="encrypted")
            mock_decrypt.return_value = {"type": "service_account"}
            mock_email.return_value = "user@example.com"
            mock_create.return_value = {
                "spreadsheet_id": "abc123",
                "url": "https://docs.google.com/spreadsheets/d/abc123",
            }
            mock_share.return_value = {"permission_id": "perm1"}
            result = await sheets_create_execute(
                {"title": "Test Sheet"},
                _CONTEXT,
            )
        assert result["spreadsheet_id"] == "abc123"
        assert "url" in result
        assert result["error"] is False

    @pytest.mark.asyncio
    async def test_returns_error_when_no_connector(self):
        with patch("app.mcp.tools.sheets_tools._get_sheets_connector", return_value=None):
            result = await sheets_create_execute({"title": "Test"}, _CONTEXT)
        assert result["error"] is True


class TestSheetsWriteRangeExecute:
    @pytest.mark.asyncio
    async def test_writes_data(self):
        with patch("app.mcp.tools.sheets_tools._get_sheets_connector") as mock_conn, \
             patch("app.mcp.tools.sheets_tools.write_range") as mock_write, \
             patch("app.mcp.tools.sheets_tools.decrypt_credentials") as mock_decrypt:
            mock_conn.return_value = MagicMock(encrypted_credentials="encrypted")
            mock_decrypt.return_value = {"type": "service_account"}
            mock_write.return_value = {"updated_rows": 3, "updated_range": "Sheet1!A1:B3", "updated_columns": 2}
            result = await sheets_write_range_execute(
                {
                    "spreadsheet_id": "abc123",
                    "data": [["Name", "Value"], ["A", 1], ["B", 2]],
                },
                _CONTEXT,
            )
        assert result["updated_rows"] == 3
        assert result["error"] is False

    @pytest.mark.asyncio
    async def test_returns_error_on_empty_data(self):
        with patch("app.mcp.tools.sheets_tools._get_sheets_connector") as mock_conn, \
             patch("app.mcp.tools.sheets_tools.decrypt_credentials") as mock_decrypt:
            mock_conn.return_value = MagicMock(encrypted_credentials="encrypted")
            mock_decrypt.return_value = {"type": "service_account"}
            result = await sheets_write_range_execute(
                {"spreadsheet_id": "abc123", "data": []},
                _CONTEXT,
            )
        assert result["error"] is True

    @pytest.mark.asyncio
    async def test_returns_error_when_no_connector(self):
        with patch("app.mcp.tools.sheets_tools._get_sheets_connector", return_value=None):
            result = await sheets_write_range_execute(
                {"spreadsheet_id": "abc123", "data": [["a"]]},
                _CONTEXT,
            )
        assert result["error"] is True
