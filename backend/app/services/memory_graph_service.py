"""Retrieve confirmed tenant memory concepts for agent context injection.

The read-loop half of the tenant memory graph. Mirrors
``learned_rules_service.retrieve_learned_rules``: a small, dependency-free
async retriever the orchestrator calls during its concurrent context gather.

THE GATE: only ``review_state == 'confirmed'`` concepts are ever returned.
Pending concepts (awaiting human review) and rejected concepts (the customer
said "stop using this") are NEVER injected into the prompt. This is the trust
spine of the subsystem — a rejected concept must stop driving answers.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.tenant_memory_concept import TenantMemoryConcept

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def retrieve_confirmed_concepts(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    query_text: str | None = None,
    max_concepts: int = 10,
) -> list[dict]:
    """Retrieve confirmed memory concepts for a tenant.

    Only concepts with ``review_state == 'confirmed'`` are returned — this is
    the trust gate. ``pending``/``rejected``/``merged`` concepts are excluded by
    the SQL filter and therefore never reach the prompt.

    ``query_text`` is accepted for interface parity with the learned-rules
    retriever (and for a future relevance ranking), but v1 simply returns the
    most-recently-confirmed concepts up to ``max_concepts``.

    Returns a list of ``{"name": str, "summary": str}`` dicts.
    """
    result = await db.execute(
        select(TenantMemoryConcept)
        .where(
            TenantMemoryConcept.tenant_id == tenant_id,
            TenantMemoryConcept.review_state == "confirmed",
            # Belt-and-suspenders: a merge tombstone can NEVER be injected, even if
            # its review_state somehow reads 'confirmed' (its evidence already moved
            # to the survivor).
            TenantMemoryConcept.merged_into_id.is_(None),
        )
        .order_by(TenantMemoryConcept.last_used_at.desc().nullslast(), TenantMemoryConcept.created_at.desc())
        .limit(max_concepts)
    )
    concepts = result.scalars().all()

    if not concepts:
        return []

    print(
        f"[MEMORY_GRAPH_RETRIEVAL] tenant={str(tenant_id)[:8]} confirmed_count={len(concepts)}",
        flush=True,
    )

    return [{"name": c.name, "summary": c.summary} for c in concepts]
