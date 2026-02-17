import voyageai

from app.core.config import settings

_client: voyageai.AsyncClient | None = None


def get_voyage_client() -> voyageai.AsyncClient | None:
    global _client
    if _client is None:
        if not settings.VOYAGE_API_KEY:
            return None
        _client = voyageai.AsyncClient(api_key=settings.VOYAGE_API_KEY)
    return _client


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Batch embed texts using Voyage AI. Returns None if not configured."""
    client = get_voyage_client()
    if client is None:
        return None
    result = await client.embed(texts, model=settings.VOYAGE_EMBED_MODEL)
    return result.embeddings


async def embed_query(text: str) -> list[float] | None:
    """Embed a single query text. Returns None if not configured."""
    embeddings = await embed_texts([text])
    if embeddings is None:
        return None
    return embeddings[0]
