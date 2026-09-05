"""Authenticated transaction configuration, investigations and human decisions.

Run creation is durable queueing only. Provider execution/worker registration
belongs to the runner; no request body can provide an approval actor.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_feature, require_permission
from app.models.user import User
from app.schemas.transaction_runs import (
    ConfigControl,
    ConfigCreate,
    ConfigOut,
    FindingOut,
    ProposalDecision,
    ProposalOut,
    RunCreate,
    RunOut,
)
from app.services.transaction_ops import state_service as service

router = APIRouter(
    prefix="/transaction-ops",
    tags=["transaction-ops"],
    dependencies=[Depends(require_feature("celigo")), Depends(require_feature("reconciliation"))],
)
Database = Annotated[AsyncSession, Depends(get_db)]
Reader = Annotated[User, Depends(require_permission("recon.run"))]
Manager = Annotated[User, Depends(require_permission("connections.manage"))]


def _http_error(exc):
    return HTTPException(status_code=exc.http_status, detail={"code": exc.code})


@router.get("/configs", response_model=list[ConfigOut])
async def list_configs(user: Reader, db: Database):
    return await service.list_configs(db, user.tenant_id)


@router.post("/configs", response_model=ConfigOut, status_code=201)
async def create_config(request: ConfigCreate, user: Manager, db: Database):
    try:
        return await service.create_config(db, user.tenant_id, request, actor=user)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.get("/configs/{config_id}", response_model=ConfigOut)
async def get_config(config_id: UUID, user: Reader, db: Database):
    try:
        return await service.get_config(db, user.tenant_id, config_id)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.patch("/configs/{config_id}", response_model=ConfigOut)
async def control_config(config_id: UUID, request: ConfigControl, user: Manager, db: Database):
    try:
        return await service.control_config(db, user.tenant_id, config_id, request, actor=user)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.post("/configs/{config_id}/runs", response_model=RunOut, status_code=202)
async def create_run(config_id: UUID, request: RunCreate, user: Reader, db: Database):
    if request.origin == "schedule":
        raise HTTPException(status_code=422, detail={"code": "schedule_origin_is_worker_only"})
    try:
        return await service.create_run(db, user.tenant_id, config_id, request, actor=user)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    user: Reader, db: Database, config_id: UUID | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 100
):
    return await service.list_runs(db, user.tenant_id, config_id=config_id, limit=limit)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: UUID, user: Reader, db: Database):
    try:
        return await service.get_run(db, user.tenant_id, run_id)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.get("/proposals", response_model=list[ProposalOut])
async def list_proposals(
    user: Reader,
    db: Database,
    run_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return await service.list_proposals(db, user.tenant_id, run_id=run_id, limit=limit, offset=offset)


@router.get("/runs/{run_id}/findings", response_model=list[FindingOut])
async def list_findings(
    run_id: UUID,
    user: Reader,
    db: Database,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
):
    try:
        return await service.list_findings(db, user.tenant_id, run_id, offset=offset, limit=limit)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.get("/proposals/{proposal_id}", response_model=ProposalOut)
async def get_proposal(proposal_id: UUID, user: Reader, db: Database):
    try:
        return await service.get_proposal(db, user.tenant_id, proposal_id)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.post("/proposals/{proposal_id}/decision", response_model=ProposalOut)
async def decide_proposal(proposal_id: UUID, request: ProposalDecision, user: Reader, db: Database):
    # This authenticated API is the only human-approval ingress. No equivalent
    # write tool is registered for chat/LLM or unattended workers.
    try:
        return await service.decide_proposal(db, user.tenant_id, proposal_id, request, actor=user)
    except service.StateError as exc:
        raise _http_error(exc) from None
