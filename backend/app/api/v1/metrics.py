from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.user import User
from app.schemas.metric import MetricCreate, MetricResponse
from app.services import audit_service
from app.services.metrics.metric_authoring import (
    AuthoringError,
    create_metric,
    validate_definition,
)

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.post("", response_model=MetricResponse, status_code=status.HTTP_201_CREATED)
async def create_tenant_metric(
    payload: MetricCreate,
    user: Annotated[User, Depends(require_permission("metrics.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        validate_definition(payload.model_dump())
    except AuthoringError as ex:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(ex)) from ex
    metric = await create_metric(db, tenant_id=user.tenant_id, payload=payload.model_dump())
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
    return MetricResponse(
        id=str(metric.id),
        key=metric.key,
        display_name=metric.display_name,
        unit=metric.unit,
        source_kind=metric.source_kind,
        status=metric.status,
        version=metric.version,
    )
