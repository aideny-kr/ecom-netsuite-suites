"""Celery tasks for Drive RAG folder sync (Phase 9.1).

- `drive_rag_sync_folder(folder_id)` — sync ONE folder via the indexer,
  reusing the tenant's Google Sheets connector credentials (service account).
- `drive_rag_sync_all()` — iterate all enabled DriveFolder rows across tenants
  and enqueue per-folder sync tasks. Scheduled daily at 06:00 UTC by Beat.

Both tasks run on the "sync" queue. Per-folder sync happens in its own sub-task
so one folder's failure doesn't block others.

Uses `asyncio.run()` + `async_session_factory` — same pattern as
`netsuite_deposit_sync.py` (the canonical async-in-celery precedent in this repo).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.core.encryption import decrypt_credentials
from app.models.drive import DriveFolder
from app.models.mcp_connector import McpConnector
from app.services.drive_rag.indexer import sync_folder
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _get_sheets_connector(
    db: AsyncSession, tenant_id: uuid.UUID
) -> McpConnector | None:
    """Look up the tenant's active Google Sheets connector (service account)."""
    return (
        (
            await db.execute(
                select(McpConnector).where(
                    McpConnector.tenant_id == tenant_id,
                    McpConnector.provider == "google_sheets",
                    McpConnector.status == "active",
                    McpConnector.is_enabled.is_(True),
                )
            )
        )
        .scalars()
        .first()
    )


async def _sync_one_async(folder_id: str) -> dict:
    """Async body of `drive_rag_sync_folder`. Loads the folder + connector,
    decrypts the service-account credentials envelope, and delegates to
    the indexer."""
    fid = uuid.UUID(folder_id)
    async with async_session_factory() as db:
        folder = (
            await db.execute(select(DriveFolder).where(DriveFolder.id == fid))
        ).scalars().first()
        if not folder:
            logger.warning(
                "drive_rag.sync_folder.not_found", extra={"folder_id": folder_id}
            )
            return {"skipped": "folder_not_found", "folder_id": folder_id}

        connector = await _get_sheets_connector(db, folder.tenant_id)
        if not connector:
            logger.warning(
                "drive_rag.sync_folder.no_connector",
                extra={"folder_id": folder_id, "tenant_id": str(folder.tenant_id)},
            )
            return {"skipped": "no_sheets_connector", "folder_id": folder_id}

        envelope = decrypt_credentials(connector.encrypted_credentials)
        credentials = envelope.get("service_account_json", envelope)
        return await sync_folder(db, folder_id=fid, credentials=credentials)


@celery_app.task(name="tasks.drive_rag_sync_folder", queue="sync")
def drive_rag_sync_folder(folder_id: str) -> dict:
    """Sync ONE Drive folder (manual button or per-folder dispatch)."""
    return asyncio.run(_sync_one_async(folder_id))


async def _sync_all_async() -> dict:
    """Async body of `drive_rag_sync_all`. Queries all enabled DriveFolder
    rows across all tenants and enqueues a per-folder sync task for each."""
    async with async_session_factory() as db:
        folders = (
            (
                await db.execute(
                    select(DriveFolder).where(DriveFolder.is_enabled.is_(True))
                )
            )
            .scalars()
            .all()
        )

    enqueued = 0
    for f in folders:
        try:
            drive_rag_sync_folder.delay(str(f.id))
            enqueued += 1
        except Exception:
            logger.exception(
                "drive_rag.sync_all.enqueue_failed",
                extra={"folder_id": str(f.id)},
            )
    logger.info("drive_rag.sync_all.completed", extra={"enqueued": enqueued})
    return {"enqueued": enqueued}


@celery_app.task(name="tasks.drive_rag_sync_all", queue="sync")
def drive_rag_sync_all() -> dict:
    """Beat-scheduled entry point — enqueue per-folder sync tasks across tenants."""
    return asyncio.run(_sync_all_async())
