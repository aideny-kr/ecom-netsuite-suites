from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from app.services.drive_rag import drive_client

CREDS = {"type": "service_account", "client_email": "x@y.iam.gserviceaccount.com"}


@pytest.mark.asyncio
async def test_get_folder_metadata_returns_name():
    mock_service = MagicMock()
    mock_service.files().get().execute.return_value = {
        "id": "FID",
        "name": "My Docs",
        "mimeType": "application/vnd.google-apps.folder",
    }
    with patch("app.services.drive_rag.drive_client._build_drive", return_value=mock_service):
        result = await drive_client.get_folder_metadata(credentials=CREDS, folder_id="FID")
    assert result["name"] == "My Docs"


@pytest.mark.asyncio
async def test_get_folder_metadata_wraps_disabled_drive_api_error():
    mock_service = MagicMock()
    content = (
        b'{"error":{"message":"Google Drive API has not been used in project 704055641880 before or it is disabled. '
        b"Enable it by visiting https://console.developers.google.com/apis/api/drive.googleapis.com/overview?"
        b'project=704055641880 then retry."}}'
    )
    mock_service.files().get().execute.side_effect = HttpError(MagicMock(status=403), content)

    with patch("app.services.drive_rag.drive_client._build_drive", return_value=mock_service):
        with pytest.raises(drive_client.DriveApiError) as exc:
            await drive_client.get_folder_metadata(credentials=CREDS, folder_id="FID")

    message = str(exc.value)
    assert "Google Drive API is disabled" in message
    assert "704055641880" in message
    assert "drive.googleapis.com" in message


@pytest.mark.asyncio
async def test_list_folder_files_paginates_and_filters_unsupported():
    mock_service = MagicMock()
    mock_service.files().list().execute.side_effect = [
        {
            "files": [
                {
                    "id": "1",
                    "name": "doc",
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "2026-04-22T00:00:00Z",
                    "webViewLink": "https://x",
                },
                {
                    "id": "2",
                    "name": "unsup",
                    "mimeType": "image/png",
                    "modifiedTime": "2026-04-22T00:00:00Z",
                    "webViewLink": "https://y",
                },
            ],
            "nextPageToken": "p2",
        },
        {
            "files": [
                {
                    "id": "3",
                    "name": "pdf",
                    "mimeType": "application/pdf",
                    "modifiedTime": "2026-04-22T00:00:00Z",
                    "webViewLink": "https://z",
                },
            ],
        },
    ]
    with patch("app.services.drive_rag.drive_client._build_drive", return_value=mock_service):
        files = await drive_client.list_folder_files(credentials=CREDS, folder_id="FID")
    assert [f["id"] for f in files] == ["1", "3"]


def test_is_supported_mime_types():
    assert drive_client.is_supported_mime("application/vnd.google-apps.document")
    assert drive_client.is_supported_mime("application/pdf")
    assert drive_client.is_supported_mime("application/vnd.google-apps.spreadsheet")
    assert drive_client.is_supported_mime("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert drive_client.is_supported_mime("text/plain")
    assert drive_client.is_supported_mime("text/markdown")
    assert not drive_client.is_supported_mime("image/png")
    assert not drive_client.is_supported_mime("application/vnd.google-apps.folder")
