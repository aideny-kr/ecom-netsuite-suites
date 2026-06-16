"""CRUD for the tenant memory graph (concepts / edges / links).

All operations are tenant-scoped — every query carries
`.where(Model.tenant_id == tenant_id)` (defense-in-depth on top of RLS). Mutations
flush but do NOT commit; the caller (endpoint) commits and audit-logs, per the
FastAPI/SQLAlchemy convention (mirrors learned_rule_service).

Soft-reject flips `review_state` to 'rejected' (never `db.delete`). Merge repoints
the loser concepts' links + edges to the survivor and marks losers
`review_state='merged'` + `merged_into_id`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
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
    # A merged concept is a tombstone — the merge already moved its evidence
    # (links + edges) to the survivor. Any update would resurrect a dead node and
    # corrupt the trust spine, so block it outright.
    if concept.review_state == "merged":
        raise ValueError("cannot modify a merged concept")
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


async def merge_concepts(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    survivor_id: uuid.UUID,
    merged_ids: list[uuid.UUID],
) -> TenantMemoryConcept | None:
    """Merge `merged_ids` into `survivor_id` (all tenant-scoped).

    Repoints the losers' links + edges (both endpoints) to the survivor, then marks
    each loser `review_state='merged'` + `merged_into_id=survivor_id`. Cross-tenant or
    unknown loser ids are silently skipped (the survivor must belong to the tenant or
    this returns None). Flushes; the endpoint commits.
    """
    survivor = await get_concept(db, tenant_id, survivor_id)
    if survivor is None:
        return None

    # Only operate on losers that actually belong to this tenant (and aren't the survivor).
    loser_rows = await db.execute(
        select(TenantMemoryConcept).where(
            TenantMemoryConcept.tenant_id == tenant_id,
            TenantMemoryConcept.id.in_(merged_ids),
            TenantMemoryConcept.id != survivor_id,
        )
    )
    losers = list(loser_rows.scalars().all())
    if not losers:
        return survivor

    loser_id_list = [c.id for c in losers]

    # Repoint evidence links to the survivor.
    await db.execute(
        update(TenantMemoryLink)
        .where(
            TenantMemoryLink.tenant_id == tenant_id,
            TenantMemoryLink.concept_id.in_(loser_id_list),
        )
        .values(concept_id=survivor_id)
    )
    # Repoint edge endpoints (source + target) to the survivor.
    await db.execute(
        update(TenantMemoryEdge)
        .where(
            TenantMemoryEdge.tenant_id == tenant_id,
            TenantMemoryEdge.source_concept_id.in_(loser_id_list),
        )
        .values(source_concept_id=survivor_id)
    )
    await db.execute(
        update(TenantMemoryEdge)
        .where(
            TenantMemoryEdge.tenant_id == tenant_id,
            TenantMemoryEdge.target_concept_id.in_(loser_id_list),
        )
        .values(target_concept_id=survivor_id)
    )
    # Mark losers merged.
    for loser in losers:
        loser.review_state = "merged"
        loser.merged_into_id = survivor_id

    await db.flush()
    return survivor
