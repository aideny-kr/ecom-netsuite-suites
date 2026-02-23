"""Web search tool for the chat system.

Searches the web using DuckDuckGo (no API key required) and returns
structured results. Designed for AI agent use: concise snippets,
not full page content.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _sync_search(query: str, max_results: int) -> list[dict]:
    """Run DuckDuckGo search synchronously (called via asyncio.to_thread)."""
    from duckduckgo_search import DDGS

    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))


async def execute(
    params: dict[str, Any],
    context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict:
    """Search the web and return top results.

    Parameters
    ----------
    params.query : str
        Search query string.
    params.max_results : int, optional
        Maximum number of results to return (default 5, max 10).
    """
    query = params.get("query", "").strip()
    if not query:
        return {"error": "query parameter is required"}

    max_results = min(int(params.get("max_results", 5)), 10)

    try:
        raw_results = await asyncio.to_thread(_sync_search, query, max_results)

        results = []
        for r in raw_results:
            results.append(
                {
                    "title": r.get("title", ""),
                    "snippet": r.get("body", "")[:500],
                    "url": r.get("href", ""),
                }
            )

        return {
            "results": results,
            "count": len(results),
            "query": query,
        }

    except ImportError:
        logger.error("duckduckgo-search package not installed")
        return {
            "error": "Web search not available: duckduckgo-search package not installed. "
            "Install with: pip install duckduckgo-search"
        }
    except Exception as exc:
        logger.warning("web_search.execute failed", exc_info=True)
        return {"error": f"Web search failed: {exc}"}
