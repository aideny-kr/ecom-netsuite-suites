"""Embedding service for doc_chunks (RAG).

Primary: OpenAI text-embedding-3-small (dimensions=1024 to match DocChunk Vector).
Fallback: Voyage AI (if OpenAI key not configured).
Returns None if neither is available.
"""

from __future__ import annotations

import logging

import openai

from app.core.config import settings

logger = logging.getLogger(__name__)

# DocChunk.embedding is Vector(1024), so we request 1024-dim from OpenAI.
_DOC_CHUNK_DIMENSIONS = 1024

_openai_client: openai.AsyncOpenAI | None = None
_voyage_client = None  # lazy import


def _get_openai_client() -> openai.AsyncOpenAI | None:
    global _openai_client
    if _openai_client is None:
        if not settings.OPENAI_EMBEDDING_API_KEY:
            return None
        _openai_client = openai.AsyncOpenAI(api_key=settings.OPENAI_EMBEDDING_API_KEY)
    return _openai_client


def _get_voyage_client():
    global _voyage_client
    if _voyage_client is None:
        if not settings.VOYAGE_API_KEY:
            return None
        import voyageai

        _voyage_client = voyageai.AsyncClient(api_key=settings.VOYAGE_API_KEY)
    return _voyage_client


_MAX_EMBED_CHARS = 24000  # ~6000 tokens — stay under OpenAI 8192 limit


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Batch embed texts. Tries OpenAI first, then Voyage AI fallback.

    Returns list of 1024-dim vectors, or None if unavailable.
    Truncates inputs exceeding the model's context window.
    """
    # Truncate oversized inputs
    texts = [t[:_MAX_EMBED_CHARS] if len(t) > _MAX_EMBED_CHARS else t for t in texts]

    # Try OpenAI first
    client = _get_openai_client()
    if client is not None:
        try:
            response = await client.embeddings.create(
                input=texts,
                model=settings.OPENAI_EMBEDDING_MODEL,
                dimensions=_DOC_CHUNK_DIMENSIONS,
            )
            return [item.embedding for item in response.data]
        except Exception:
            logger.warning("OpenAI embedding failed for doc_chunks", exc_info=True)

    # Fallback to Voyage AI
    voyage = _get_voyage_client()
    if voyage is not None:
        try:
            result = await voyage.embed(texts, model=settings.VOYAGE_EMBED_MODEL)
            return result.embeddings
        except Exception:
            logger.warning("Voyage AI embedding failed", exc_info=True)

    return None


async def embed_query(text: str) -> list[float] | None:
    """Embed a single query text. Returns None if not configured or unavailable."""
    embeddings = await embed_texts([text])
    if embeddings is None:
        return None
    return embeddings[0]
