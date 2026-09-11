"""Authenticated transaction configuration, investigations and human decisions.

Run creation persists before bounded broker publication. Provider execution
belongs to the worker; no request body can provide an approval actor.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_feature, require_permission
from app.models.user import User
from app.schemas.transaction_runs import (
    CaseObservationOut,
    CaseOut,
    ConfigControl,
    ConfigCreate,
    ConfigOut,
    FindingOut,
    ProposalDecision,
    ProposalOut,
    RunCreate,
    RunOut,
)
from app.services.transaction_ops import case_service, order_actions, period_review, scheduler
from app.services.transaction_ops import state_service as service
from app.services.transaction_ops.period_review import PeriodReview

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


async def _publish_pending(run, tenant_id):
    if run.status == "pending":
        # Creation has committed. Publication is bounded and deduplicated;
        # failures leave this durable run available to scheduler recovery.
        await scheduler._dispatch(tenant_id, run.id, {"dispatched": 0, "dispatch_failed": 0})
    return run


class OrderInvestigation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evaluation_key: UUID


class SourceReconciliation(OrderInvestigation):
    source_connection_id: UUID
    window_start: AwareDatetime
    window_end: AwareDatetime


@router.post("/orders/{order_id}/investigate", response_model=RunOut, status_code=202)
async def investigate_order(order_id: UUID, request: OrderInvestigation, user: Reader, db: Database):
    try:
        return await order_actions.investigate_order(db, user, order_id, request.evaluation_key)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.post("/reconcile", response_model=list[RunOut], status_code=202)
async def reconcile_source(request: SourceReconciliation, user: Reader, db: Database):
    try:
        scope = RunCreate(
            evaluation_key=str(request.evaluation_key), window_start=request.window_start, window_end=request.window_end
        )
        return await order_actions.reconcile_source(db, user, request.source_connection_id, scope)
    except service.StateError as exc:
        raise _http_error(exc) from None
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "invalid_reconciliation_window"}) from None


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


class AccountingProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A required null explicitly disables the current treatment.
    sales_credit_profile: dict | None


@router.put("/configs/{config_id}/accounting-profile")
async def configure_accounting_profile(config_id: UUID, request: AccountingProfileUpdate, user: Manager, db: Database):
    from app.services.transaction_ops.accounting_profiles import configure_sales_credit_profile

    try:
        return await configure_sales_credit_profile(
            db, user.tenant_id, config_id, request.sales_credit_profile, actor=user
        )
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.post("/configs/{config_id}/runs", response_model=RunOut, status_code=202)
async def create_run(config_id: UUID, request: RunCreate, user: Reader, db: Database):
    if request.origin == "schedule":
        raise HTTPException(status_code=422, detail={"code": "schedule_origin_is_worker_only"})
    if request.review is not None:
        raise HTTPException(status_code=422, detail={"code": "review_scope_is_server_owned"})
    try:
        run = await service.create_run(db, user.tenant_id, config_id, request, actor=user)
        return await _publish_pending(run, user.tenant_id)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.post("/configs/{config_id}/review", response_model=RunOut, status_code=202)
async def review_period(config_id: UUID, request: PeriodReview, user: Reader, db: Database):
    try:
        run = await period_review.create_review(db, user.tenant_id, config_id, request, actor=user)
        return await _publish_pending(run, user.tenant_id)
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
        run = await service.get_run(db, user.tenant_id, run_id)
        result = RunOut.model_validate(run)
        if run.termination_reason == "budget":
            from app.services.transaction_ops.continuation import continuation_result

            child, blocked = await continuation_result(db, user.tenant_id, run_id)
            result = result.model_copy(
                update={
                    "continuation_run_id": child.id if child else None,
                    "continuation_blocked": blocked.get("reason") if blocked else None,
                }
            )
        return result
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


@router.get("/operations/{operation_id}/settlement")
async def operation_settlement(operation_id: UUID, user: Reader, db: Database):
    from app.services.transaction_ops import settlement

    try:
        return await settlement.status(db, user.tenant_id, operation_id)
    except service.StateError as exc:
        raise _http_error(exc) from None


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


@router.get("/case-groups")
async def list_case_groups(
    user: Reader,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    review_run_ids: Annotated[list[UUID] | None, Query(max_length=20)] = None,
    status: Literal["matched", "needs_review", "not_verified"] | None = None,
    search: Annotated[str, Query(max_length=200)] = "",
):
    from app.services.transaction_ops.case_groups import list_groups

    try:
        return await list_groups(
            db, user.tenant_id, limit=limit, offset=offset, review_run_ids=review_run_ids, status=status, search=search
        )
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.get("/case-groups/{group_id}/cases")
async def list_group_cases(
    group_id: str,
    user: Reader,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    review_run_ids: Annotated[list[UUID] | None, Query(max_length=20)] = None,
    status: Literal["matched", "needs_review", "not_verified"] | None = None,
    search: Annotated[str, Query(max_length=200)] = "",
):
    from app.services.transaction_ops.case_groups import group_members

    try:
        return await group_members(
            db,
            user.tenant_id,
            group_id,
            limit=limit,
            offset=offset,
            review_run_ids=review_run_ids,
            status=status,
            search=search,
        )
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.get("/cases", response_model=list[CaseOut])
async def list_cases(
    user: Reader,
    db: Database,
    status: Literal["open", "reconciled"] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return await case_service.list_cases(db, user.tenant_id, status=status, limit=limit, offset=offset)


@router.get("/cases/{case_id}", response_model=CaseOut)
async def get_case(case_id: UUID, user: Reader, db: Database):
    try:
        return await case_service.get_case(db, user.tenant_id, case_id)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.get("/cases/{case_id}/observations", response_model=list[CaseObservationOut])
async def case_observations(
    case_id: UUID,
    user: Reader,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    try:
        return await case_service.list_observations(db, user.tenant_id, case_id, limit=limit, offset=offset)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.get("/cases/{case_id}/resolution-history")
async def case_resolution_history(
    case_id: UUID,
    user: Reader,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    from app.services.transaction_ops.resolution_history import history

    try:
        return await history(db, user.tenant_id, case_id, limit=limit, offset=offset)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.post("/cases/{case_id}/investigate", response_model=RunOut, status_code=202)
async def investigate_case(case_id: UUID, request: OrderInvestigation, user: Reader, db: Database):
    try:
        return await case_service.investigate_case(db, user.tenant_id, case_id, request.evaluation_key, actor=user)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.get("/runs/{run_id}/review")
async def review_status(run_id: UUID, user: Reader, db: Database):
    try:
        return await period_review.review_status(db, user.tenant_id, run_id)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.get("/runs/{run_id}/review/findings")
async def review_findings(
    run_id: UUID,
    user: Reader,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Literal["matched", "needs_review", "not_verified"] | None = None,
    search: Annotated[str, Query(max_length=100)] = "",
):
    try:
        return await period_review.review_results(
            db, user.tenant_id, run_id, limit=limit, offset=offset, status=status, search=search
        )
    except service.StateError as exc:
        raise _http_error(exc) from None


class CaseBatchInvestigation(OrderInvestigation):
    case_ids: tuple[UUID, ...] = Field(min_length=1, max_length=50)


@router.post("/cases/investigate", status_code=202)
async def investigate_cases(request: CaseBatchInvestigation, user: Reader, db: Database):
    try:
        return await case_service.investigate_cases(
            db, user.tenant_id, request.case_ids, request.evaluation_key, actor=user
        )
    except service.StateError as exc:
        raise _http_error(exc) from None


class ReviewExport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_run_ids: list[UUID] = Field(min_length=1, max_length=20)
    status: Literal["matched", "needs_review", "not_verified"] | None = None
    search: str = Field(default="", max_length=200)


@router.get("/review-results")
async def selected_review_results(
    user: Reader,
    db: Database,
    review_run_ids: Annotated[list[UUID], Query(min_length=1, max_length=20)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Literal["matched", "needs_review", "not_verified"] | None = None,
    search: Annotated[str, Query(max_length=200)] = "",
):
    from app.services.transaction_ops.workspace_results import review_page

    try:
        return await review_page(
            db, user.tenant_id, review_run_ids, limit=limit, offset=offset, status=status, search=search
        )
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.get("/workspace-page")
async def workspace_page(
    user: Reader,
    db: Database,
    view: Literal["cases", "runs", "proposals"],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    config_id: UUID | None = None,
):
    from app.services.transaction_ops.workspace_results import record_page

    try:
        return await record_page(db, user.tenant_id, view, limit=limit, offset=offset, config_id=config_id)
    except service.StateError as exc:
        raise _http_error(exc) from None


@router.post("/review-export")
async def download_review_report(request: ReviewExport, user: Reader, db: Database):
    from fastapi.responses import Response

    from app.services.transaction_ops.excel_report import export_review

    try:
        content, report_id = await export_review(
            db, user, request.review_run_ids, status=request.status, search=request.search
        )
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="reconciliation-{report_id}.xlsx"',
                "Cache-Control": "no-store",
            },
        )
    except service.StateError as exc:
        raise _http_error(exc) from None
