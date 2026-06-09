import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_superadmin, require_permission
from app.models.metric_definition import SYSTEM_TENANT_ID
from app.models.user import User
from app.schemas.metric import MetricCreate, MetricResponse, MetricUpdate
from app.services import audit_service
from app.services.metrics.metric_authoring import (
    AuthoringError,
    create_metric,
    update_metric,
    validate_definition,
)

router = APIRouter(prefix="/metrics", tags=["metrics"])


def _to_response(metric) -> MetricResponse:
    return MetricResponse(
        id=str(metric.id),
        key=metric.key,
        display_name=metric.display_name,
        unit=metric.unit,
        source_kind=metric.source_kind,
        status=metric.status,
        version=metric.version,
    )


@router.post("", response_model=MetricResponse, status_code=status.HTTP_201_CREATED)
async def create_tenant_metric(
    payload: MetricCreate,
    user: Annotated[User, Depends(require_permission("metrics.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        validate_definition(payload.model_dump())
        # DB-aware leaf-existence runs inside create_metric (also AuthoringError → 422).
        metric = await create_metric(db, tenant_id=user.tenant_id, payload=payload.model_dump())
    except AuthoringError as ex:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(ex)) from ex
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="metrics",
        action="metric.create",
        actor_id=user.id,
        resource_type="metric_definition",
        resource_id=str(metric.id),
    )
    await db.commit()
    return _to_response(metric)


@router.post("/system", response_model=MetricResponse, status_code=status.HTTP_201_CREATED)
async def create_system_metric(
    payload: MetricCreate,
    user: Annotated[User, Depends(get_current_superadmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Author a SYSTEM-default (cross-tenant) metric. Superadmin-only by row grain."""
    try:
        validate_definition(payload.model_dump())
        # DB-aware leaf-existence runs inside create_metric (also AuthoringError → 422).
        metric = await create_metric(db, tenant_id=SYSTEM_TENANT_ID, payload=payload.model_dump())
    except AuthoringError as ex:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(ex)) from ex
    await audit_service.log_event(
        db=db,
        tenant_id=SYSTEM_TENANT_ID,
        category="metrics",
        action="metric.create",
        actor_id=user.id,
        resource_type="metric_definition",
        resource_id=str(metric.id),
    )
    await db.commit()
    return _to_response(metric)


@router.put("/{metric_id}", response_model=MetricResponse)
async def update_tenant_metric(
    metric_id: uuid.UUID,
    payload: MetricUpdate,
    user: Annotated[User, Depends(require_permission("metrics.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Edit a tenant-owned metric definition: re-validate, bump version, allow status transitions."""
    try:
        metric = await update_metric(
            db,
            tenant_id=user.tenant_id,
            metric_id=metric_id,
            payload=payload.model_dump(exclude_none=True),
        )
    except AuthoringError as ex:
        detail = str(ex)
        if "not found" in detail:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail) from ex
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail) from ex
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="metrics",
        action="metric.update",
        actor_id=user.id,
        resource_type="metric_definition",
        resource_id=str(metric.id),
    )
    await db.commit()
    return _to_response(metric)


@router.put("/system/{metric_id}", response_model=MetricResponse)
async def update_system_metric(
    metric_id: uuid.UUID,
    payload: MetricUpdate,
    user: Annotated[User, Depends(get_current_superadmin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Edit a SYSTEM-default (cross-tenant) metric. Superadmin-only by row grain."""
    try:
        metric = await update_metric(
            db,
            tenant_id=SYSTEM_TENANT_ID,
            metric_id=metric_id,
            payload=payload.model_dump(exclude_none=True),
        )
    except AuthoringError as ex:
        detail = str(ex)
        if "not found" in detail:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail) from ex
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail) from ex
    await audit_service.log_event(
        db=db,
        tenant_id=SYSTEM_TENANT_ID,
        category="metrics",
        action="metric.update",
        actor_id=user.id,
        resource_type="metric_definition",
        resource_id=str(metric.id),
    )
    await db.commit()
    return _to_response(metric)
