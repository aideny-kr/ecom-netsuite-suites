# backend/app/services/metrics/metric_resolver.py
"""Resolve an NL phrase or key to metric rows across tenant ∪ SYSTEM (tenant wins by key)."""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.services.chat.domain_knowledge import embed_domain_query


async def resolve_metrics(
    db: AsyncSession, *, tenant_id: uuid.UUID, query: str, top_k: int = 5
) -> list[MetricDefinition]:
    # Application-level visibility filter (SYSTEM defaults + this tenant). This is the real
    # isolation guarantee; the RLS policy is defense-in-depth (owner role bypasses non-FORCED RLS).
    visible = or_(
        MetricDefinition.tenant_id == tenant_id,
        MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
    )
    embedding = await embed_domain_query(query)
    rows: list[MetricDefinition]
    if embedding is not None:
        stmt = (
            select(MetricDefinition)
            .where(visible, MetricDefinition.status == "active", MetricDefinition.intent_embedding.isnot(None))
            .order_by(MetricDefinition.intent_embedding.cosine_distance(embedding))
            .limit(top_k * 3)
        )
        rows = list((await db.execute(stmt)).scalars().all())
    else:
        rows = []

    # Always union exact key + synonym matches (covers unembedded rows and exact asks).
    q_lower = query.strip().lower()
    kw_stmt = select(MetricDefinition).where(
        visible,
        MetricDefinition.status == "active",
        or_(
            MetricDefinition.key == q_lower,
            MetricDefinition.synonyms.any(q_lower),
            MetricDefinition.display_name.ilike(f"%{query.strip()}%"),
        ),
    )
    for r in (await db.execute(kw_stmt)).scalars().all():
        if r.id not in {x.id for x in rows}:
            rows.append(r)

    # SYSTEM-default metrics are visible to every tenant regardless of query match
    # (mirrors the RLS policy). Always include them as a baseline so a tenant can
    # discover the blessed defaults even when the NL phrase only hit a tenant row.
    sys_stmt = select(MetricDefinition).where(
        MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
        MetricDefinition.status == "active",
    )
    for r in (await db.execute(sys_stmt)).scalars().all():
        if r.id not in {x.id for x in rows}:
            rows.append(r)

    # Tenant override wins by key; cap at top_k.
    by_key: dict[str, MetricDefinition] = {}
    for r in rows:
        existing = by_key.get(r.key)
        if existing is None or (r.tenant_id == tenant_id and existing.tenant_id == SYSTEM_TENANT_ID):
            by_key[r.key] = r
    return list(by_key.values())[:top_k]
