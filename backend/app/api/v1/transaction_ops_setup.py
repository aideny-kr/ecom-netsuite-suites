"""Tenant-scoped setup candidates from the mirror; no implied live verification."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, select

from app.api.v1.transaction_ops import Database, Manager
from app.core.database import set_tenant_context
from app.core.dependencies import require_feature
from app.models.celigo import CeligoFlow, CeligoFlowStep, CeligoIntegration
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection

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
    return source, target, connections


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
    )
