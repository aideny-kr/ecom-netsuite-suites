"""Google Docs tool executor for the chat agent.

Reuses the Google Sheets SA connector — `drive.file` + `drive.readonly` scopes
already cover `files.create` with markdown→Doc conversion, so no new OAuth
scopes or onboarding are required.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials
from app.models.mcp_connector import McpConnector
from app.services.docs_service import create_doc, share_doc

logger = logging.getLogger(__name__)


async def _get_sheets_connector(context: dict) -> McpConnector | None:
    """Docs creation reuses the google_sheets connector — same SA, same scopes."""
    db: AsyncSession | None = context.get("db")
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


async def _get_user_email(context: dict) -> str | None:
    db: AsyncSession | None = context.get("db")
    actor_id = context.get("actor_id")
    if not db or not actor_id:
        return None
    try:
        actor_uuid = uuid.UUID(actor_id) if isinstance(actor_id, str) else actor_id
    except (ValueError, TypeError):
        return None
    from app.models.user import User

    result = await db.execute(select(User.email).where(User.id == actor_uuid))
    return result.scalar_one_or_none()


async def docs_create_execute(params: dict, context: dict, **kwargs: Any) -> dict:
    if not context or not context.get("tenant_id") or not context.get("db"):
        return {
            "error": True,
            "message": "Missing context — tenant_id and db are required.",
        }

    connector = await _get_sheets_connector(context)
    if not connector:
        return {
            "error": True,
            "message": "Google Sheets connector not configured. Set up in Settings.",
        }

    title = (params.get("title") or "Research Notes").strip() or "Research Notes"
    body_md = params.get("content_markdown") or ""
    if not body_md.strip():
        return {
            "error": True,
            "message": "content_markdown is required and must be non-empty.",
        }

    credentials_envelope = decrypt_credentials(connector.encrypted_credentials)
    credentials = credentials_envelope.get("service_account_json", credentials_envelope)

    folder_id_param = params.get("folder_id")
    shared_drive_id = (connector.metadata_json or {}).get("shared_drive_id")
    parent_id = folder_id_param or shared_drive_id

    try:
        result = await create_doc(
            credentials=credentials,
            title=title,
            body_markdown=body_md,
            parent_id=parent_id,
        )
    except Exception as e:
        logger.warning("docs_tools.create_failed", exc_info=True)
        return {"error": True, "message": f"Failed to create doc: {e}"}

    # Skip share when Doc lives in a Shared Drive or an explicit folder — the
    # requesting user already has access through drive membership.
    shared_with: str | None = None
    if not parent_id:
        user_email = await _get_user_email(context)
        if user_email:
            try:
                await share_doc(
                    credentials=credentials,
                    doc_id=result["doc_id"],
                    email=user_email,
                )
                shared_with = user_email
            except Exception:
                logger.warning("docs_tools.share_failed", exc_info=True)

    return {
        "error": False,
        "doc_id": result["doc_id"],
        "url": result["url"],
        "title": title,
        "shared_with": shared_with,
    }
