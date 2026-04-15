"""Domain knowledge embedding and retrieval service.

Uses OpenAI text-embedding-3-small (1536-dim) for embedding curated
domain knowledge chunks. At query time, retrieves the Top K most
relevant chunks via pgvector cosine similarity, with keyword fallback.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.domain_knowledge import DomainKnowledgeChunk
from app.services.chat.embeddings import _get_openai_client

logger = logging.getLogger(__name__)


async def embed_domain_texts(texts: list[str]) -> list[list[float]] | None:
    """Batch embed texts using OpenAI. Returns None if not configured."""
    client = _get_openai_client()
    if client is None:
        return None
    try:
        response = await client.embeddings.create(
            input=texts,
            model=settings.OPENAI_EMBEDDING_MODEL,
            dimensions=settings.OPENAI_EMBEDDING_DIMENSIONS,
        )
        return [item.embedding for item in response.data]
    except Exception:
        logger.warning("OpenAI embedding failed", exc_info=True)
        return None


async def embed_domain_query(text: str) -> list[float] | None:
    """Embed a single query text. Returns None if not configured."""
    embeddings = await embed_domain_texts([text])
    if embeddings is None:
        return None
    return embeddings[0]


async def retrieve_domain_knowledge(
    db: AsyncSession,
    query_text: str,
    top_k: int | None = None,
    partition_ids: list[str] | None = None,
) -> list[dict]:
    """Retrieve top-K domain knowledge chunks for a query.

    Tries vector similarity first, falls back to keyword search
    if embeddings are unavailable.

    Args:
        db: Async DB session.
        query_text: Natural language query to match against.
        top_k: Maximum number of chunks to return.
        partition_ids: When provided and non-empty, restrict results to chunks
            whose ``partition_id`` is in this list. Enables per-agent RAG
            isolation without multiple round-trips (single SQL IN clause).
    """
    if top_k is None:
        top_k = settings.DOMAIN_KNOWLEDGE_TOP_K

    try:
        query_embedding = await embed_domain_query(query_text)
        if query_embedding is None:
            return await _keyword_domain_search(db, query_text, top_k, partition_ids=partition_ids)

        # Push similarity threshold into the SQL WHERE clause so Postgres
        # skips irrelevant rows at the index level (cheaper than fetching
        # 18 candidates then filtering in Python).
        min_sim = settings.DOMAIN_KNOWLEDGE_MIN_SIMILARITY
        max_distance = 1.0 - min_sim  # cosine_distance = 1 - similarity

        where_clauses = [
            DomainKnowledgeChunk.is_deprecated.is_(False),
            DomainKnowledgeChunk.embedding.isnot(None),
            DomainKnowledgeChunk.embedding.cosine_distance(query_embedding) <= max_distance,
        ]
        if partition_ids:
            where_clauses.append(DomainKnowledgeChunk.partition_id.in_(partition_ids))

        stmt = (
            select(
                DomainKnowledgeChunk,
                DomainKnowledgeChunk.embedding.cosine_distance(query_embedding).label("distance"),
            )
            .where(*where_clauses)
            .order_by("distance")
        )

        # Fetch more candidates than needed for re-ranking
        fetch_k = max(top_k * 3, 10)
        stmt = stmt.limit(fetch_k)

        result = await db.execute(stmt)
        rows = result.all()

        if not rows:
            return await _keyword_domain_search(db, query_text, top_k, partition_ids=partition_ids)

        # Keyword boosting: re-rank by combining vector similarity with keyword overlap
        query_keywords = set(re.findall(r"\b\w{3,}\b", query_text.lower()))
        scored = []
        for row in rows:
            chunk = row[0]
            distance = float(row[1]) if row[1] is not None else 1.0
            vector_sim = 1.0 - distance
            chunk_lower = chunk.raw_text.lower()
            keyword_hits = sum(1 for kw in query_keywords if kw in chunk_lower)
            # Combine: vector similarity + keyword boost (0.1 per hit)
            adjusted_score = vector_sim + (keyword_hits * 0.1)
            scored.append((chunk, adjusted_score, vector_sim, keyword_hits))

        # Sort by adjusted score descending, take top_k
        scored.sort(key=lambda x: x[1], reverse=True)

        # Threshold already applied in SQL WHERE clause — no Python filter needed.
        returned = scored[:top_k]

        # Instrumentation: log similarity scores so we can see whether
        # retrieved chunks are actually relevant or just "least irrelevant
        # top K". This is read by the benchmark harness.
        if returned:
            sims = [round(item[2], 3) for item in returned]
            kw_hits = [item[3] for item in returned]
            print(
                f"[DOMAIN_KNOWLEDGE_RETRIEVAL] "
                f'q="{query_text[:80]}" '
                f"returned={len(returned)} similarities={sims} keyword_hits={kw_hits}",
                flush=True,
            )
        else:
            print(
                f'[DOMAIN_KNOWLEDGE_RETRIEVAL] q="{query_text[:80]}" returned=0',
                flush=True,
            )

        return [
            {
                "raw_text": item[0].raw_text,
                "source_uri": item[0].source_uri,
                "similarity": round(item[2], 4),
                "keyword_hits": item[3],
                "adjusted_score": round(item[1], 4),
                "topic_tags": item[0].topic_tags,
            }
            for item in returned
        ]

    except Exception:
        logger.warning("Domain knowledge retrieval failed, trying keyword fallback", exc_info=True)
        try:
            return await _keyword_domain_search(db, query_text, top_k, partition_ids=partition_ids)
        except Exception:
            logger.warning("Keyword fallback also failed", exc_info=True)
            return []


async def _keyword_domain_search(
    db: AsyncSession,
    query_text: str,
    top_k: int,
    partition_ids: list[str] | None = None,
) -> list[dict]:
    """OR-based keyword fallback when embeddings are unavailable."""
    words = [w.strip().lower() for w in query_text.split() if len(w.strip()) >= 3]
    if not words:
        words = [query_text.strip().lower()]

    # Limit to 10 keywords, 50 chars each
    conditions = [DomainKnowledgeChunk.raw_text.ilike(f"%{w[:50]}%") for w in words[:10]]

    hit_score = sum(case((DomainKnowledgeChunk.raw_text.ilike(f"%{w[:50]}%"), 1), else_=0) for w in words[:10])

    where_clauses = [
        DomainKnowledgeChunk.is_deprecated.is_(False),
        or_(*conditions),
    ]
    if partition_ids:
        where_clauses.append(DomainKnowledgeChunk.partition_id.in_(partition_ids))

    stmt = (
        select(DomainKnowledgeChunk, hit_score.label("score"))
        .where(*where_clauses)
        .order_by(hit_score.desc())
        .limit(top_k)
    )

    result = await db.execute(stmt)
    rows = result.all()

    return [
        {
            "raw_text": row[0].raw_text,
            "source_uri": row[0].source_uri,
            "similarity": None,
            "keyword_hits": int(row[1]) if row[1] else 0,
            "topic_tags": row[0].topic_tags,
        }
        for row in rows
    ]
