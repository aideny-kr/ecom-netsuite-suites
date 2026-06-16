"""CRUD for the tenant memory graph (concepts / edges / links).

All operations are tenant-scoped — every query carries
`.where(Model.tenant_id == tenant_id)` (defense-in-depth on top of RLS). Mutations
flush but do NOT commit; the caller (endpoint) commits and audit-logs, per the
FastAPI/SQLAlchemy convention (mirrors learned_rule_service).

Soft-reject flips `review_state` to 'rejected' (never `db.delete`).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_memory_concept import TenantMemoryConcept
from app.models.tenant_memory_edge import TenantMemoryEdge
from app.models.tenant_memory_link import TenantMemoryLink


async def list_concepts(
    db: AsyncSession, tenant_id: uuid.UUID, review_state: str | None = None
) -> list[TenantMemoryConcept]:
    """All concepts for a tenant (optionally filtered by review_state), newest first."""
    stmt = select(TenantMemoryConcept).where(TenantMemoryConcept.tenant_id == tenant_id)
    if review_state is not None:
        stmt = stmt.where(TenantMemoryConcept.review_state == review_state)
    stmt = stmt.order_by(TenantMemoryConcept.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_edges(db: AsyncSession, tenant_id: uuid.UUID) -> list[TenantMemoryEdge]:
    """All edges for a tenant, newest first."""
    result = await db.execute(
        select(TenantMemoryEdge)
        .where(TenantMemoryEdge.tenant_id == tenant_id)
        .order_by(TenantMemoryEdge.created_at.desc())
    )
    return list(result.scalars().all())


async def get_concept(db: AsyncSession, tenant_id: uuid.UUID, concept_id: uuid.UUID) -> TenantMemoryConcept | None:
    """Fetch a single concept, scoped to the tenant (None if missing or cross-tenant)."""
    result = await db.execute(
        select(TenantMemoryConcept).where(
            TenantMemoryConcept.id == concept_id,
            TenantMemoryConcept.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def get_concept_links(db: AsyncSession, tenant_id: uuid.UUID, concept_id: uuid.UUID) -> list[TenantMemoryLink]:
    """Evidence links for a concept, tenant-scoped."""
    result = await db.execute(
        select(TenantMemoryLink)
        .where(
            TenantMemoryLink.tenant_id == tenant_id,
            TenantMemoryLink.concept_id == concept_id,
        )
        .order_by(TenantMemoryLink.created_at.desc())
    )
    return list(result.scalars().all())


async def update_concept(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    concept_id: uuid.UUID,
    *,
    name: str | None = None,
    summary: str | None = None,
    concept_type: str | None = None,
    review_state: str | None = None,
    confirmed_by: uuid.UUID | None = None,
) -> TenantMemoryConcept | None:
    """Patch the provided fields in place. Returns None if not found for this tenant.

    When `review_state` transitions to 'confirmed', stamp `confirmed_by` (the
    confirming actor) so the trust spine records who blessed the concept.
    """
    concept = await get_concept(db, tenant_id, concept_id)
    if concept is None:
        return None
    if name is not None:
        concept.name = name
    if summary is not None:
        concept.summary = summary
    if concept_type is not None:
        concept.concept_type = concept_type
    if review_state is not None:
        concept.review_state = review_state
        if review_state == "confirmed" and confirmed_by is not None:
            concept.confirmed_by = confirmed_by
    await db.flush()
    return concept


async def soft_reject_concept(db: AsyncSession, tenant_id: uuid.UUID, concept_id: uuid.UUID) -> bool:
    """Soft-delete: flip review_state to 'rejected' (NOT db.delete). False if not found."""
    concept = await get_concept(db, tenant_id, concept_id)
    if concept is None:
        return False
    concept.review_state = "rejected"
    await db.flush()
    return True
