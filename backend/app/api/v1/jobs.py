import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.job import Job
from app.models.user import User
from app.schemas.common import JobResponse, PaginatedResponse

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=PaginatedResponse[JobResponse])
async def list_jobs(
    user: Annotated[User, Depends(require_permission("tables.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    count_result = await db.execute(
        select(func.count()).select_from(Job).where(Job.tenant_id == user.tenant_id)
    )
    total = count_result.scalar() or 0

    offset = (page - 1) * page_size
    result = await db.execute(
        select(Job)
        .where(Job.tenant_id == user.tenant_id)
        .order_by(Job.created_at.desc())
        .offset(offset).limit(page_size)
    )
    jobs = result.scalars().all()

    items = [
        JobResponse(
            id=str(j.id), tenant_id=str(j.tenant_id), job_type=j.job_type,
            status=j.status, correlation_id=j.correlation_id,
            connection_id=str(j.connection_id) if j.connection_id else None,
            started_at=j.started_at.isoformat() if j.started_at else None,
            completed_at=j.completed_at.isoformat() if j.completed_at else None,
            parameters=j.parameters, result_summary=j.result_summary,
            error_message=j.error_message, celery_task_id=j.celery_task_id,
        )
        for j in jobs
    ]

    pages = (total + page_size - 1) // page_size if page_size > 0 else 0
    return PaginatedResponse(items=items, total=total, page=page, page_size=page_size, pages=pages)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("tables.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.tenant_id == user.tenant_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobResponse(
        id=str(job.id), tenant_id=str(job.tenant_id), job_type=job.job_type,
        status=job.status, correlation_id=job.correlation_id,
        connection_id=str(job.connection_id) if job.connection_id else None,
        started_at=job.started_at.isoformat() if job.started_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
        parameters=job.parameters, result_summary=job.result_summary,
        error_message=job.error_message, celery_task_id=job.celery_task_id,
    )
