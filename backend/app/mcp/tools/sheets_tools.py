"""Google Sheets tool executors for the chat agent.

Same pattern as bigquery_tools.py: async functions taking (params, context),
looking up the active connector, decrypting credentials, calling the service.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials
from app.models.mcp_connector import McpConnector
from app.services.sheets_service import (
    create_spreadsheet,
    share_spreadsheet,
    write_range,
)

logger = logging.getLogger(__name__)


async def _get_sheets_connector(context: dict) -> McpConnector | None:
    db: AsyncSession = context["db"]
    tenant_id = uuid.UUID(context["tenant_id"]) if isinstance(context["tenant_id"], str) else context["tenant_id"]
    result = await db.execute(
        select(McpConnector).where(
            McpConnector.tenant_id == tenant_id,
            McpConnector.provider == "google_sheets",
            McpConnector.status == "active",
            McpConnector.is_enabled.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def _get_user_email(context: dict) -> str | None:
    db: AsyncSession = context["db"]
    actor_id = uuid.UUID(context["actor_id"]) if isinstance(context["actor_id"], str) else context["actor_id"]
    from app.models.user import User

    result = await db.execute(select(User.email).where(User.id == actor_id))
    return result.scalar_one_or_none()


async def sheets_create_execute(params: dict, context: dict, **kwargs: Any) -> dict:
    connector = await _get_sheets_connector(context)
    if not connector:
        return {"error": True, "message": "Google Sheets connector not configured. Set up in Settings."}

    credentials = decrypt_credentials(connector.encrypted_credentials)
    title = params.get("title", "AI-den Export")

    try:
        result = await create_spreadsheet(credentials=credentials, title=title)
    except Exception as e:
        logger.warning("sheets_tools.create_failed", exc_info=True)
        return {"error": True, "message": f"Failed to create spreadsheet: {e}"}

    user_email = await _get_user_email(context)
    if user_email:
        try:
            await share_spreadsheet(
                credentials=credentials,
                spreadsheet_id=result["spreadsheet_id"],
                email=user_email,
            )
        except Exception:
            logger.warning("sheets_tools.share_failed", exc_info=True)

    return {
        "error": False,
        "spreadsheet_id": result["spreadsheet_id"],
        "url": result["url"],
        "shared_with": user_email,
    }


async def sheets_write_range_execute(params: dict, context: dict, **kwargs: Any) -> dict:
    connector = await _get_sheets_connector(context)
    if not connector:
        return {"error": True, "message": "Google Sheets connector not configured."}

    spreadsheet_id = params.get("spreadsheet_id")
    data = params.get("data", [])
    range_str = params.get("range", "Sheet1!A1")

    if not spreadsheet_id:
        return {"error": True, "message": "spreadsheet_id is required."}
    if not data:
        return {"error": True, "message": "data must be a non-empty 2D array."}

    credentials = decrypt_credentials(connector.encrypted_credentials)

    try:
        result = await write_range(
            credentials=credentials,
            spreadsheet_id=spreadsheet_id,
            data=data,
            range_str=range_str,
        )
    except Exception as e:
        logger.warning("sheets_tools.write_failed", exc_info=True)
        return {"error": True, "message": f"Failed to write to spreadsheet: {e}"}

    return {
        "error": False,
        "updated_rows": result["updated_rows"],
        "updated_columns": result["updated_columns"],
        "updated_range": result["updated_range"],
    }
