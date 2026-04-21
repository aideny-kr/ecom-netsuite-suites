from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

    @pytest.mark.asyncio
    async def test_create_succeeds_when_share_fails(self):
        """Sheet is returned successfully even if auto-share step fails (share is best-effort)."""
        with patch("app.mcp.tools.sheets_tools._get_sheets_connector") as mock_conn, \
             patch("app.mcp.tools.sheets_tools.create_spreadsheet") as mock_create, \
             patch("app.mcp.tools.sheets_tools.share_spreadsheet") as mock_share, \
             patch("app.mcp.tools.sheets_tools.decrypt_credentials") as mock_decrypt, \
             patch("app.mcp.tools.sheets_tools._get_user_email") as mock_email:
            mock_conn.return_value = MagicMock(encrypted_credentials="encrypted")
            mock_decrypt.return_value = {"type": "service_account"}
            mock_email.return_value = "user@example.com"
            mock_create.return_value = {"spreadsheet_id": "abc123", "url": "https://docs.google.com/spreadsheets/d/abc123"}
            mock_share.side_effect = Exception("share failed")
            result = await sheets_create_execute({"title": "Test"}, _CONTEXT)
        assert result["error"] is False
        assert result["spreadsheet_id"] == "abc123"

    @pytest.mark.asyncio
    async def test_create_succeeds_when_no_user_email(self):
        """Sheet created even if actor's email cannot be resolved."""
        with patch("app.mcp.tools.sheets_tools._get_sheets_connector") as mock_conn, \
             patch("app.mcp.tools.sheets_tools.create_spreadsheet") as mock_create, \
             patch("app.mcp.tools.sheets_tools.share_spreadsheet") as mock_share, \
             patch("app.mcp.tools.sheets_tools.decrypt_credentials") as mock_decrypt, \
             patch("app.mcp.tools.sheets_tools._get_user_email") as mock_email:
            mock_conn.return_value = MagicMock(encrypted_credentials="encrypted")
            mock_decrypt.return_value = {"type": "service_account"}
            mock_email.return_value = None
            mock_create.return_value = {"spreadsheet_id": "abc123", "url": "https://docs.google.com/spreadsheets/d/abc123"}
            result = await sheets_create_execute({"title": "Test"}, _CONTEXT)
        assert result["error"] is False
        assert result["shared_with"] is None
        mock_share.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_returns_error_on_missing_context(self):
        result = await sheets_create_execute({"title": "Test"}, {})
        assert result["error"] is True
        assert "context" in result["message"].lower() or "missing" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_create_echoes_title_in_result(self):
        """Title passed in params must appear in the result so the orchestrator can display it on the card."""
        with patch("app.mcp.tools.sheets_tools._get_sheets_connector") as mock_conn, \
             patch("app.mcp.tools.sheets_tools.create_spreadsheet") as mock_create, \
             patch("app.mcp.tools.sheets_tools.share_spreadsheet"), \
             patch("app.mcp.tools.sheets_tools.decrypt_credentials") as mock_decrypt, \
             patch("app.mcp.tools.sheets_tools._get_user_email") as mock_email:
            mock_conn.return_value = MagicMock(encrypted_credentials="encrypted")
            mock_decrypt.return_value = {"type": "service_account"}
            mock_email.return_value = None
            mock_create.return_value = {"spreadsheet_id": "abc", "url": "https://docs.google.com/spreadsheets/d/abc"}
            result = await sheets_create_execute({"title": "Q4 Sales Export"}, _CONTEXT)
        assert result["title"] == "Q4 Sales Export"


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


class TestSheetsToolsSharedDrive:
    @pytest.mark.asyncio
    async def test_create_passes_shared_drive_id_when_set(self):
        """metadata_json.shared_drive_id flows to create_spreadsheet."""
        from app.mcp.tools.sheets_tools import sheets_create_execute

        mock_connector = MagicMock()
        mock_connector.encrypted_credentials = b"enc"
        mock_connector.metadata_json = {
            "client_email": "sa@x.iam.gserviceaccount.com",
            "shared_drive_id": "0ACabcdEFGH1234567890",
        }

        with patch(
            "app.mcp.tools.sheets_tools._get_sheets_connector",
            new=AsyncMock(return_value=mock_connector),
        ), patch(
            "app.mcp.tools.sheets_tools.decrypt_credentials",
            return_value={"service_account_json": {"type": "service_account"}},
        ), patch(
            "app.mcp.tools.sheets_tools.create_spreadsheet",
            new=AsyncMock(return_value={"spreadsheet_id": "abc", "url": "https://..."}),
        ) as mock_create, patch(
            "app.mcp.tools.sheets_tools._get_user_email",
            new=AsyncMock(return_value=None),
        ):
            result = await sheets_create_execute(
                {"title": "My Sheet"},
                {"tenant_id": "t", "db": MagicMock(), "actor_id": "a"},
            )

        assert result["error"] is False
        mock_create.assert_awaited_once()
        assert mock_create.call_args.kwargs["shared_drive_id"] == "0ACabcdEFGH1234567890"

    @pytest.mark.asyncio
    async def test_create_skips_share_when_in_shared_drive(self):
        """When a Shared Drive is in play, share_spreadsheet should NOT be called
        (members already have drive-level access)."""
        from app.mcp.tools.sheets_tools import sheets_create_execute

        mock_connector = MagicMock()
        mock_connector.encrypted_credentials = b"enc"
        mock_connector.metadata_json = {
            "client_email": "sa@x.iam.gserviceaccount.com",
            "shared_drive_id": "0ACabcdEFGH1234567890",
        }

        with patch(
            "app.mcp.tools.sheets_tools._get_sheets_connector",
            new=AsyncMock(return_value=mock_connector),
        ), patch(
            "app.mcp.tools.sheets_tools.decrypt_credentials",
            return_value={"service_account_json": {}},
        ), patch(
            "app.mcp.tools.sheets_tools.create_spreadsheet",
            new=AsyncMock(return_value={"spreadsheet_id": "abc", "url": "u"}),
        ), patch(
            "app.mcp.tools.sheets_tools._get_user_email",
            new=AsyncMock(return_value="user@example.com"),
        ), patch(
            "app.mcp.tools.sheets_tools.share_spreadsheet",
            new=AsyncMock(),
        ) as mock_share:
            result = await sheets_create_execute(
                {"title": "My Sheet"},
                {"tenant_id": "t", "db": MagicMock(), "actor_id": "a"},
            )

        assert result["error"] is False
        mock_share.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_still_shares_when_no_shared_drive(self):
        """Existing behavior: without Shared Drive, share_spreadsheet runs."""
        from app.mcp.tools.sheets_tools import sheets_create_execute

        mock_connector = MagicMock()
        mock_connector.encrypted_credentials = b"enc"
        mock_connector.metadata_json = {"client_email": "sa@x.iam.gserviceaccount.com"}

        with patch(
            "app.mcp.tools.sheets_tools._get_sheets_connector",
            new=AsyncMock(return_value=mock_connector),
        ), patch(
            "app.mcp.tools.sheets_tools.decrypt_credentials",
            return_value={"service_account_json": {}},
        ), patch(
            "app.mcp.tools.sheets_tools.create_spreadsheet",
            new=AsyncMock(return_value={"spreadsheet_id": "abc", "url": "u"}),
        ), patch(
            "app.mcp.tools.sheets_tools._get_user_email",
            new=AsyncMock(return_value="user@example.com"),
        ), patch(
            "app.mcp.tools.sheets_tools.share_spreadsheet",
            new=AsyncMock(),
        ) as mock_share:
            await sheets_create_execute(
                {"title": "My Sheet"},
                {"tenant_id": "t", "db": MagicMock(), "actor_id": "a"},
            )

        mock_share.assert_awaited_once()


class TestSheetsReadRangeExecute:
    @pytest.mark.asyncio
    async def test_reads_data(self):
        from app.mcp.tools.sheets_tools import sheets_read_range_execute

        mock_connector = MagicMock()
        mock_connector.encrypted_credentials = b"enc"
        mock_connector.metadata_json = {"client_email": "sa@x.iam.gserviceaccount.com"}

        with patch(
            "app.mcp.tools.sheets_tools._get_sheets_connector",
            new=AsyncMock(return_value=mock_connector),
        ), patch(
            "app.mcp.tools.sheets_tools.decrypt_credentials",
            return_value={"service_account_json": {"type": "service_account"}},
        ), patch(
            "app.mcp.tools.sheets_tools.read_range",
            new=AsyncMock(return_value={
                "range": "Sheet1!A1:B3",
                "values": [["Product", "Qty"], ["Apples", "12"], ["Pears", "7"]],
            }),
        ) as mock_read:
            result = await sheets_read_range_execute(
                {"spreadsheet_id": "abc", "range": "Sheet1!A1:B3"},
                {"tenant_id": "t", "db": MagicMock()},
            )

        assert result["error"] is False
        assert result["range"] == "Sheet1!A1:B3"
        assert result["row_count"] == 3
        assert result["values"][0] == ["Product", "Qty"]
        # Envelope unwrap: raw SA dict flows to service, not the envelope
        assert mock_read.call_args.kwargs["credentials"] == {"type": "service_account"}

    @pytest.mark.asyncio
    async def test_returns_error_when_no_connector(self):
        from app.mcp.tools.sheets_tools import sheets_read_range_execute

        with patch(
            "app.mcp.tools.sheets_tools._get_sheets_connector",
            new=AsyncMock(return_value=None),
        ):
            result = await sheets_read_range_execute(
                {"spreadsheet_id": "abc"},
                {"tenant_id": "t", "db": MagicMock()},
            )
        assert result["error"] is True
        assert "not configured" in result["message"]

    @pytest.mark.asyncio
    async def test_returns_error_on_missing_spreadsheet_id(self):
        from app.mcp.tools.sheets_tools import sheets_read_range_execute

        mock_connector = MagicMock()
        mock_connector.encrypted_credentials = b"enc"
        mock_connector.metadata_json = {}

        with patch(
            "app.mcp.tools.sheets_tools._get_sheets_connector",
            new=AsyncMock(return_value=mock_connector),
        ):
            result = await sheets_read_range_execute(
                {},
                {"tenant_id": "t", "db": MagicMock()},
            )
        assert result["error"] is True
        assert "spreadsheet_id" in result["message"]
