"""Web search tool for the chat system.

Searches the web using Brave Search API (primary) or DuckDuckGo (fallback)
and returns structured results. Designed for AI agent use: concise snippets,
not full page content.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


async def _brave_search(query: str, max_results: int) -> list[dict]:
    """Search via Brave Search API."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={
                "q": query,
                "count": max_results,
                "extra_snippets": True,
            },
            headers={
                "X-Subscription-Token": settings.BRAVE_SEARCH_API_KEY,
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("web", {}).get("results", [])[:max_results]:
        snippet = item.get("description", "")
        extra = item.get("extra_snippets", [])
        if extra:
            snippet += "\n" + "\n".join(extra[:2])
        results.append(
            {
                "title": item.get("title", ""),
                "snippet": snippet[:800],
                "url": item.get("url", ""),
            }
        )
    return results


def _sync_ddg_search(query: str, max_results: int) -> list[dict]:
    """DuckDuckGo fallback (no API key required)."""
    from ddgs import DDGS

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

    # Try Brave Search first if configured
    if settings.BRAVE_SEARCH_API_KEY:
        try:
            results = await _brave_search(query, max_results)
            return {
                "results": results,
                "count": len(results),
                "query": query,
                "provider": "brave",
            }
        except Exception as exc:
            logger.warning("Brave search failed, falling back to DuckDuckGo: %s", exc)

    # Fallback to DuckDuckGo
    try:
        raw_results = await asyncio.to_thread(_sync_ddg_search, query, max_results)
        results = [
            {
                "title": r.get("title", ""),
                "snippet": r.get("body", "")[:500],
                "url": r.get("href", ""),
            }
            for r in raw_results
        ]
        return {
            "results": results,
            "count": len(results),
            "query": query,
            "provider": "duckduckgo",
        }
    except ImportError:
        logger.error("ddgs package not installed and Brave API key not set")
        return {"error": "Web search not available: set BRAVE_SEARCH_API_KEY or install ddgs package"}
    except Exception as exc:
        logger.warning("web_search.execute failed", exc_info=True)
        return {"error": f"Web search failed: {exc}"}
