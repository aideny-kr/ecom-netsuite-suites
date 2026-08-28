"""Task 8 — read APIs over the synced Celigo flow map (migration 094+095's
eight tables, Task 5's models + repository).

Every endpoint here is gated on `connections.view` AND `require_feature("celigo")`
-- mirrors the four `/connector-status/celigo*` endpoints in `connector_status.py`
exactly (Plan A established the flag must gate the API, not just the UI).

PII RULING (task-8 brief, not covered by the plan): `celigo_flow_errors.message`
and `celigo_error_signatures.sample_message` hold raw Celigo error text -- real
customer emails, order references. Returning them to an authenticated
SAME-TENANT admin is legitimate and necessary: an operator triaging a failed
flow needs to know which order broke, and stripping the message makes the
feature useless. `sanitizer.py`/`repository.py`'s "never log the message" rule
is about LOG LINES, a different threat (structured logs are searchable,
aggregated, and often shipped to a third party) -- it does not extend to an
authorized API response. This module still never logs either field itself, and
nothing here constructs an exception message or `logger.*(...)` call from
either column.

EXPLICIT RESPONSE MODELS, DELIBERATELY -- the single most important design
constraint in this file. Every response model below names every field by hand;
nothing uses `from_attributes=True`/`model_validate(orm_row)` against a mapped
class. An ORM auto-dump would make a column added to `app/models/celigo.py`
next month silently API-visible with no review of whether it's safe to expose
(`raw_json` on four of these tables is exactly the column that must never do
that). The explicit-field boundary here IS the review point.

EXPLICIT TENANT SCOPING IN EVERY QUERY -- RLS (FORCE, per
`test_celigo_flow_map_rls.py`) is the backstop, not the plan. Every SELECT
below filters on `tenant_id == user.tenant_id` in application code as well.

This module is READ-ONLY: no Celigo write verb, no mutation of any of the
eight tables. It does not call `app.services.celigo.repository`'s upsert/mark
functions -- only `list_logical_scripts` (a pure read helper) for script
clone-family collapsing, per that module's own docstring.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.connector_status import _get_celigo_connection
from app.core.database import get_db
from app.core.dependencies import require_feature, require_permission
from app.models.celigo import (
    CeligoErrorSignature,
    CeligoFlow,
    CeligoFlowError,
    CeligoFlowStep,
    CeligoIntegration,
    CeligoScript,
    CeligoScriptAttachment,
)
from app.models.user import User
from app.services.celigo.repository import list_logical_scripts

router = APIRouter(prefix="/celigo", tags=["celigo"])


# ---------------------------------------------------------------------------
# Response schemas -- see module docstring: every field named by hand.
# ---------------------------------------------------------------------------


class CeligoIntegrationOut(BaseModel):
    id: str
    celigo_id: str
    name: str
    sandbox: bool | None
    mode: str | None
    description: str | None
    celigo_last_modified: datetime | None


class CeligoFlowSummaryOut(BaseModel):
    """One row of `GET /celigo/integrations/{id}/flows`. `error_count`/
    `signature_count` are OPEN counts (`resolved_at IS NULL AND purged_at IS
    NULL`) computed with one GROUP BY query across every flow in the
    integration -- never N+1. `disabled` flows are never filtered out (mockup
    spec: paused flows stay visible, dimmed by the frontend on this flag)."""

    id: str
    celigo_id: str
    name: str
    disabled: bool | None
    schedule: dict | None
    timezone: str | None
    last_executed_at: datetime | None
    error_count: int
    signature_count: int


class CeligoAttachmentOut(BaseModel):
    id: str
    flow_id: str
    flow_step_id: str | None
    script_id: str | None
    script_celigo_id: str
    function_name: str | None
    json_path: str
    site_type: str | None


class CeligoFlowStepOut(BaseModel):
    id: str
    celigo_id: str
    role: str
    router_id: str | None
    branch_id: str | None
    branch_key: str
    sequence: int
    adaptor_type: str | None
    connection_celigo_id: str | None
    filter_json: dict | None
    mapping_json: dict | None
    proceed_on_failure: bool | None
    skip_retries: bool | None
    attachments: list[CeligoAttachmentOut]


class CeligoFlowDetailOut(BaseModel):
    id: str
    integration_id: str
    celigo_id: str
    name: str
    disabled: bool | None
    schedule: dict | None
    timezone: str | None
    last_executed_at: datetime | None
    source_id: str | None
    ai_description_summary: str | None
    ai_description_detailed: str | None
    celigo_last_modified: datetime | None
    steps: list[CeligoFlowStepOut]
    # Attachments with no owning step -- e.g. a `routers[].script` ref, which
    # belongs to the router itself, not to any one page-generator/processor
    # step (see `app/models/celigo.py`'s `CeligoScriptAttachment` docstring).
    unassigned_attachments: list[CeligoAttachmentOut]


class CeligoScriptAttachmentSiteOut(BaseModel):
    """One row of the script viewer's "Attached to / Where / Function" table
    (mockup spec, Screen 04). `script_celigo_id` names WHICH clone in the
    logical group was actually attached at this site -- clones can diverge, so
    this is not always the id the caller looked up."""

    flow_id: str
    flow_name: str
    flow_step_id: str | None
    flow_step_role: str | None
    flow_step_adaptor_type: str | None
    script_celigo_id: str
    json_path: str
    function_name: str | None
    site_type: str | None


class CeligoScriptOut(BaseModel):
    """`content`/`content_hash`/`name` are THIS script row's own values -- the
    one the caller asked for by id, not necessarily the clone family's
    "representative" (`list_logical_scripts` picks a representative for
    display purposes in the LIST view; a clone's content can legitimately
    diverge from its original, per `content_diverged` below, so the detail
    view shows exactly the row the caller navigated to). `dedup_key`/
    `copies_count`/`attachment_count`/`content_diverged` describe the whole
    clone family this script belongs to (`app.services.celigo.repository.
    list_logical_scripts` -- reused verbatim, not reimplemented here)."""

    id: str
    dedup_key: str
    name: str
    content: str | None
    content_hash: str | None
    copies_count: int
    attachment_count: int
    content_diverged: bool
    used_by: list[CeligoScriptAttachmentSiteOut]


class CeligoErrorOut(BaseModel):
    id: str
    celigo_id: str
    flow_id: str | None
    flow_step_id: str | None
    trace_key: str | None
    source: str | None
    code: str | None
    # PII -- see module docstring's PII ruling. Never logged.
    message: str | None
    occurred_at: datetime | None
    purge_at: datetime | None
    resolved_at: datetime | None
    purged_at: datetime | None
    retriable: bool | None


class CeligoErrorSignatureOut(BaseModel):
    id: str
    fingerprint: str
    source: str | None
    code: str | None
    # PII -- see module docstring's PII ruling. Never logged.
    sample_message: str | None
    occurrence_count: int
    first_seen: datetime | None
    last_seen: datetime | None


class CeligoErrorsResponse(BaseModel):
    signature: CeligoErrorSignatureOut
    errors: list[CeligoErrorOut]


# ---------------------------------------------------------------------------
# GET /celigo/integrations
# ---------------------------------------------------------------------------


@router.get("/integrations", response_model=list[CeligoIntegrationOut])
async def list_integrations(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Integrations synced under the tenant's currently active Celigo
    connection. Empty (never 404) when there is no active connection -- that
    is a legitimate "not connected yet" state, not an error."""
    connection = await _get_celigo_connection(db, user.tenant_id)
    if connection is None:
        return []

    integrations = (
        (
            await db.execute(
                select(CeligoIntegration)
                .where(
                    CeligoIntegration.tenant_id == user.tenant_id,
                    CeligoIntegration.celigo_connection_id == connection.id,
                )
                .order_by(CeligoIntegration.name)
            )
        )
        .scalars()
        .all()
    )
    return [
        CeligoIntegrationOut(
            id=str(i.id),
            celigo_id=i.celigo_id,
            name=i.name,
            sandbox=i.sandbox,
            mode=i.mode,
            description=i.description,
            celigo_last_modified=i.celigo_last_modified,
        )
        for i in integrations
    ]


# ---------------------------------------------------------------------------
# GET /celigo/integrations/{id}/flows
# ---------------------------------------------------------------------------


@router.get("/integrations/{integration_id}/flows", response_model=list[CeligoFlowSummaryOut])
async def list_integration_flows(
    integration_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Flows under one integration, each with its OPEN error/signature counts
    (one GROUP BY query for the whole list, not N+1 -- see
    `CeligoFlowSummaryOut`'s docstring). The mockup leads with signature count
    ("3 root causes") and shows raw error count secondary; both are returned
    so the frontend never has to make a second call per flow to get either."""
    integration = (
        await db.execute(
            select(CeligoIntegration).where(
                CeligoIntegration.id == integration_id,
                CeligoIntegration.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found")

    flows = (
        (
            await db.execute(
                select(CeligoFlow)
                .where(
                    CeligoFlow.tenant_id == user.tenant_id,
                    CeligoFlow.integration_id == integration_id,
                )
                .order_by(CeligoFlow.name)
            )
        )
        .scalars()
        .all()
    )
    if not flows:
        return []

    flow_ids = [f.id for f in flows]
    counts_result = await db.execute(
        select(
            CeligoFlowError.flow_id,
            func.count().label("error_count"),
            func.count(distinct(CeligoFlowError.signature_id)).label("signature_count"),
        )
        .where(
            CeligoFlowError.tenant_id == user.tenant_id,
            CeligoFlowError.flow_id.in_(flow_ids),
            CeligoFlowError.resolved_at.is_(None),
            CeligoFlowError.purged_at.is_(None),
        )
        .group_by(CeligoFlowError.flow_id)
    )
    counts_by_flow: dict[uuid.UUID, tuple[int, int]] = {
        row.flow_id: (row.error_count, row.signature_count) for row in counts_result.all()
    }

    return [
        CeligoFlowSummaryOut(
            id=str(f.id),
            celigo_id=f.celigo_id,
            name=f.name,
            disabled=f.disabled,
            schedule=f.schedule,
            timezone=f.timezone,
            last_executed_at=f.last_executed_at,
            error_count=counts_by_flow.get(f.id, (0, 0))[0],
            signature_count=counts_by_flow.get(f.id, (0, 0))[1],
        )
        for f in flows
    ]


# ---------------------------------------------------------------------------
# GET /celigo/flows/{id}
# ---------------------------------------------------------------------------


@router.get("/flows/{flow_id}", response_model=CeligoFlowDetailOut)
async def get_flow_detail(
    flow_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """One flow's steps (ordered, generators then processors then
    router-branch processors -- `sequence` within each) plus every script
    attachment, nested onto the step it belongs to. Attachments with no owning
    step (a `routers[].script` ref -- belongs to the router, not a step) come
    back in `unassigned_attachments` instead of being dropped."""
    flow = (
        await db.execute(
            select(CeligoFlow).where(
                CeligoFlow.id == flow_id,
                CeligoFlow.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if flow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")

    steps = (
        (
            await db.execute(
                select(CeligoFlowStep)
                .where(
                    CeligoFlowStep.tenant_id == user.tenant_id,
                    CeligoFlowStep.flow_id == flow_id,
                )
                .order_by(CeligoFlowStep.sequence)
            )
        )
        .scalars()
        .all()
    )
    attachments = (
        (
            await db.execute(
                select(CeligoScriptAttachment).where(
                    CeligoScriptAttachment.tenant_id == user.tenant_id,
                    CeligoScriptAttachment.flow_id == flow_id,
                )
            )
        )
        .scalars()
        .all()
    )

    attachments_by_step: dict[uuid.UUID, list[CeligoAttachmentOut]] = defaultdict(list)
    unassigned: list[CeligoAttachmentOut] = []
    for a in attachments:
        out = CeligoAttachmentOut(
            id=str(a.id),
            flow_id=str(a.flow_id),
            flow_step_id=str(a.flow_step_id) if a.flow_step_id else None,
            script_id=str(a.script_id) if a.script_id else None,
            script_celigo_id=a.script_celigo_id,
            function_name=a.function_name,
            json_path=a.json_path,
            site_type=a.site_type,
        )
        if a.flow_step_id is not None:
            attachments_by_step[a.flow_step_id].append(out)
        else:
            unassigned.append(out)

    step_outs = [
        CeligoFlowStepOut(
            id=str(s.id),
            celigo_id=s.celigo_id,
            role=s.role,
            router_id=s.router_id,
            branch_id=s.branch_id,
            branch_key=s.branch_key,
            sequence=s.sequence,
            adaptor_type=s.adaptor_type,
            connection_celigo_id=s.connection_celigo_id,
            filter_json=s.filter_json,
            mapping_json=s.mapping_json,
            proceed_on_failure=s.proceed_on_failure,
            skip_retries=s.skip_retries,
            attachments=attachments_by_step.get(s.id, []),
        )
        for s in steps
    ]

    return CeligoFlowDetailOut(
        id=str(flow.id),
        integration_id=str(flow.integration_id),
        celigo_id=flow.celigo_id,
        name=flow.name,
        disabled=flow.disabled,
        schedule=flow.schedule,
        timezone=flow.timezone,
        last_executed_at=flow.last_executed_at,
        source_id=flow.source_id,
        ai_description_summary=flow.ai_description_summary,
        ai_description_detailed=flow.ai_description_detailed,
        celigo_last_modified=flow.celigo_last_modified,
        steps=step_outs,
        unassigned_attachments=unassigned,
    )


# ---------------------------------------------------------------------------
# GET /celigo/scripts/{id}
# ---------------------------------------------------------------------------


@router.get("/scripts/{script_id}", response_model=CeligoScriptOut)
async def get_script_detail(
    script_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """The script the caller asked for, plus its whole clone family's
    attachment sites collapsed into one `used_by` list -- reuses
    `list_logical_scripts` (Task 5) for the family grouping rather than
    reimplementing the clone-dedup rule (see `CeligoScriptOut`'s docstring for
    why `content` itself still comes from THIS row, not the family's
    "representative")."""
    script = (
        await db.execute(
            select(CeligoScript).where(
                CeligoScript.id == script_id,
                CeligoScript.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if script is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Script not found")

    logical_scripts = await list_logical_scripts(
        db, tenant_id=user.tenant_id, connection_id=script.celigo_connection_id
    )
    group = next((g for g in logical_scripts if g.dedup_key == script.dedup_key), None)
    # Defensive only -- `script` itself is a member of its own dedup group by
    # construction, so `group` is never actually None. Falls back to a
    # single-member view rather than a 500 if that invariant is ever violated.
    celigo_ids = group.celigo_ids if group is not None else (script.celigo_id,)
    attachment_count = group.attachment_count if group is not None else 0
    content_diverged = group.content_diverged if group is not None else False

    sites_result = await db.execute(
        select(
            CeligoScriptAttachment,
            CeligoFlow.name.label("flow_name"),
            CeligoFlowStep.role.label("step_role"),
            CeligoFlowStep.adaptor_type.label("step_adaptor_type"),
        )
        .join(CeligoFlow, CeligoFlow.id == CeligoScriptAttachment.flow_id)
        .outerjoin(CeligoFlowStep, CeligoFlowStep.id == CeligoScriptAttachment.flow_step_id)
        .where(
            CeligoScriptAttachment.tenant_id == user.tenant_id,
            CeligoScriptAttachment.celigo_connection_id == script.celigo_connection_id,
            CeligoScriptAttachment.script_celigo_id.in_(celigo_ids),
        )
        .order_by(CeligoFlow.name, CeligoScriptAttachment.json_path)
    )

    used_by = [
        CeligoScriptAttachmentSiteOut(
            flow_id=str(attachment.flow_id),
            flow_name=flow_name,
            flow_step_id=str(attachment.flow_step_id) if attachment.flow_step_id else None,
            flow_step_role=step_role,
            flow_step_adaptor_type=step_adaptor_type,
            script_celigo_id=attachment.script_celigo_id,
            json_path=attachment.json_path,
            function_name=attachment.function_name,
            site_type=attachment.site_type,
        )
        for attachment, flow_name, step_role, step_adaptor_type in sites_result.all()
    ]

    return CeligoScriptOut(
        id=str(script.id),
        dedup_key=script.dedup_key,
        name=script.name,
        content=script.content,
        content_hash=script.content_hash,
        copies_count=len(celigo_ids),
        attachment_count=attachment_count,
        content_diverged=content_diverged,
        used_by=used_by,
    )


# ---------------------------------------------------------------------------
# GET /celigo/errors?signature=...
# ---------------------------------------------------------------------------


@router.get("/errors", response_model=CeligoErrorsResponse)
async def get_errors_for_signature(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    _flag: Annotated[User, Depends(require_feature("celigo"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    signature: uuid.UUID = Query(..., description="celigo_error_signatures.id"),
    limit: int = Query(100, ge=1, le=500, description="Max errors returned, most recent first"),
):
    """One error signature (a normalized root cause) plus its raw occurrences.

    `signature` identifies the row by its OWN local id -- the same
    id-based-navigation shape as `/celigo/flows/{id}` and `/celigo/scripts/{id}`
    (a value the caller only ever has because a prior response handed it back),
    not the `fingerprint` hash, which is an internal computed value never
    meant as an external lookup key."""
    sig = (
        await db.execute(
            select(CeligoErrorSignature).where(
                CeligoErrorSignature.id == signature,
                CeligoErrorSignature.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if sig is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Error signature not found")

    errors = (
        (
            await db.execute(
                select(CeligoFlowError)
                .where(
                    CeligoFlowError.tenant_id == user.tenant_id,
                    CeligoFlowError.signature_id == signature,
                )
                .order_by(CeligoFlowError.occurred_at.desc().nullslast(), CeligoFlowError.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return CeligoErrorsResponse(
        signature=CeligoErrorSignatureOut(
            id=str(sig.id),
            fingerprint=sig.fingerprint,
            source=sig.source,
            code=sig.code,
            sample_message=sig.sample_message,
            occurrence_count=sig.occurrence_count,
            first_seen=sig.first_seen,
            last_seen=sig.last_seen,
        ),
        errors=[
            CeligoErrorOut(
                id=str(e.id),
                celigo_id=e.celigo_id,
                flow_id=str(e.flow_id) if e.flow_id else None,
                flow_step_id=str(e.flow_step_id) if e.flow_step_id else None,
                trace_key=e.trace_key,
                source=e.source,
                code=e.code,
                message=e.message,
                occurred_at=e.occurred_at,
                purge_at=e.purge_at,
                resolved_at=e.resolved_at,
                purged_at=e.purged_at,
                retriable=e.retriable,
            )
            for e in errors
        ],
    )
