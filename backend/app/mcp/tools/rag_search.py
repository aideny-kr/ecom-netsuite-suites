"""RAG vector search exposed as an MCP tool.

Wraps the pgvector cosine similarity search so specialist agents can call
it as a tool rather than relying on upfront retrieval only.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import DocChunk
from app.services.chat.embeddings import embed_query

logger = logging.getLogger(__name__)

SYSTEM_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


async def execute(params: dict[str, Any], context: dict[str, Any] | None = None, **kwargs: Any) -> dict:
    """Search the RAG document store via vector similarity.

    Parameters
    ----------
    params.query : str
        Natural language search query.
    params.top_k : int, optional
        Maximum number of results (default 10).
    params.source_filter : str, optional
        Prefix filter on source_path (e.g. "netsuite_metadata/").
    """
    query_text = params.get("query", "")
    if not query_text:
        return {"error": "query parameter is required"}

    top_k = min(int(params.get("top_k", 10)), 30)  # Cap at 30
    source_filter = params.get("source_filter")

    tenant_id_str = (context or {}).get("tenant_id", "")
    db: AsyncSession | None = (context or {}).get("db")
    if db is None:
        return {"error": "Database session not available"}

    try:
        tenant_id = uuid.UUID(tenant_id_str) if tenant_id_str else None
    except (ValueError, TypeError):
        return {"error": f"Invalid tenant_id: {tenant_id_str}"}

    try:
        query_embedding = await embed_query(query_text)
        if query_embedding is None:
            # Embedding service not configured — fall back to keyword search
            return await _keyword_search(db, tenant_id, query_text, top_k, source_filter)

        # Build base query
        stmt = (
            select(
                DocChunk,
                DocChunk.embedding.cosine_distance(query_embedding).label("distance"),
            )
            .order_by("distance")
            .limit(top_k)
        )

        # Tenant scoping: include tenant-specific + system/platform docs
        if tenant_id:
            stmt = stmt.where((DocChunk.tenant_id == tenant_id) | (DocChunk.tenant_id == SYSTEM_TENANT_ID))
        else:
            stmt = stmt.where(DocChunk.tenant_id == SYSTEM_TENANT_ID)

        # Optional source path filter
        if source_filter:
            stmt = stmt.where(DocChunk.source_path.ilike(f"{source_filter}%"))

        result = await db.execute(stmt)
        rows = result.all()

        results = []
        for row in rows:
            chunk = row[0]
            distance = float(row[1]) if row[1] is not None else 1.0
            similarity = round(1.0 - distance, 4)
            results.append(
                {
                    "title": chunk.title,
                    "content": chunk.content[:2000],  # Truncate for token efficiency
                    "source_path": chunk.source_path,
                    "similarity_score": similarity,
                }
            )

        return {"results": results, "count": len(results), "query": query_text}

    except Exception as exc:
        logger.warning("rag_search.execute failed", exc_info=True)
        return {"error": f"RAG search failed: {exc}"}


async def _keyword_search(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    query_text: str,
    top_k: int,
    source_filter: str | None,
) -> dict:
    """Fallback keyword search when embeddings are not available."""
    search_term = f"%{query_text[:100]}%"
    stmt = select(DocChunk).where(DocChunk.content.ilike(search_term)).limit(top_k)
    if tenant_id:
        stmt = stmt.where((DocChunk.tenant_id == tenant_id) | (DocChunk.tenant_id == SYSTEM_TENANT_ID))
    if source_filter:
        stmt = stmt.where(DocChunk.source_path.ilike(f"{source_filter}%"))

    result = await db.execute(stmt)
    chunks = result.scalars().all()

    results = [
        {
            "title": c.title,
            "content": c.content[:2000],
            "source_path": c.source_path,
            "similarity_score": None,
        }
        for c in chunks
    ]
    return {"results": results, "count": len(results), "query": query_text, "method": "keyword_fallback"}
