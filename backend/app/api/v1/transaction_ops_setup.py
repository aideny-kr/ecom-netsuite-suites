"""Tenant-scoped setup candidates from the mirror; no implied live verification."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import redis
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from starlette.concurrency import run_in_threadpool

from app.api.v1.transaction_ops import Database, Manager
from app.core.database import set_tenant_context
from app.core.dependencies import require_feature
from app.models.celigo import CeligoFlow, CeligoFlowStep, CeligoIntegration
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.services import audit_service
from app.workers.tasks import celigo_flow_map_sync as source_refresh

router = APIRouter(
    prefix="/transaction-ops/setup",
    tags=["transaction-ops"],
    dependencies=[Depends(require_feature("celigo")), Depends(require_feature("reconciliation"))],
)


class StepOption(BaseModel):
    id: UUID
    reference_name: str | None
    flow_name: str
    integration_name: str
    connection_label: str
    mirrored_at: datetime | None
    sandbox: bool | None
    record_type: str | None
    operation: str | None
    provider_verified: Literal[False] = False


class ConnectionOption(BaseModel):
    id: UUID
    label: str
    account_id: str | None
    status: str


class SetupOptions(BaseModel):
    source_steps: list[StepOption]
    target_steps: list[StepOption]
    netsuite_connections: list[ConnectionOption]
    source_has_more: bool
    target_has_more: bool
    connection_has_more: bool
    solidus_connections: list[ConnectionOption] = Field(default_factory=list)
    solidus_has_more: bool = False


class SourceRefreshStatus(BaseModel):
    request_id: UUID
    status: Literal["queued", "running", "completed", "failed"]
    already_running: bool = False
    poll_after_seconds: int = 3
    error_code: Literal["refresh_failed"] | None = None


def _refresh_response(row: dict) -> SourceRefreshStatus:
    return SourceRefreshStatus(
        request_id=row["request_id"],
        status=row["status"],
        already_running=row["already_running"],
        error_code="refresh_failed" if row["status"] == "failed" else None,
    )


@router.post("/refresh-sources", response_model=SourceRefreshStatus, status_code=202)
async def refresh_sources(user: Manager, db: Database):
    """Refresh one tenant's existing Celigo mirror; no credential or order writes."""
    await set_tenant_context(db, str(user.tenant_id))
    connection_ids = (
        (
            await db.execute(
                select(Connection.id)
                .where(
                    Connection.tenant_id == user.tenant_id,
                    Connection.provider == "celigo",
                    Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
                )
                .order_by(Connection.id)
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if not connection_ids:
        raise HTTPException(status_code=409, detail={"code": "celigo_connection_required"})
    if len(connection_ids) != 1:
        raise HTTPException(status_code=409, detail={"code": "multiple_celigo_connections"})
    tenant_id, connection_id = str(user.tenant_id), str(connection_ids[0])
    try:
        row = await run_in_threadpool(source_refresh.reserve_refresh, tenant_id, connection_id)
    except redis.RedisError:
        raise HTTPException(status_code=503, detail={"code": "source_refresh_unavailable"}) from None
    if row["already_running"]:
        return _refresh_response(row)
    try:
        await audit_service.log_event(
            db=db,
            tenant_id=user.tenant_id,
            category="transaction_ops",
            action="transaction_ops.sources.refresh_requested",
            actor_id=user.id,
            resource_type="connection",
            resource_id=connection_id,
            correlation_id=row["request_id"],
        )
        await db.commit()
        await run_in_threadpool(
            source_refresh.celery_app.send_task,
            "tasks.celigo_flow_map_sync",
            kwargs={"tenant_id": tenant_id, "connection_id": connection_id, "setup_refresh_id": row["request_id"]},
            queue="sync",
            task_id=row["request_id"],
            expires=source_refresh.REFRESH_QUEUE_SECONDS,
        )
    except Exception:
        # Dispatch may have reached the broker before an error. Owner-checked
        # cancellation makes a delayed queued delivery unable to start afterward.
        await db.rollback()
        try:
            await run_in_threadpool(source_refresh.cancel_refresh, tenant_id, connection_id, row["request_id"])
        except redis.RedisError:
            pass  # Remains reserved until its bounded lease expires; fail closed.
        raise HTTPException(status_code=503, detail={"code": "source_refresh_unavailable"}) from None
    return _refresh_response(row)


@router.get("/refresh-sources/{request_id}", response_model=SourceRefreshStatus)
async def get_source_refresh(request_id: UUID, user: Manager):
    try:
        row = await run_in_threadpool(source_refresh.read_refresh, str(user.tenant_id), str(request_id))
    except redis.RedisError:
        raise HTTPException(status_code=503, detail={"code": "source_refresh_unavailable"}) from None
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "source_refresh_not_found"})
    return _refresh_response(row)


def _option_queries(tenant_id, *, offset, limit):
    steps = (
        select(
            CeligoFlowStep.id,
            CeligoFlowStep.reference_name,
            CeligoFlow.name.label("flow_name"),
            CeligoIntegration.name.label("integration_name"),
            Connection.label.label("connection_label"),
            CeligoFlowStep.updated_at.label("mirrored_at"),
            CeligoIntegration.sandbox,
            CeligoFlowStep.record_type,
            CeligoFlowStep.operation,
        )
        .join(
            CeligoFlow,
            and_(
                CeligoFlow.id == CeligoFlowStep.flow_id,
                CeligoFlow.tenant_id == tenant_id,
                CeligoFlow.celigo_connection_id == CeligoFlowStep.celigo_connection_id,
            ),
        )
        .join(
            CeligoIntegration,
            and_(
                CeligoIntegration.id == CeligoFlow.integration_id,
                CeligoIntegration.tenant_id == tenant_id,
                CeligoIntegration.celigo_connection_id == CeligoFlow.celigo_connection_id,
            ),
        )
        .join(
            Connection,
            and_(
                Connection.id == CeligoFlowStep.celigo_connection_id,
                Connection.tenant_id == tenant_id,
                Connection.provider == "celigo",
                Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
            ),
        )
        .where(CeligoFlowStep.tenant_id == tenant_id, CeligoFlow.disabled.is_not(True))
        .order_by(CeligoIntegration.name, CeligoFlow.name, CeligoFlowStep.sequence, CeligoFlowStep.id)
        .offset(offset)
        .limit(limit + 1)
    )
    source = steps.where(CeligoFlowStep.adaptor_type == "HTTPExport")
    target = steps.where(
        CeligoFlowStep.role == "processor",
        CeligoFlowStep.record_type == "salesorder",
        CeligoFlowStep.operation.in_(("add", "update", "addupdate")),
    )
    connections = (
        select(
            Connection.id,
            Connection.label,
            Connection.metadata_json["account_id"].as_string().label("account_id"),
            Connection.status,
        )
        .where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
        )
        .order_by(Connection.label, Connection.id)
        .offset(offset)
        .limit(limit + 1)
    )
    solidus = (
        select(
            Connection.id,
            Connection.label,
            Connection.metadata_json["account_id"].as_string().label("account_id"),
            Connection.status,
        )
        .where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "solidus",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
            Connection.metadata_json["api_profile"].as_string() == "framework_sync",
        )
        .order_by(Connection.label, Connection.id)
        .offset(offset)
        .limit(limit + 1)
    )
    return source, target, connections, solidus


@router.get("/options", response_model=SetupOptions)
async def list_setup_options(
    user: Manager,
    db: Database,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
):
    await set_tenant_context(db, str(user.tenant_id))
    pages = [
        (await db.execute(query)).mappings().all()
        for query in _option_queries(user.tenant_id, offset=offset, limit=limit)
    ]
    return SetupOptions(
        source_steps=pages[0][:limit],
        target_steps=pages[1][:limit],
        netsuite_connections=pages[2][:limit],
        source_has_more=len(pages[0]) > limit,
        target_has_more=len(pages[1]) > limit,
        connection_has_more=len(pages[2]) > limit,
        solidus_connections=pages[3][:limit],
        solidus_has_more=len(pages[3]) > limit,
    )
