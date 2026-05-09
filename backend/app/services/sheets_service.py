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
    "https://www.googleapis.com/auth/drive.readonly",
]


def _build_sheets_service(credentials: dict):
    creds = service_account.Credentials.from_service_account_info(credentials, scopes=_SCOPES)
    return build("sheets", "v4", credentials=creds)


def _build_drive_service(credentials: dict):
    creds = service_account.Credentials.from_service_account_info(credentials, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds)


async def create_spreadsheet(
    *, credentials: dict | None, title: str, shared_drive_id: str | None = None
) -> dict[str, str]:
    if not credentials:
        raise ValueError("credentials required")

    def _sync_sheets_api():
        service = _build_sheets_service(credentials)
        result = (
            service.spreadsheets()
            .create(
                body={"properties": {"title": title}},
                fields="spreadsheetId,spreadsheetUrl",
            )
            .execute()
        )
        return {
            "spreadsheet_id": result["spreadsheetId"],
            "url": result["spreadsheetUrl"],
        }

    def _sync_drive_api():
        drive = _build_drive_service(credentials)
        result = (
            drive.files()
            .create(
                body={
                    "name": title,
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                    "parents": [shared_drive_id],
                },
                supportsAllDrives=True,
                fields="id,webViewLink",
            )
            .execute()
        )
        return {
            "spreadsheet_id": result["id"],
            "url": result["webViewLink"],
        }

    _sync = _sync_drive_api if shared_drive_id else _sync_sheets_api
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
        result = (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=range_str,
                valueInputOption="RAW",
                body={"values": data},
            )
            .execute()
        )
        return {
            "updated_range": result.get("updatedRange", ""),
            "updated_rows": result.get("updatedRows", 0),
            "updated_columns": result.get("updatedColumns", 0),
        }

    return await asyncio.to_thread(_sync)


async def read_range(
    *,
    credentials: dict | None,
    spreadsheet_id: str,
    range_str: str = "Sheet1",
) -> dict[str, Any]:
    """Read cell values from a Google Spreadsheet.

    Returns: {"range": "<actual A1 range returned>", "values": [[...], ...]}
    `values` is a list of rows. Empty cells on the trailing edge are omitted by
    Sheets API (per-row length may vary).
    """
    if not credentials:
        raise ValueError("credentials required")

    def _sync():
        service = _build_sheets_service(credentials)
        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=range_str,
            )
            .execute()
        )
        return {
            "range": result.get("range", range_str),
            "values": result.get("values", []),
        }

    return await asyncio.to_thread(_sync)


# Brand palette — matches excel_export_service.ExcelExportConfig (#1A73E8).
_BRAND_BLUE = {"red": 26 / 255, "green": 115 / 255, "blue": 232 / 255}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_LIGHT_GRAY = {"red": 248 / 255, "green": 249 / 255, "blue": 250 / 255}


def _build_pricing_styling_requests(
    *,
    sheet_id: int,
    headers: list[str],
    row_count: int,
    currency_columns: set[str],
) -> list[dict]:
    """Build Sheets API batchUpdate requests to style a pricing-export sheet.

    Layers (in render order):
    - Frozen header row (1 row)
    - Header cell format: bold + white text + brand-blue fill + center
    - Per-column number formats: currency only for headers in `currency_columns`
    - Banded data rows (alternating white / light-gray)
    - Auto-resize all columns

    `currency_columns` is matched case-insensitively against each header.
    Caller passes the actual currency codes from the pricing payload — no
    header-string heuristics, so QTY/UPC don't get false-positive currency
    formatting and IDR doesn't get false-negative text formatting.

    Pure function — returns the request list. The caller is responsible for
    issuing the batchUpdate call.
    """
    if not headers:
        return []

    currency_set_upper = {c.strip().upper() for c in currency_columns}

    col_count = len(headers)
    total_rows = row_count + 1  # header + data
    requests: list[dict] = []

    # 1. Freeze the header row.
    requests.append(
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        }
    )

    # 2. Header row formatting.
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": col_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _BRAND_BLUE,
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": _WHITE,
                        },
                    }
                },
                "fields": ("userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"),
            }
        }
    )

    # 3. Per-column data formats — currency only when the header is in the
    #    explicit currency_columns set (case-insensitive).
    for col_idx, header in enumerate(headers):
        if header.strip().upper() not in currency_set_upper:
            continue
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": total_rows,
                        "startColumnIndex": col_idx,
                        "endColumnIndex": col_idx + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "CURRENCY",
                                "pattern": "#,##0.00;(#,##0.00)",
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        )

    # 4. Banded rows over header + data.
    if row_count > 0:
        requests.append(
            {
                "addBanding": {
                    "bandedRange": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": total_rows,
                            "startColumnIndex": 0,
                            "endColumnIndex": col_count,
                        },
                        "rowProperties": {
                            "headerColor": _BRAND_BLUE,
                            "firstBandColor": _WHITE,
                            "secondBandColor": _LIGHT_GRAY,
                        },
                    }
                }
            }
        )

    # 5. Auto-resize columns to fit content.
    requests.append(
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": col_count,
                }
            }
        }
    )

    return requests


async def apply_pricing_styling(
    *,
    credentials: dict | None,
    spreadsheet_id: str,
    headers: list[str],
    row_count: int,
    currency_columns: set[str],
    sheet_id: int = 0,
) -> dict[str, Any]:
    """Issue a batchUpdate to apply pricing-export styling to a sheet.

    Best-effort — caller should swallow exceptions so a styling failure
    doesn't block the user from getting the spreadsheet URL.
    """
    if not credentials:
        raise ValueError("credentials required")

    requests = _build_pricing_styling_requests(
        sheet_id=sheet_id,
        headers=headers,
        row_count=row_count,
        currency_columns=currency_columns,
    )
    if not requests:
        return {"replies": []}

    def _sync():
        service = _build_sheets_service(credentials)
        return (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            )
            .execute()
        )

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
        result = (
            drive.permissions()
            .create(
                fileId=spreadsheet_id,
                body={"type": "user", "role": role, "emailAddress": email},
                sendNotificationEmail=False,
            )
            .execute()
        )
        return {"permission_id": result["id"]}

    return await asyncio.to_thread(_sync)


async def validate_connection(*, credentials: dict | None, shared_drive_id: str | None = None) -> dict[str, Any]:
    if not credentials:
        raise ValueError("credentials required")

    def _sync_sheets_api():
        sheets = _build_sheets_service(credentials)
        result = (
            sheets.spreadsheets()
            .create(
                body={"properties": {"title": "AI-den Connection Test"}},
                fields="spreadsheetId",
            )
            .execute()
        )
        test_id = result["spreadsheetId"]
        try:
            drive = _build_drive_service(credentials)
            drive.files().delete(fileId=test_id).execute()
        except Exception:
            logger.warning("sheets_service.validate_cleanup_failed", exc_info=True)
        return {"valid": True}

    def _sync_drive_api():
        drive = _build_drive_service(credentials)
        # Create test file as a spreadsheet in the Shared Drive.
        # files.create with parents=[drive_id] + supportsAllDrives=True will fail
        # cleanly if the drive doesn't exist or the SA lacks access — no need for
        # a separate drives.get preflight (which requires a broader OAuth scope).
        created = (
            drive.files()
            .create(
                body={
                    "name": "AI-den Connection Test",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                    "parents": [shared_drive_id],
                },
                supportsAllDrives=True,
                fields="id",
            )
            .execute()
        )
        # 3. Cleanup (best-effort)
        try:
            drive.files().delete(
                fileId=created["id"],
                supportsAllDrives=True,
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
