"""Tenant memory graph management API — view/confirm/edit/reject concepts.

The graph is a self-serve, plain-English overlay over the tenant's existing
learning rows. Only `review_state='confirmed'` concepts reach the agent prompt
(see the read-loop), so this surface is the trust gate: admins (`memory.manage`)
confirm/edit/reject; readers (any authed user) can view.

Reads use `get_current_user` (view-only); every mutation requires `memory.manage`,
audit-logs BEFORE the commit, and is tenant-scoped (defense-in-depth on top of RLS).
`from_attributes` does NOT coerce UUID→str, so the explicit `_*_to_response()`
helpers `str()` every UUID and `float()` the Numeric confidence.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_permission
from app.models.tenant_memory_concept import TenantMemoryConcept
from app.models.tenant_memory_edge import TenantMemoryEdge
from app.models.tenant_memory_link import TenantMemoryLink
from app.models.user import User
from app.schemas.tenant_memory import (
    MemoryConceptDetail,
    MemoryConceptResponse,
    MemoryConceptUpdate,
    MemoryEdgeResponse,
    MemoryGraphResponse,
    MemoryLinkResponse,
)
from app.services import audit_service, tenant_memory_service
from app.workers.celery_app import celery_app

router = APIRouter(prefix="/tenant-memory", tags=["tenant-memory"])

_Reader = Annotated[User, Depends(get_current_user)]
_Manager = Annotated[User, Depends(require_permission("memory.manage"))]
_Backfiller = Annotated[User, Depends(require_permission("tenant.manage"))]
_Db = Annotated[AsyncSession, Depends(get_db)]


def _parse_uuid(value: str) -> uuid.UUID:
    """Parse a path UUID, returning 404 (not 500) on a malformed id."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Concept not found")


def _concept_to_response(c: TenantMemoryConcept) -> MemoryConceptResponse:
    return MemoryConceptResponse(
        id=str(c.id),
        tenant_id=str(c.tenant_id),
        name=c.name,
        summary=c.summary,
        concept_type=c.concept_type,
        review_state=c.review_state,
        confidence=float(c.confidence) if c.confidence is not None else None,
        confirmed_by=str(c.confirmed_by) if c.confirmed_by else None,
        use_count=c.use_count,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _edge_to_response(e: TenantMemoryEdge) -> MemoryEdgeResponse:
    return MemoryEdgeResponse(
        id=str(e.id),
        tenant_id=str(e.tenant_id),
        source_concept_id=str(e.source_concept_id),
        target_concept_id=str(e.target_concept_id),
        relation=e.relation,
        review_state=e.review_state,
        created_at=e.created_at,
        updated_at=e.updated_at,
    )


def _link_to_response(link: TenantMemoryLink) -> MemoryLinkResponse:
    return MemoryLinkResponse(
        id=str(link.id),
        tenant_id=str(link.tenant_id),
        concept_id=str(link.concept_id),
        source_table=link.source_table,
        source_id=str(link.source_id),
        created_at=link.created_at,
    )


@router.get("", response_model=MemoryGraphResponse)
async def get_memory_graph(user: _Reader, db: _Db, review_state: str | None = None):
    concepts = await tenant_memory_service.list_concepts(db, user.tenant_id, review_state=review_state)
    edges = await tenant_memory_service.list_edges(db, user.tenant_id)
    return MemoryGraphResponse(
        concepts=[_concept_to_response(c) for c in concepts],
        edges=[_edge_to_response(e) for e in edges],
    )


@router.get("/concepts/{concept_id}", response_model=MemoryConceptDetail)
async def get_concept_detail(concept_id: str, user: _Reader, db: _Db):
    cid = _parse_uuid(concept_id)
    concept = await tenant_memory_service.get_concept(db, user.tenant_id, cid)
    if concept is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Concept not found")
    links = await tenant_memory_service.get_concept_links(db, user.tenant_id, cid)
    base = _concept_to_response(concept)
    return MemoryConceptDetail(
        **base.model_dump(),
        links=[_link_to_response(link) for link in links],
    )


@router.patch("/concepts/{concept_id}", response_model=MemoryConceptResponse)
async def update_concept(concept_id: str, request: MemoryConceptUpdate, user: _Manager, db: _Db):
    cid = _parse_uuid(concept_id)
    try:
        concept = await tenant_memory_service.update_concept(
            db,
            user.tenant_id,
            cid,
            name=request.name,
            summary=request.summary,
            concept_type=request.concept_type,
            review_state=request.review_state,
            confirmed_by=user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    if concept is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Concept not found")
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="tenant_memory",
        action="tenant_memory.concept.update",
        actor_id=user.id,
        resource_type="tenant_memory_concept",
        resource_id=str(cid),
        payload=request.model_dump(exclude_none=True),
    )
    await db.commit()
    await db.refresh(concept)
    return _concept_to_response(concept)


@router.delete("/concepts/{concept_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_concept(concept_id: str, user: _Manager, db: _Db):
    """Soft-delete: flips review_state to 'rejected' (the row is NOT removed)."""
    cid = _parse_uuid(concept_id)
    ok = await tenant_memory_service.soft_reject_concept(db, user.tenant_id, cid)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Concept not found")
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="tenant_memory",
        action="tenant_memory.concept.reject",
        actor_id=user.id,
        resource_type="tenant_memory_concept",
        resource_id=str(cid),
    )
    await db.commit()
    return None


@router.post("/backfill", status_code=status.HTTP_202_ACCEPTED)
async def trigger_backfill(user: _Backfiller, db: _Db):
    """Dispatch the async backfill that distills existing learning rows into
    pending memory concepts. Requires ``tenant.manage`` (it touches the tenant's
    whole learning corpus). Returns immediately with the Celery task id.
    """
    result = celery_app.send_task(
        "tasks.tenant_memory_extract_backfill",
        kwargs={"tenant_id": str(user.tenant_id)},
        queue="sync",
    )
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="tenant_memory",
        action="tenant_memory.backfill.trigger",
        actor_id=user.id,
        resource_type="tenant_memory_backfill",
        resource_id=str(result.id),
        payload={"celery_task_id": str(result.id)},
    )
    await db.commit()
    return {"celery_task_id": str(result.id), "status": "queued"}
