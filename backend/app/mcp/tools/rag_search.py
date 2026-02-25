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

        # Vector search only works on docs WITH embeddings.
        # Many tenant docs may have NULL embeddings (e.g. rate-limited Voyage AI).
        # Strategy: run vector search for embedded docs, then supplement with
        # keyword search for unembedded tenant docs, deduplicating by source_path.

        # 1. Vector search (only finds docs with non-NULL embeddings)
        stmt = (
            select(
                DocChunk,
                DocChunk.embedding.cosine_distance(query_embedding).label("distance"),
            )
            .where(DocChunk.embedding.isnot(None))
            .order_by("distance")
            .limit(top_k)
        )

        if tenant_id:
            stmt = stmt.where((DocChunk.tenant_id == tenant_id) | (DocChunk.tenant_id == SYSTEM_TENANT_ID))
        else:
            stmt = stmt.where(DocChunk.tenant_id == SYSTEM_TENANT_ID)

        if source_filter:
            stmt = stmt.where(DocChunk.source_path.ilike(f"{source_filter}%"))

        result = await db.execute(stmt)
        rows = result.all()

        results = []
        seen_paths: set[str] = set()
        for row in rows:
            chunk = row[0]
            distance = float(row[1]) if row[1] is not None else 1.0
            similarity = round(1.0 - distance, 4)
            results.append(
                {
                    "title": chunk.title,
                    "content": chunk.content[:2000],
                    "source_path": chunk.source_path,
                    "similarity_score": similarity,
                }
            )
            seen_paths.add(chunk.source_path)

        # 2. Always supplement with keyword search for tenant docs
        # (many tenant docs have NULL embeddings and are invisible to vector search)
        if tenant_id:
            kw_result = await _keyword_search(
                db, tenant_id, query_text, top_k, source_filter
            )
            for kr in kw_result.get("results", []):
                if kr["source_path"] not in seen_paths:
                    results.append(kr)
                    seen_paths.add(kr["source_path"])

        # Prioritise keyword-matched tenant docs over low-similarity system docs
        # by sorting: tenant keyword hits first, then vector similarity
        def _sort_key(r: dict) -> tuple:
            kw_hits = r.get("keyword_hits", 0)
            sim = r.get("similarity_score") or 0
            return (-kw_hits, -sim)

        results.sort(key=_sort_key)
        results = results[:top_k]

        return {"results": results, "count": len(results), "query": query_text}

    except Exception:
        logger.warning("rag_search.execute failed", exc_info=True)
        # Return empty results instead of error so agents don't waste steps retrying
        return {
            "results": [],
            "count": 0,
            "query": query_text,
            "note": "Search temporarily unavailable, proceed without documentation context.",
        }


async def _keyword_search(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    query_text: str,
    top_k: int,
    source_filter: str | None,
) -> dict:
    """Fallback keyword search when embeddings are not available.

    Splits the query into individual words and matches documents containing
    ANY of them (OR logic), then ranks by number of keyword hits.
    """
    from sqlalchemy import case, or_

    # Extract meaningful keywords (skip very short words)
    words = [w.strip().lower() for w in query_text.split() if len(w.strip()) >= 3]
    if not words:
        words = [query_text.strip().lower()]

    # Build OR conditions: content ILIKE '%word%' for each keyword
    conditions = [DocChunk.content.ilike(f"%{w[:50]}%") for w in words[:10]]

    # Score = number of keywords found in each doc (for ranking)
    hit_score = sum(
        case((DocChunk.content.ilike(f"%{w[:50]}%"), 1), else_=0)
        for w in words[:10]
    )

    stmt = (
        select(DocChunk, hit_score.label("score"))
        .where(or_(*conditions))
        .order_by(hit_score.desc())
        .limit(top_k)
    )
    if tenant_id:
        stmt = stmt.where(
            (DocChunk.tenant_id == tenant_id) | (DocChunk.tenant_id == SYSTEM_TENANT_ID)
        )
    if source_filter:
        stmt = stmt.where(DocChunk.source_path.ilike(f"{source_filter}%"))

    result = await db.execute(stmt)
    rows = result.all()

    results = [
        {
            "title": row[0].title,
            "content": row[0].content[:2000],
            "source_path": row[0].source_path,
            "similarity_score": None,
            "keyword_hits": int(row[1]) if row[1] else 0,
        }
        for row in rows
    ]
    return {"results": results, "count": len(results), "query": query_text, "method": "keyword_fallback"}
