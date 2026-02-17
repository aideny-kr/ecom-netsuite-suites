"""NetSuite SuiteQL client — MCP-first with REST API fallback.

Mirrors the working OAuth 2.0 + MCP flow from netsuite-mcp-chatapp:
  - MCP transport: Streamable HTTP (type="http") to /services/mcp/v1/all
  - Auth: Bearer token in Authorization header
  - Tool: ns_runCustomSuiteQL with { sqlQuery, description }
  - Response: { method, description, queryExecuted, resultCount, data: [...] }
"""

from __future__ import annotations

import json

import httpx
import structlog

from app.core.config import settings

logger = structlog.get_logger()


def _normalize_account_id(account_id: str) -> str:
    """Normalize account ID for use in NetSuite URLs.

    NetSuite accepts plain numeric IDs (e.g. '6738075') or hyphenated
    sandbox IDs (e.g. '6738075-sb1'). Underscores are converted to hyphens.
    """
    return account_id.replace("_", "-").lower()


def _rest_url(account_id: str) -> str:
    slug = _normalize_account_id(account_id)
    return f"https://{slug}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"


def _mcp_url(account_id: str) -> str:
    slug = _normalize_account_id(account_id)
    return f"https://{slug}.suitetalk.api.netsuite.com/services/mcp/v1/all"


async def execute_suiteql_via_rest(access_token: str, account_id: str, query: str, limit: int = 1000) -> dict:
    """Execute a SuiteQL query via the NetSuite REST API."""
    url = _rest_url(account_id)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Prefer": "transient",
    }
    async with httpx.AsyncClient(timeout=settings.NETSUITE_SUITEQL_TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json={"q": query})
        resp.raise_for_status()

    data = resp.json()
    items = data.get("items", [])
    columns = list(items[0].keys()) if items else []
    rows = [list(item.values()) for item in items]
    total_results = data.get("totalResults", len(rows))

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": total_results > len(rows),
    }


async def execute_suiteql_via_mcp(access_token: str, account_id: str, query: str, limit: int = 1000) -> dict:
    """Execute a SuiteQL query via NetSuite's native MCP endpoint.

    Uses the MCP SDK's StreamableHTTP transport (matching the reference
    netsuite-mcp-chatapp's "http" transport type) to call ns_runCustomSuiteQL.

    NetSuite MCP response format:
      { method, description, queryExecuted, resultCount, data: [{...}, ...] }
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = _mcp_url(account_id)
    headers = {"Authorization": f"Bearer {access_token}"}

    async with streamablehttp_client(url=url, headers=headers) as (
        read_stream,
        write_stream,
        _get_session_id,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                "ns_runCustomSuiteQL",
                {"sqlQuery": query, "description": "SuiteQL query via MCP"},
            )

    if result.isError:
        raise RuntimeError(f"MCP tool error: {result.content}")

    # MCP tool results come as content blocks; extract text
    text_parts = [block.text for block in result.content if hasattr(block, "text")]
    raw = json.loads(text_parts[0]) if text_parts else {}

    # NetSuite MCP returns data in the "data" field (array of objects).
    # Fall back to "items" or "rows" for other response shapes.
    items = raw.get("data", raw.get("items", raw.get("rows", [])))
    result_count = raw.get("resultCount", len(items))

    if items and isinstance(items[0], dict):
        columns = list(items[0].keys())
        rows = [list(item.values()) for item in items]
    else:
        columns = raw.get("columns", [])
        rows = items

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": result_count > len(rows),
    }


async def execute_suiteql(access_token: str, account_id: str, query: str, limit: int = 1000) -> dict:
    """Execute SuiteQL — try MCP first, fall back to REST API."""
    try:
        return await execute_suiteql_via_mcp(access_token, account_id, query, limit)
    except Exception as exc:
        logger.warning(
            "netsuite.mcp_fallback_to_rest",
            error=str(exc),
            account_id=account_id,
        )

    return await execute_suiteql_via_rest(access_token, account_id, query, limit)
