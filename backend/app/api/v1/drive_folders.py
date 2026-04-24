"""CRUD + sync + status endpoints for tenant-registered Drive folders."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_feature, require_permission
from app.core.encryption import decrypt_credentials
from app.models.drive import DriveChunk, DriveFile, DriveFolder
from app.models.mcp_connector import McpConnector
from app.models.user import User
from app.schemas.drive import (
    DriveFileListItem,
    DriveFolderCreate,
    DriveFolderResponse,
    DriveFolderStatus,
    DriveFolderUpdate,
)
from app.services import audit_service
from app.services.drive_rag import drive_client
from app.services.drive_rag.url_parser import parse_folder_id
from app.workers.tasks.drive_rag_sync import drive_rag_sync_folder

router = APIRouter(prefix="/drive-folders", tags=["drive-folders"])


def _folder_to_response(folder: DriveFolder, *, chunk_count: int, file_count: int) -> DriveFolderResponse:
    return DriveFolderResponse(
        id=str(folder.id),
        tenant_id=str(folder.tenant_id),
        folder_id=folder.folder_id,
        folder_name=folder.folder_name,
        is_enabled=folder.is_enabled,
        sync_status=folder.sync_status,
        last_synced_at=folder.last_synced_at,
        last_sync_error=folder.last_sync_error,
        chunk_count=chunk_count,
        file_count=file_count,
        created_at=folder.created_at,
    )


def _file_to_response(file: DriveFile, folder_name: str) -> DriveFileListItem:
    """Coerce ORM row + folder join result into the picker schema.

    `id` (internal UUID) and `drive_file_id` (Google's id) are kept separate so
    the frontend can use `id` as a React Query key and `drive_file_id` when it
    needs to call `drive_read_doc` or store a reference.
    """
    return DriveFileListItem(
        id=str(file.id),
        drive_file_id=file.drive_file_id,
        name=file.name,
        mime_type=file.mime_type,
        web_view_link=file.web_view_link,
        folder_name=folder_name,
        chunk_count=file.chunk_count,
    )


async def _sheets_connector(db: AsyncSession, tenant_id: uuid.UUID) -> McpConnector | None:
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


async def _counts_for(db: AsyncSession, folder_id: uuid.UUID) -> tuple[int, int]:
    chunk_count = (
        await db.execute(select(func.count(DriveChunk.id)).join(DriveFile).where(DriveFile.folder_id == folder_id))
    ).scalar() or 0
    file_count = (
        await db.execute(select(func.count(DriveFile.id)).where(DriveFile.folder_id == folder_id))
    ).scalar() or 0
    return chunk_count, file_count


@router.get("", response_model=list[DriveFolderResponse])
async def list_drive_folders(
    user: Annotated[User, Depends(require_feature("drive_rag"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    folders = (
        (
            await db.execute(
                select(DriveFolder).where(DriveFolder.tenant_id == user.tenant_id).order_by(DriveFolder.created_at)
            )
        )
        .scalars()
        .all()
    )
    out = []
    for f in folders:
        chunks, files = await _counts_for(db, f.id)
        out.append(_folder_to_response(f, chunk_count=chunks, file_count=files))
    return out


@router.get("/files", response_model=list[DriveFileListItem])
async def list_drive_files(
    user: Annotated[User, Depends(require_feature("drive_rag"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
    limit: int = 20,
):
    """Typeahead list of indexed Drive files for the `#` mention picker.

    Scoped to enabled folders for the tenant. Optional `q` filters by
    case-insensitive substring match against the file name. Capped at 100.
    """
    limit = max(1, min(limit, 100))
    stmt = (
        select(DriveFile, DriveFolder.folder_name)
        .join(DriveFolder, DriveFolder.id == DriveFile.folder_id)
        .where(
            DriveFile.tenant_id == user.tenant_id,
            DriveFolder.is_enabled.is_(True),
        )
        .order_by(DriveFile.name.asc())
        .limit(limit)
    )
    if q:
        stmt = stmt.where(DriveFile.name.ilike(f"%{q}%"))

    rows = (await db.execute(stmt)).all()
    return [_file_to_response(f, folder_name=folder_name) for f, folder_name in rows]


@router.post("", response_model=DriveFolderResponse, status_code=status.HTTP_201_CREATED)
async def create_drive_folder(
    request: DriveFolderCreate,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    connector = await _sheets_connector(db, user.tenant_id)
    if not connector:
        raise HTTPException(
            status_code=400,
            detail="Google Sheets connector is required before registering Drive folders.",
        )
    try:
        folder_id = parse_folder_id(request.folder_id_or_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    existing = (
        (
            await db.execute(
                select(DriveFolder).where(
                    DriveFolder.tenant_id == user.tenant_id,
                    DriveFolder.folder_id == folder_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="Folder already registered")

    envelope = decrypt_credentials(connector.encrypted_credentials)
    credentials = envelope.get("service_account_json", envelope)
    try:
        meta = await drive_client.get_folder_metadata(credentials=credentials, folder_id=folder_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot access folder: {e}")

    folder = DriveFolder(
        tenant_id=user.tenant_id,
        folder_id=folder_id,
        folder_name=meta.get("name", folder_id),
        created_by=user.id,
    )
    db.add(folder)
    await db.flush()
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="drive_folder",
        action="drive_folder.create",
        actor_id=user.id,
        resource_type="drive_folder",
        resource_id=str(folder.id),
    )
    await db.commit()
    await db.refresh(folder)

    drive_rag_sync_folder.delay(str(folder.id), tenant_id=str(user.tenant_id))
    return _folder_to_response(folder, chunk_count=0, file_count=0)


@router.patch("/{folder_id}", response_model=DriveFolderResponse)
async def patch_drive_folder(
    folder_id: uuid.UUID,
    request: DriveFolderUpdate,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    folder = (
        (
            await db.execute(
                select(DriveFolder).where(
                    DriveFolder.id == folder_id,
                    DriveFolder.tenant_id == user.tenant_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    folder.is_enabled = request.is_enabled
    await db.commit()
    await db.refresh(folder)
    chunks, files = await _counts_for(db, folder.id)
    return _folder_to_response(folder, chunk_count=chunks, file_count=files)


@router.delete("/{folder_id}", status_code=204)
async def delete_drive_folder(
    folder_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    folder = (
        (
            await db.execute(
                select(DriveFolder).where(
                    DriveFolder.id == folder_id,
                    DriveFolder.tenant_id == user.tenant_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    await db.delete(folder)
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="drive_folder",
        action="drive_folder.delete",
        actor_id=user.id,
        resource_type="drive_folder",
        resource_id=str(folder_id),
    )
    await db.commit()


@router.post("/{folder_id}/sync", status_code=202)
async def sync_drive_folder(
    folder_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    folder = (
        (
            await db.execute(
                select(DriveFolder).where(
                    DriveFolder.id == folder_id,
                    DriveFolder.tenant_id == user.tenant_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    drive_rag_sync_folder.delay(str(folder.id), tenant_id=str(user.tenant_id))
    return {"accepted": True, "folder_id": str(folder.id)}


@router.get("/{folder_id}/status", response_model=DriveFolderStatus)
async def status_drive_folder(
    folder_id: uuid.UUID,
    user: Annotated[User, Depends(require_feature("drive_rag"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    folder = (
        (
            await db.execute(
                select(DriveFolder).where(
                    DriveFolder.id == folder_id,
                    DriveFolder.tenant_id == user.tenant_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    chunks, files = await _counts_for(db, folder.id)
    return DriveFolderStatus(
        id=str(folder.id),
        sync_status=folder.sync_status,
        last_synced_at=folder.last_synced_at,
        last_sync_error=folder.last_sync_error,
        chunk_count=chunks,
        file_count=files,
    )
