"""Drive tool executors (use case C — ad-hoc URL read)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials
from app.models.mcp_connector import McpConnector
from app.services.drive_rag import drive_client, extractors
from app.services.drive_rag.url_parser import parse_file_id

logger = logging.getLogger(__name__)

_MAX_RETURN_CHARS = 50_000


async def _get_sheets_connector(context: dict) -> McpConnector | None:
    db: AsyncSession = context.get("db")
    tenant_id = context.get("tenant_id")
    if not db or not tenant_id:
        return None
    if isinstance(tenant_id, str):
        tenant_id = uuid.UUID(tenant_id)
    result = await db.execute(
        select(McpConnector).where(
            McpConnector.tenant_id == tenant_id,
            McpConnector.provider == "google_sheets",
            McpConnector.status == "active",
            McpConnector.is_enabled.is_(True),
        )
    )
    return result.scalars().first()


async def drive_read_doc_execute(params: dict, context: dict, **_: Any) -> dict:
    if not context or not context.get("tenant_id") or not context.get("db"):
        return {"error": True, "message": "Missing context — tenant_id and db are required."}
    connector = await _get_sheets_connector(context)
    if not connector:
        return {
            "error": True,
            "message": "Google Drive requires a Google Sheets connector. Configure one in Settings.",
        }

    raw = params.get("file_id_or_url") or ""
    try:
        file_id = parse_file_id(raw)
    except ValueError as e:
        return {"error": True, "message": str(e)}

    envelope = decrypt_credentials(connector.encrypted_credentials)
    credentials = envelope.get("service_account_json", envelope)

    try:
        meta = await drive_client.get_file_metadata(credentials=credentials, file_id=file_id)
    except Exception as e:
        logger.warning("drive_tools.get_metadata_failed", exc_info=True)
        return {"error": True, "message": f"Could not access Drive file: {e}"}

    try:
        text = await extractors.extract_by_mime(
            credentials=credentials,
            file_id=file_id,
            mime_type=meta.get("mimeType", ""),
        )
    except ValueError as e:
        return {"error": True, "message": str(e)}
    except Exception as e:
        logger.warning("drive_tools.extract_failed", exc_info=True)
        return {"error": True, "message": f"Failed to read file: {e}"}

    truncated = len(text) > _MAX_RETURN_CHARS
    return {
        "error": False,
        "text": text[:_MAX_RETURN_CHARS],
        "truncated": truncated,
        "source_name": meta.get("name", file_id),
        "web_view_link": meta.get("webViewLink", ""),
        "mime_type": meta.get("mimeType", ""),
    }
