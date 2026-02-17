"""REST API for Dev Workspace: workspaces, files, changesets, patches."""

import uuid

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.user import User
from app.schemas.workspace import (
    ChangeSetCreate,
    ChangeSetTransition,
    WorkspaceCreate,
)
from app.services import audit_service
from app.services import workspace_service as ws_svc

logger = structlog.get_logger()

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _serialize_workspace(ws) -> dict:
    return {
        "id": str(ws.id),
        "tenant_id": str(ws.tenant_id),
        "name": ws.name,
        "description": ws.description,
        "status": ws.status,
        "created_by": str(ws.created_by),
        "created_at": ws.created_at.isoformat(),
        "updated_at": ws.updated_at.isoformat(),
    }


def _serialize_changeset(cs) -> dict:
    result = {
        "id": str(cs.id),
        "workspace_id": str(cs.workspace_id),
        "title": cs.title,
        "description": cs.description,
        "status": cs.status,
        "proposed_by": str(cs.proposed_by),
        "reviewed_by": str(cs.reviewed_by) if cs.reviewed_by else None,
        "applied_by": str(cs.applied_by) if cs.applied_by else None,
        "proposed_at": cs.proposed_at.isoformat() if cs.proposed_at else None,
        "reviewed_at": cs.reviewed_at.isoformat() if cs.reviewed_at else None,
        "applied_at": cs.applied_at.isoformat() if cs.applied_at else None,
        "rejection_reason": cs.rejection_reason,
        "created_at": cs.created_at.isoformat(),
        "updated_at": cs.updated_at.isoformat(),
    }
    if "patches" in cs.__dict__ and cs.__dict__["patches"]:
        result["patches"] = [
            {
                "id": str(p.id),
                "changeset_id": str(p.changeset_id),
                "file_path": p.file_path,
                "operation": p.operation,
                "unified_diff": p.unified_diff,
                "new_content": p.new_content,
                "baseline_sha256": p.baseline_sha256,
                "apply_order": p.apply_order,
                "created_at": p.created_at.isoformat(),
            }
            for p in cs.patches
        ]
    return result


# --- Workspace CRUD ---


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate,
    user: User = Depends(require_permission("workspace.manage")),
    db: AsyncSession = Depends(get_db),
):
    ws = await ws_svc.create_workspace(db, user.tenant_id, body.name, user.id, body.description)
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="workspace",
        action="workspace.created",
        actor_id=user.id,
        resource_type="workspace",
        resource_id=str(ws.id),
        payload={"name": body.name},
    )
    await db.commit()
    return _serialize_workspace(ws)


@router.get("")
async def list_workspaces(
    user: User = Depends(require_permission("workspace.view")),
    db: AsyncSession = Depends(get_db),
):
    workspaces = await ws_svc.list_workspaces(db, user.tenant_id)
    return [_serialize_workspace(ws) for ws in workspaces]


@router.get("/{workspace_id}")
async def get_workspace(
    workspace_id: uuid.UUID,
    user: User = Depends(require_permission("workspace.view")),
    db: AsyncSession = Depends(get_db),
):
    ws = await ws_svc.get_workspace(db, workspace_id, user.tenant_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return _serialize_workspace(ws)


@router.post("/{workspace_id}/import", status_code=status.HTTP_201_CREATED)
async def import_workspace(
    workspace_id: uuid.UUID,
    file: UploadFile = File(...),
    user: User = Depends(require_permission("workspace.manage")),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")

    zip_bytes = await file.read()
    if len(zip_bytes) > 50 * 1024 * 1024:  # 50MB limit
        raise HTTPException(status_code=400, detail="File too large (max 50MB)")

    try:
        result = await ws_svc.import_workspace(db, workspace_id, user.tenant_id, zip_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="workspace",
        action="workspace.imported",
        actor_id=user.id,
        resource_type="workspace",
        resource_id=str(workspace_id),
        payload=result,
    )
    await db.commit()
    return result


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_workspace(
    workspace_id: uuid.UUID,
    user: User = Depends(require_permission("workspace.manage")),
    db: AsyncSession = Depends(get_db),
):
    ws = await ws_svc.archive_workspace(db, workspace_id, user.tenant_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="workspace",
        action="workspace.archived",
        actor_id=user.id,
        resource_type="workspace",
        resource_id=str(workspace_id),
    )
    await db.commit()


# --- Files ---


@router.get("/{workspace_id}/files")
async def list_files(
    workspace_id: uuid.UUID,
    prefix: str | None = None,
    recursive: bool = True,
    user: User = Depends(require_permission("workspace.view")),
    db: AsyncSession = Depends(get_db),
):
    files = await ws_svc.list_files(db, workspace_id, user.tenant_id, prefix, recursive)
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="workspace",
        action="workspace.files.listed",
        actor_id=user.id,
        resource_type="workspace",
        resource_id=str(workspace_id),
        payload={"prefix": prefix, "recursive": recursive, "file_count": len(files)},
    )
    await db.commit()
    return files


@router.get("/{workspace_id}/files/{file_id}")
async def read_file(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    line_start: int = 1,
    line_end: int | None = None,
    user: User = Depends(require_permission("workspace.view")),
    db: AsyncSession = Depends(get_db),
):
    result = await ws_svc.read_file(db, workspace_id, file_id, user.tenant_id, line_start, line_end)
    if not result:
        raise HTTPException(status_code=404, detail="File not found")
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="workspace",
        action="workspace.file.read",
        actor_id=user.id,
        resource_type="workspace",
        resource_id=str(workspace_id),
        payload={
            "workspace_id": str(workspace_id),
            "file_id": str(file_id),
            "line_start": line_start,
            "line_end": line_end,
        },
    )
    await db.commit()
    return result


@router.get("/{workspace_id}/search")
async def search_files(
    workspace_id: uuid.UUID,
    query: str,
    search_type: str = "filename",
    limit: int = 20,
    user: User = Depends(require_permission("workspace.view")),
    db: AsyncSession = Depends(get_db),
):
    results = await ws_svc.search_files(db, workspace_id, user.tenant_id, query, search_type, limit)
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="workspace",
        action="workspace.files.searched",
        actor_id=user.id,
        resource_type="workspace",
        resource_id=str(workspace_id),
        payload={"query": query, "search_type": search_type, "result_count": len(results)},
    )
    await db.commit()
    return results


# --- Changesets ---


@router.post("/{workspace_id}/changesets", status_code=status.HTTP_201_CREATED)
async def create_changeset(
    workspace_id: uuid.UUID,
    body: ChangeSetCreate,
    user: User = Depends(require_permission("workspace.manage")),
    db: AsyncSession = Depends(get_db),
):
    cs = await ws_svc.create_changeset(db, workspace_id, user.tenant_id, body.title, user.id, body.description)
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="workspace",
        action="changeset.created",
        actor_id=user.id,
        resource_type="changeset",
        resource_id=str(cs.id),
        payload={"title": body.title, "workspace_id": str(workspace_id)},
    )
    await db.commit()
    return _serialize_changeset(cs)


@router.get("/{workspace_id}/changesets")
async def list_changesets(
    workspace_id: uuid.UUID,
    user: User = Depends(require_permission("workspace.view")),
    db: AsyncSession = Depends(get_db),
):
    changesets = await ws_svc.list_changesets(db, workspace_id, user.tenant_id)
    return [_serialize_changeset(cs) for cs in changesets]


# Changeset detail/transition/apply routes (not workspace-scoped)

changeset_router = APIRouter(prefix="/changesets", tags=["workspaces"])


@changeset_router.get("/{changeset_id}")
async def get_changeset(
    changeset_id: uuid.UUID,
    user: User = Depends(require_permission("workspace.view")),
    db: AsyncSession = Depends(get_db),
):
    cs = await ws_svc.get_changeset(db, changeset_id, user.tenant_id)
    if not cs:
        raise HTTPException(status_code=404, detail="Changeset not found")
    return _serialize_changeset(cs)


@changeset_router.get("/{changeset_id}/diff")
async def get_changeset_diff(
    changeset_id: uuid.UUID,
    user: User = Depends(require_permission("workspace.view")),
    db: AsyncSession = Depends(get_db),
):
    diff = await ws_svc.get_changeset_diff(db, changeset_id, user.tenant_id)
    if not diff:
        raise HTTPException(status_code=404, detail="Changeset not found")
    return diff


@changeset_router.post("/{changeset_id}/transition")
async def transition_changeset(
    changeset_id: uuid.UUID,
    body: ChangeSetTransition,
    user: User = Depends(require_permission("workspace.review")),
    db: AsyncSession = Depends(get_db),
):
    # Capture old status for transition audit
    old_cs = await ws_svc.get_changeset(db, changeset_id, user.tenant_id)
    if not old_cs:
        raise HTTPException(status_code=404, detail="Changeset not found")
    old_status = old_cs.status

    try:
        cs = await ws_svc.transition_changeset(
            db, changeset_id, user.tenant_id, body.action, user.id, body.rejection_reason
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    new_status = cs.status
    await db.refresh(cs)
    response = _serialize_changeset(cs)
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="workspace",
        action=f"changeset.{body.action}",
        actor_id=user.id,
        resource_type="changeset",
        resource_id=str(changeset_id),
        payload={"action": body.action, "new_status": new_status},
    )
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="workspace",
        action="changeset.transitioned",
        actor_id=user.id,
        resource_type="changeset",
        resource_id=str(changeset_id),
        payload={"from_status": old_status, "to_status": new_status, "action": body.action},
    )
    await db.commit()
    return response


@changeset_router.post("/{changeset_id}/apply")
async def apply_changeset(
    changeset_id: uuid.UUID,
    user: User = Depends(require_permission("workspace.apply")),
    db: AsyncSession = Depends(get_db),
):
    try:
        cs = await ws_svc.apply_changeset(db, changeset_id, user.tenant_id, user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="workspace",
        action="changeset.applied",
        actor_id=user.id,
        resource_type="changeset",
        resource_id=str(changeset_id),
    )
    await db.flush()
    await db.refresh(cs)
    response = _serialize_changeset(cs)
    await db.commit()
    return response
