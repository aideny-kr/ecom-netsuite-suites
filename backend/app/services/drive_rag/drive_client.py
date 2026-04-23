"""Thin Google Drive API wrapper. Async-wrapped sync calls via asyncio.to_thread."""

from __future__ import annotations

import asyncio
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.services.sheets_service import _SCOPES

SUPPORTED_MIMES = frozenset(
    {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.spreadsheet",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
        "text/markdown",
    }
)


def is_supported_mime(mime_type: str) -> bool:
    return mime_type in SUPPORTED_MIMES


def _build_drive(credentials: dict):
    creds = service_account.Credentials.from_service_account_info(credentials, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds)


async def get_folder_metadata(*, credentials: dict, folder_id: str) -> dict[str, Any]:
    def _sync():
        service = _build_drive(credentials)
        return (
            service.files()
            .get(fileId=folder_id, fields="id,name,mimeType", supportsAllDrives=True)
            .execute()
        )

    return await asyncio.to_thread(_sync)


async def list_folder_files(*, credentials: dict, folder_id: str) -> list[dict[str, Any]]:
    """List supported files inside folder_id. Paginates fully. Filters unsupported mimes."""

    def _sync():
        service = _build_drive(credentials)
        files: list[dict] = []
        page_token: str | None = None
        q = f"'{folder_id}' in parents and trashed=false"
        fields = "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink)"
        while True:
            kwargs = {
                "q": q,
                "fields": fields,
                "pageSize": 1000,
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
                "corpora": "allDrives",
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = service.files().list(**kwargs).execute()
            for f in resp.get("files", []):
                if is_supported_mime(f.get("mimeType", "")):
                    files.append(f)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return files

    return await asyncio.to_thread(_sync)


async def get_file_metadata(*, credentials: dict, file_id: str) -> dict[str, Any]:
    def _sync():
        service = _build_drive(credentials)
        return (
            service.files()
            .get(
                fileId=file_id,
                fields="id,name,mimeType,modifiedTime,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )

    return await asyncio.to_thread(_sync)


async def download_file_bytes(*, credentials: dict, file_id: str) -> bytes:
    """Download binary file bytes (for PDF, docx, txt, md)."""

    def _sync():
        service = _build_drive(credentials)
        return (
            service.files()
            .get_media(fileId=file_id, supportsAllDrives=True)
            .execute()
        )

    return await asyncio.to_thread(_sync)


async def export_google_doc_text(*, credentials: dict, file_id: str) -> str:
    """Export a native Google Doc as plain text."""

    def _sync():
        service = _build_drive(credentials)
        data = service.files().export(fileId=file_id, mimeType="text/plain").execute()
        return data.decode("utf-8") if isinstance(data, bytes) else str(data)

    return await asyncio.to_thread(_sync)
