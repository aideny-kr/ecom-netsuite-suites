"""Tenant-scoped semantic retrieval from drive_chunks via pgvector cosine distance."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.drive import DriveChunk
from app.services.chat.embeddings import embed_query

logger = logging.getLogger(__name__)

_DEFAULT_TOP_K = 6
_MIN_SIMILARITY = 0.50

# pgvector ≥ 0.8 — preserves recall when the HNSW approximate top-K candidates
# are dominated by other tenants' vectors. Without this, `tenant_id` filtering
# happens AFTER the index returns its top-K, so a tenant with a small chunk
# count can get fewer (or zero) results when other tenants' chunks dominate
# the global ANN neighborhood. strict_order keeps results sorted by distance.
_HNSW_ITERATIVE_SCAN = "SET LOCAL hnsw.iterative_scan = strict_order"


async def retrieve_drive_chunks(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query_text: str,
    top_k: int = _DEFAULT_TOP_K,
    min_similarity: float = _MIN_SIMILARITY,
) -> list[dict]:
    """Return top-K tenant-scoped Drive chunks most similar to query_text.

    Each result: {content, source_name, web_view_link, similarity}.
    Returns [] when embedding service is unavailable (embed_query -> None).
    """
    embedding = await embed_query(query_text)
    if embedding is None:
        return []

    # SET LOCAL applies for the rest of the current transaction. Issue it
    # before the cosine search so HNSW iterative scan kicks in when needed.
    await db.execute(text(_HNSW_ITERATIVE_SCAN))

    max_distance = 1.0 - min_similarity
    stmt = (
        select(
            DriveChunk,
            DriveChunk.embedding.cosine_distance(embedding).label("distance"),
        )
        .where(
            DriveChunk.tenant_id == tenant_id,
            DriveChunk.embedding.isnot(None),
            DriveChunk.embedding.cosine_distance(embedding) <= max_distance,
        )
        .order_by("distance")
        .limit(top_k)
    )
    result = await db.execute(stmt)
    rows = result.all()
    out: list[dict] = []
    for row in rows:
        chunk = row[0]
        distance = float(row[1])
        meta = chunk.metadata_ or {}
        out.append(
            {
                "content": chunk.content,
                "source_name": meta.get("source_name") or "Drive File",
                "web_view_link": meta.get("web_view_link") or "",
                "similarity": 1.0 - distance,
            }
        )
    return out
