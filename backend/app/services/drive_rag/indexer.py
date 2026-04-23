"""Google Drive folder sync orchestrator.

Given a folder registration, list current Drive contents, diff against
indexed files by modified_time, extract/chunk/embed new or changed files,
delete rows for files removed from Drive. Extract failures are recorded
on drive_files.last_extract_error and do not abort the folder.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.drive import DriveChunk, DriveFile, DriveFolder
from app.services.chat.embeddings import embed_texts
from app.services.drive_rag import chunker, drive_client, extractors

logger = logging.getLogger(__name__)


def _parse_rfc3339(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


async def _mark_syncing(db: AsyncSession, folder: DriveFolder) -> None:
    folder.sync_status = "syncing"
    folder.last_sync_error = None
    await db.commit()


async def _mark_finished(
    db: AsyncSession,
    folder: DriveFolder,
    *,
    success: bool,
    error: str | None = None,
) -> None:
    folder.sync_status = "success" if success else "error"
    folder.last_sync_error = error
    folder.last_synced_at = datetime.now(timezone.utc)
    await db.commit()


async def sync_folder(
    db: AsyncSession,
    *,
    folder_id: uuid.UUID,
    credentials: dict,
) -> dict:
    """Sync one folder. Returns counts."""
    folder = (await db.execute(select(DriveFolder).where(DriveFolder.id == folder_id))).scalar_one()
    await _mark_syncing(db, folder)

    files_indexed = 0
    files_deleted = 0
    files_failed = 0
    try:
        meta = await drive_client.get_folder_metadata(credentials=credentials, folder_id=folder.folder_id)
        if meta.get("name") and meta["name"] != folder.folder_name:
            folder.folder_name = meta["name"]

        drive_files = await drive_client.list_folder_files(credentials=credentials, folder_id=folder.folder_id)

        existing_rows = (await db.execute(select(DriveFile).where(DriveFile.folder_id == folder.id))).scalars().all()
        existing_by_drive_id = {r.drive_file_id: r for r in existing_rows}
        drive_ids_now = {f["id"] for f in drive_files}

        for row in existing_rows:
            if row.drive_file_id not in drive_ids_now:
                await db.delete(row)  # cascades to drive_chunks
                files_deleted += 1

        for idx, f in enumerate(drive_files):
            drive_modified = _parse_rfc3339(f["modifiedTime"])
            existing = existing_by_drive_id.get(f["id"])
            if existing and existing.modified_time >= drive_modified:
                continue

            try:
                text = await extractors.extract_by_mime(
                    credentials=credentials,
                    file_id=f["id"],
                    mime_type=f["mimeType"],
                )
            except Exception as e:
                files_failed += 1
                if existing:
                    existing.last_extract_error = str(e)[:500]
                else:
                    db.add(
                        DriveFile(
                            tenant_id=folder.tenant_id,
                            folder_id=folder.id,
                            drive_file_id=f["id"],
                            name=f["name"],
                            mime_type=f["mimeType"],
                            web_view_link=f["webViewLink"],
                            modified_time=drive_modified,
                            indexed_at=datetime.now(timezone.utc),
                            chunk_count=0,
                            last_extract_error=str(e)[:500],
                        )
                    )
                continue

            pieces = chunker.chunk_text(text)
            if not pieces:
                continue
            embeddings = await embed_texts([p["content"] for p in pieces])

            if existing:
                await db.execute(delete(DriveChunk).where(DriveChunk.file_id == existing.id))
                existing.name = f["name"]
                existing.mime_type = f["mimeType"]
                existing.web_view_link = f["webViewLink"]
                existing.modified_time = drive_modified
                existing.indexed_at = datetime.now(timezone.utc)
                existing.chunk_count = len(pieces)
                existing.last_extract_error = None
                file_row = existing
            else:
                file_row = DriveFile(
                    tenant_id=folder.tenant_id,
                    folder_id=folder.id,
                    drive_file_id=f["id"],
                    name=f["name"],
                    mime_type=f["mimeType"],
                    web_view_link=f["webViewLink"],
                    modified_time=drive_modified,
                    indexed_at=datetime.now(timezone.utc),
                    chunk_count=len(pieces),
                )
                db.add(file_row)
                await db.flush()

            for i, piece in enumerate(pieces):
                db.add(
                    DriveChunk(
                        tenant_id=folder.tenant_id,
                        file_id=file_row.id,
                        chunk_index=piece["chunk_index"],
                        content=piece["content"],
                        token_count=piece["token_count"],
                        embedding=embeddings[i] if embeddings else None,
                        metadata_={
                            "source_name": f["name"],
                            "web_view_link": f["webViewLink"],
                            "mime_type": f["mimeType"],
                        },
                    )
                )

            files_indexed += 1
            if (idx + 1) % 10 == 0:
                await db.commit()

        await _mark_finished(db, folder, success=True)
    except Exception as e:
        logger.exception("drive_rag.sync_folder_failed", extra={"folder_id": str(folder_id)})
        await _mark_finished(db, folder, success=False, error=str(e)[:500])
        raise

    return {
        "files_indexed": files_indexed,
        "files_deleted": files_deleted,
        "files_failed": files_failed,
    }
