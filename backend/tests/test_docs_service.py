"""Tests for docs_service.create_doc — Drive multipart markdown → Doc upload."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.docs_service import create_doc


class TestCreateDoc:
    @pytest.mark.asyncio
    async def test_create_doc_calls_drive_files_create_with_correct_mime(self):
        mock_drive = MagicMock()
        mock_drive.files().create().execute.return_value = {
            "id": "FID",
            "webViewLink": "https://docs.google.com/document/d/FID",
        }
        with patch("app.services.docs_service._build_drive_service", return_value=mock_drive):
            await create_doc(
                credentials={"type": "service_account"},
                title="Research",
                body_markdown="# Title\n\nBody",
                parent_id="SD_ABC",
            )

        # Pull out the last create() call (the one we execute())
        create_call = mock_drive.files().create.call_args
        kwargs = create_call.kwargs
        assert kwargs["body"]["name"] == "Research"
        assert kwargs["body"]["mimeType"] == "application/vnd.google-apps.document"
        assert kwargs["body"]["parents"] == ["SD_ABC"]
        assert kwargs["supportsAllDrives"] is True
        # MediaInMemoryUpload carries the markdown mimetype
        media = kwargs["media_body"]
        assert media.mimetype() == "text/markdown"

    @pytest.mark.asyncio
    async def test_create_doc_returns_id_and_url(self):
        mock_drive = MagicMock()
        mock_drive.files().create().execute.return_value = {
            "id": "FID",
            "webViewLink": "https://docs.google.com/document/d/FID",
        }
        with patch("app.services.docs_service._build_drive_service", return_value=mock_drive):
            result = await create_doc(
                credentials={"type": "service_account"},
                title="Research",
                body_markdown="# Title",
                parent_id="SD_ABC",
            )

        assert result == {
            "doc_id": "FID",
            "url": "https://docs.google.com/document/d/FID",
        }

    @pytest.mark.asyncio
    async def test_create_doc_without_parent_omits_parents_key(self):
        mock_drive = MagicMock()
        mock_drive.files().create().execute.return_value = {
            "id": "FID",
            "webViewLink": "https://x",
        }
        with patch("app.services.docs_service._build_drive_service", return_value=mock_drive):
            await create_doc(
                credentials={"type": "service_account"},
                title="Research",
                body_markdown="# Title",
                parent_id=None,
            )

        kwargs = mock_drive.files().create.call_args.kwargs
        assert "parents" not in kwargs["body"]

    @pytest.mark.asyncio
    async def test_create_doc_raises_value_error_on_empty_credentials(self):
        with pytest.raises(ValueError, match="credentials"):
            await create_doc(
                credentials=None,
                title="Research",
                body_markdown="# Title",
                parent_id=None,
            )

    @pytest.mark.asyncio
    async def test_create_doc_rejects_empty_markdown(self):
        """Empty body produces a blank Doc — reject at the service boundary."""
        with pytest.raises(ValueError, match="body_markdown"):
            await create_doc(
                credentials={"type": "service_account"},
                title="Research",
                body_markdown="",
                parent_id=None,
            )
