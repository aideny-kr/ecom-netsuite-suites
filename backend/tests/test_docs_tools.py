"""Tests for docs_tools.docs_create_execute — mirrors sheets_create_execute semantics."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.tools.docs_tools import docs_create_execute

_CONTEXT = {
    "tenant_id": "ce3dfaad-626f-4992-84e9-500c8291ca0a",
    "actor_id": "1e864ab2-2310-47f8-b50d-1424e407ae03",
    "db": AsyncMock(),
    "correlation_id": "test",
}


def _connector(shared_drive_id: str | None = None) -> MagicMock:
    c = MagicMock()
    c.encrypted_credentials = b"enc"
    c.metadata_json = {"client_email": "sa@x.iam.gserviceaccount.com"}
    if shared_drive_id:
        c.metadata_json["shared_drive_id"] = shared_drive_id
    return c


class TestDocsCreateExecute:
    @pytest.mark.asyncio
    async def test_returns_error_on_missing_context(self):
        result = await docs_create_execute({"title": "Notes", "content_markdown": "# hi"}, {})
        assert result["error"] is True
        assert "context" in result["message"].lower() or "missing" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_returns_error_when_no_connector(self):
        with patch(
            "app.mcp.tools.docs_tools._get_sheets_connector",
            new=AsyncMock(return_value=None),
        ):
            result = await docs_create_execute({"title": "Notes", "content_markdown": "# hi"}, _CONTEXT)
        assert result["error"] is True
        lowered = result["message"].lower()
        assert "sheets" in lowered or "connector" in lowered

    @pytest.mark.asyncio
    async def test_rejects_empty_markdown(self):
        with (
            patch(
                "app.mcp.tools.docs_tools._get_sheets_connector",
                new=AsyncMock(return_value=_connector()),
            ),
            patch(
                "app.mcp.tools.docs_tools.decrypt_credentials",
                return_value={"service_account_json": {"type": "service_account"}},
            ),
        ):
            result = await docs_create_execute({"title": "Notes", "content_markdown": "   "}, _CONTEXT)
        assert result["error"] is True
        assert "content" in result["message"].lower() or "empty" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_happy_path_without_shared_drive_shares_with_user(self):
        with (
            patch(
                "app.mcp.tools.docs_tools._get_sheets_connector",
                new=AsyncMock(return_value=_connector()),
            ),
            patch(
                "app.mcp.tools.docs_tools.decrypt_credentials",
                return_value={"service_account_json": {"type": "service_account"}},
            ),
            patch(
                "app.mcp.tools.docs_tools.create_doc",
                new=AsyncMock(return_value={"doc_id": "FID", "url": "https://docs.google.com/document/d/FID"}),
            ) as mock_create,
            patch(
                "app.mcp.tools.docs_tools.share_doc",
                new=AsyncMock(return_value={"permission_id": "perm1"}),
            ) as mock_share,
            patch(
                "app.mcp.tools.docs_tools._get_user_email",
                new=AsyncMock(return_value="user@example.com"),
            ),
        ):
            result = await docs_create_execute(
                {"title": "Q1 Research", "content_markdown": "# Q1\n\nNotes"},
                _CONTEXT,
            )

        assert result["error"] is False
        assert result["doc_id"] == "FID"
        assert result["url"].startswith("https://docs.google.com/document/")
        assert result["title"] == "Q1 Research"
        assert result["shared_with"] == "user@example.com"

        # parent_id = None when no shared drive
        assert mock_create.call_args.kwargs["parent_id"] is None
        mock_share.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_happy_path_with_shared_drive_skips_user_share(self):
        with (
            patch(
                "app.mcp.tools.docs_tools._get_sheets_connector",
                new=AsyncMock(return_value=_connector(shared_drive_id="0AF_SHARED")),
            ),
            patch(
                "app.mcp.tools.docs_tools.decrypt_credentials",
                return_value={"service_account_json": {"type": "service_account"}},
            ),
            patch(
                "app.mcp.tools.docs_tools.create_doc",
                new=AsyncMock(return_value={"doc_id": "FID", "url": "https://x"}),
            ) as mock_create,
            patch(
                "app.mcp.tools.docs_tools.share_doc",
                new=AsyncMock(),
            ) as mock_share,
            patch(
                "app.mcp.tools.docs_tools._get_user_email",
                new=AsyncMock(return_value="user@example.com"),
            ),
        ):
            result = await docs_create_execute(
                {"title": "Q1 Research", "content_markdown": "# Q1"},
                _CONTEXT,
            )

        assert result["error"] is False
        assert result["shared_with"] is None
        assert mock_create.call_args.kwargs["parent_id"] == "0AF_SHARED"
        mock_share.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_folder_id_param_overrides_shared_drive(self):
        with (
            patch(
                "app.mcp.tools.docs_tools._get_sheets_connector",
                new=AsyncMock(return_value=_connector(shared_drive_id="0AF_SHARED")),
            ),
            patch(
                "app.mcp.tools.docs_tools.decrypt_credentials",
                return_value={"service_account_json": {"type": "service_account"}},
            ),
            patch(
                "app.mcp.tools.docs_tools.create_doc",
                new=AsyncMock(return_value={"doc_id": "FID", "url": "https://x"}),
            ) as mock_create,
            patch(
                "app.mcp.tools.docs_tools.share_doc",
                new=AsyncMock(),
            ),
            patch(
                "app.mcp.tools.docs_tools._get_user_email",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await docs_create_execute(
                {"title": "Q1", "content_markdown": "# Q1", "folder_id": "FOLDER_ABC"},
                _CONTEXT,
            )

        assert result["error"] is False
        assert mock_create.call_args.kwargs["parent_id"] == "FOLDER_ABC"

    @pytest.mark.asyncio
    async def test_create_failure_returns_error_envelope(self):
        with (
            patch(
                "app.mcp.tools.docs_tools._get_sheets_connector",
                new=AsyncMock(return_value=_connector()),
            ),
            patch(
                "app.mcp.tools.docs_tools.decrypt_credentials",
                return_value={"service_account_json": {"type": "service_account"}},
            ),
            patch(
                "app.mcp.tools.docs_tools.create_doc",
                new=AsyncMock(side_effect=Exception("boom")),
            ),
        ):
            result = await docs_create_execute({"title": "Q1", "content_markdown": "# Q1"}, _CONTEXT)
        assert result["error"] is True
        assert "boom" in result["message"] or "failed" in result["message"].lower()
