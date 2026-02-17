"""External MCP client — connect to remote MCP servers and call tools.

Uses the MCP SDK's streamablehttp_client for Streamable HTTP transport.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.core.encryption import decrypt_credentials, encrypt_credentials

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.mcp_connector import McpConnector

logger = structlog.get_logger()


async def _get_oauth2_token(connector: McpConnector, db: AsyncSession | None) -> str | None:
    """Get a valid OAuth2 access token, auto-refreshing if expired.

    Returns the access token string, or None if refresh fails.
    Updates the connector's encrypted_credentials in-place if a refresh occurs.
    """
    if not connector.encrypted_credentials:
        return None

    credentials = decrypt_credentials(connector.encrypted_credentials)
    access_token = credentials.get("access_token")
    if not access_token:
        return None

    expires_at = credentials.get("expires_at", 0)
    # Token still valid (with 60-second buffer)
    if time.time() < (expires_at - 60):
        return access_token

    # Need to refresh
    refresh_token = credentials.get("refresh_token")
    account_id = credentials.get("account_id")
    client_id = credentials.get("client_id")

    if not refresh_token or not account_id or not client_id:
        logger.warning(
            "mcp_client.oauth2.missing_refresh_info",
            connector_id=str(connector.id),
        )
        return access_token  # Return stale token — server will reject if truly expired

    if db is None:
        logger.warning(
            "mcp_client.oauth2.no_db_session_for_refresh",
            connector_id=str(connector.id),
        )
        return access_token

    try:
        from app.services.netsuite_oauth_service import refresh_tokens_with_client

        token_data = await refresh_tokens_with_client(account_id, refresh_token, client_id)
        credentials["access_token"] = token_data["access_token"]
        credentials["refresh_token"] = token_data.get("refresh_token", refresh_token)
        credentials["expires_at"] = time.time() + int(token_data.get("expires_in", 3600))

        connector.encrypted_credentials = encrypt_credentials(credentials)
        await db.flush()

        logger.info("mcp_client.oauth2.token_refreshed", connector_id=str(connector.id))
        return credentials["access_token"]
    except Exception:
        logger.exception("mcp_client.oauth2.refresh_failed", connector_id=str(connector.id))
        return access_token  # Return stale token as last resort


async def _build_headers(connector: McpConnector, db: AsyncSession | None = None) -> dict[str, str]:
    """Build auth headers from decrypted connector credentials."""
    headers: dict[str, str] = {}

    if connector.auth_type == "none" or not connector.encrypted_credentials:
        return headers

    if connector.auth_type == "oauth2":
        token = await _get_oauth2_token(connector, db)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    credentials = decrypt_credentials(connector.encrypted_credentials)

    if connector.auth_type == "bearer":
        token = credentials.get("access_token") or credentials.get("token", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif connector.auth_type == "api_key":
        api_key = credentials.get("api_key", "")
        header_name = credentials.get("header_name", "X-API-Key")
        if api_key:
            headers[header_name] = api_key

    return headers


async def discover_tools(connector: McpConnector, db: AsyncSession | None = None) -> list[dict]:
    """Connect to an external MCP server and discover available tools."""
    headers = await _build_headers(connector, db)

    async with streamablehttp_client(url=connector.server_url, headers=headers) as (
        read_stream,
        write_stream,
        _get_session_id,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()

    tools = []
    for tool in result.tools:
        tools.append(
            {
                "name": tool.name,
                "description": getattr(tool, "description", "") or "",
                "input_schema": getattr(tool, "inputSchema", None),
            }
        )

    logger.info(
        "mcp_client.discover_tools",
        server_url=connector.server_url,
        tool_count=len(tools),
    )
    return tools


async def call_external_mcp_tool(
    connector: McpConnector,
    tool_name: str,
    tool_params: dict | None = None,
    db: AsyncSession | None = None,
) -> dict:
    """Call a tool on an external MCP server and return the parsed result."""
    headers = await _build_headers(connector, db)

    async with streamablehttp_client(url=connector.server_url, headers=headers) as (
        read_stream,
        write_stream,
        _get_session_id,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, tool_params or {})

    if result.isError:
        error_text = str(result.content)
        logger.warning(
            "mcp_client.tool_error",
            server_url=connector.server_url,
            tool_name=tool_name,
            error=error_text,
        )
        return {"error": error_text}

    # Parse text content from MCP response
    text_parts = [block.text for block in result.content if hasattr(block, "text")]
    if not text_parts:
        return {"result": "No content returned"}

    raw_text = text_parts[0]
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return {"result": raw_text}
