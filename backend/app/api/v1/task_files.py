import uuid as _uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.services import audit_service
from app.services.task_file_service import TaskFileService

router = APIRouter(prefix="/task-files", tags=["task-files"])
_svc = TaskFileService()


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_task_file(
    file: UploadFile,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    content = await file.read()
    try:
        task_file = await _svc.save_upload(
            db=db,
            tenant_id=user.tenant_id,
            user_id=user.id,
            filename=file.filename or "upload.xlsx",
            content=content,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="task_file",
        action="task_file.upload",
        actor_id=user.id,
        resource_type="task_file",
        resource_id=str(task_file.id),
    )
    await db.commit()
    await db.refresh(task_file)
    return {"id": str(task_file.id), "filename": task_file.filename, "size": task_file.file_size}


@router.get("/{file_id}/download")
async def download_task_file(
    file_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        fid = _uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file ID")
    try:
        task_file, content = await _svc.get_file(db=db, tenant_id=user.tenant_id, file_id=fid)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    media_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if task_file.file_type == "xlsx"
        else "text/csv"
    )
    return StreamingResponse(
        iter([content]),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{task_file.filename}"'},
    )
