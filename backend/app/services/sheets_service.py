"""Google Sheets API wrapper using service account auth.

Synchronous google-api-python-client calls wrapped with asyncio.to_thread()
to avoid blocking the event loop. Same pattern as bigquery_service.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


def _build_sheets_service(credentials: dict):
    creds = service_account.Credentials.from_service_account_info(credentials, scopes=_SCOPES)
    return build("sheets", "v4", credentials=creds)


def _build_drive_service(credentials: dict):
    creds = service_account.Credentials.from_service_account_info(credentials, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds)


async def create_spreadsheet(*, credentials: dict | None, title: str) -> dict[str, str]:
    if not credentials:
        raise ValueError("credentials required")

    def _sync():
        service = _build_sheets_service(credentials)
        result = service.spreadsheets().create(
            body={"properties": {"title": title}},
            fields="spreadsheetId,spreadsheetUrl",
        ).execute()
        return {
            "spreadsheet_id": result["spreadsheetId"],
            "url": result["spreadsheetUrl"],
        }

    return await asyncio.to_thread(_sync)


async def write_range(
    *,
    credentials: dict | None,
    spreadsheet_id: str,
    data: list[list[Any]],
    range_str: str = "Sheet1!A1",
) -> dict[str, Any]:
    if not credentials:
        raise ValueError("credentials required")
    if not data:
        raise ValueError("data must be non-empty")

    def _sync():
        service = _build_sheets_service(credentials)
        result = service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_str,
            valueInputOption="RAW",
            body={"values": data},
        ).execute()
        return {
            "updated_range": result.get("updatedRange", ""),
            "updated_rows": result.get("updatedRows", 0),
            "updated_columns": result.get("updatedColumns", 0),
        }

    return await asyncio.to_thread(_sync)


async def share_spreadsheet(
    *,
    credentials: dict | None,
    spreadsheet_id: str,
    email: str,
    role: str = "writer",
) -> dict[str, str]:
    if not credentials:
        raise ValueError("credentials required")

    def _sync():
        drive = _build_drive_service(credentials)
        result = drive.permissions().create(
            fileId=spreadsheet_id,
            body={"type": "user", "role": role, "emailAddress": email},
            sendNotificationEmail=False,
        ).execute()
        return {"permission_id": result["id"]}

    return await asyncio.to_thread(_sync)


async def validate_connection(
    *, credentials: dict | None, shared_drive_id: str | None = None
) -> dict[str, Any]:
    if not credentials:
        raise ValueError("credentials required")

    def _sync_sheets_api():
        sheets = _build_sheets_service(credentials)
        result = sheets.spreadsheets().create(
            body={"properties": {"title": "AI-den Connection Test"}},
            fields="spreadsheetId",
        ).execute()
        test_id = result["spreadsheetId"]
        try:
            drive = _build_drive_service(credentials)
            drive.files().delete(fileId=test_id).execute()
        except Exception:
            logger.warning("sheets_service.validate_cleanup_failed", exc_info=True)
        return {"valid": True}

    def _sync_drive_api():
        drive = _build_drive_service(credentials)
        # 1. Pre-flight: confirm it's a real Shared Drive
        drive.drives().get(driveId=shared_drive_id).execute()
        # 2. Create test file as a spreadsheet in that drive
        created = drive.files().create(
            body={
                "name": "AI-den Connection Test",
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "parents": [shared_drive_id],
            },
            supportsAllDrives=True,
            fields="id",
        ).execute()
        # 3. Cleanup (best-effort)
        try:
            drive.files().delete(
                fileId=created["id"], supportsAllDrives=True,
            ).execute()
        except Exception:
            logger.warning("sheets_service.validate_cleanup_failed", exc_info=True)
        return {"valid": True}

    _sync = _sync_drive_api if shared_drive_id else _sync_sheets_api
    try:
        return await asyncio.wait_for(asyncio.to_thread(_sync), timeout=15.0)
    except asyncio.TimeoutError:
        logger.warning("sheets_service.validate_connection_timeout")
        return {"valid": False, "error": "timeout"}
    except Exception as e:
        logger.warning("sheets_service.validate_connection_failed", exc_info=True)
        return {"valid": False, "error": str(e)}
