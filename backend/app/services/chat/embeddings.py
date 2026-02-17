import voyageai

from app.core.config import settings

_client: voyageai.AsyncClient | None = None


def get_voyage_client() -> voyageai.AsyncClient:
    global _client
    if _client is None:
        _client = voyageai.AsyncClient(api_key=settings.VOYAGE_API_KEY)
    return _client


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch embed texts using Voyage AI."""
    client = get_voyage_client()
    result = await client.embed(texts, model=settings.VOYAGE_EMBED_MODEL)
    return result.embeddings


async def embed_query(text: str) -> list[float]:
    """Embed a single query text."""
    embeddings = await embed_texts([text])
    return embeddings[0]
