"""Thin Google Drive API wrapper. Async-wrapped sync calls via asyncio.to_thread."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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


class DriveApiError(RuntimeError):
    """Actionable wrapper for Google Drive API errors."""


def _google_error_message(exc: HttpError) -> str:
    raw = exc.content.decode("utf-8", errors="replace") if isinstance(exc.content, bytes) else str(exc.content)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}

    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or raw
    elif isinstance(error, str):
        message = error
    else:
        message = raw or str(exc)

    project_match = re.search(r"project\s+(\d+)", message, flags=re.IGNORECASE)
    normalized = message.lower()
    drive_api_disabled = "drive api has not been used" in normalized or (
        "drive api" in normalized and "disabled" in normalized
    )
    if drive_api_disabled:
        project = project_match.group(1) if project_match else "the service account project"
        return (
            f"Google Drive API is disabled for Google Cloud project {project}. "
            "Enable drive.googleapis.com for that project, wait a few minutes, then retry."
        )

    status = getattr(exc.resp, "status", None)
    if status == 403:
        return f"Google Drive permission or API access denied: {message}"
    if status == 404:
        return f"Google Drive folder or file was not found, or the service account lacks access: {message}"
    return f"Google Drive API error: {message}"


async def _to_thread_drive_call(fn):
    try:
        return await asyncio.to_thread(fn)
    except HttpError as exc:
        raise DriveApiError(_google_error_message(exc)) from exc


async def get_folder_metadata(*, credentials: dict, folder_id: str) -> dict[str, Any]:
    def _sync():
        service = _build_drive(credentials)
        return service.files().get(fileId=folder_id, fields="id,name,mimeType", supportsAllDrives=True).execute()

    return await _to_thread_drive_call(_sync)


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

    return await _to_thread_drive_call(_sync)


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

    return await _to_thread_drive_call(_sync)


async def download_file_bytes(*, credentials: dict, file_id: str) -> bytes:
    """Download binary file bytes (for PDF, docx, txt, md)."""

    def _sync():
        service = _build_drive(credentials)
        return service.files().get_media(fileId=file_id, supportsAllDrives=True).execute()

    return await _to_thread_drive_call(_sync)


async def export_google_doc_text(*, credentials: dict, file_id: str) -> str:
    """Export a native Google Doc as plain text."""

    def _sync():
        service = _build_drive(credentials)
        data = service.files().export(fileId=file_id, mimeType="text/plain").execute()
        return data.decode("utf-8") if isinstance(data, bytes) else str(data)

    return await _to_thread_drive_call(_sync)
