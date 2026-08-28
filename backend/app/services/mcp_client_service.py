"""External MCP client — connect to remote MCP servers and call tools.

Uses the MCP SDK's streamablehttp_client for Streamable HTTP transport.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.core.encryption import decrypt_credentials, encrypt_credentials
from app.services.chat.write_outcome import INDETERMINATE_KEY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.mcp_connector import McpConnector

logger = structlog.get_logger()

# One ceiling for every tool. There used to be two tiers — 60s for four named
# read tools, 15s for everything else — which handed every irreversible WRITE
# the shortest budget in the system while the reads got the headroom. That is
# backwards on the risk axis, and it is not a hypothetical: on 2026-08-27
# against sandbox 6738075-sb1, ns_createRecord blew the 15s ceiling, NetSuite
# created the customer anyway (internal id 5264348), and the app recorded the
# write as failed and offered to re-run the identical payload. A timeout is a
# statement about our patience, never about what NetSuite committed.
#
# A per-tool table also cannot be complete here: the NetSuite tool surface is
# discovered at runtime from Oracle's MCP server, so any name not in the table
# silently inherited the short, write-orphaning default. Same fail-closed
# reasoning as _NETSUITE_READ_ONLY_TOOLS in chat/tools.py — the safe value is
# the one an unknown tool gets.
#
# 60s is the ceiling ns_getRecordTypeMetadata already earned (controller-
# verified live 2026-08-25 at 11-18s). Slow tools were always the ones that
# mattered; the 15s tier only ever bought a faster failure on tools that are
# fast anyway. The residual — a write that exceeds even 60s — is handled as an
# indeterminate outcome rather than by guessing, see write_outcome.py.
_TOOL_TIMEOUT_SECONDS = 60.0


def _tool_timeout_seconds(tool_name: str) -> float:
    """The wall-clock ceiling for one MCP tool call.

    Takes *tool_name* so callers and tests keep a single named policy seam —
    the current policy is deliberately uniform, and a future exception must be
    argued for here rather than inherited by omission.
    """
    return _TOOL_TIMEOUT_SECONDS


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
        logger.error(
            "mcp_client.oauth2.missing_refresh_info",
            connector_id=str(connector.id),
        )
        return None  # Fail explicitly so callers know auth is broken

    if db is None:
        logger.error(
            "mcp_client.oauth2.no_db_session_for_refresh",
            connector_id=str(connector.id),
        )
        return None  # Fail explicitly — cannot refresh without DB session

    # ── Lock: prevent concurrent refresh of same MCP connector ──
    from app.core.redis_lock import acquire_lock, release_lock

    lock_key = f"oauth_refresh:mcp:{connector.id}"

    if not acquire_lock(lock_key, timeout=30):
        # Another process is refreshing — wait briefly, then re-read
        import asyncio

        await asyncio.sleep(2)
        await db.refresh(connector)
        credentials = decrypt_credentials(connector.encrypted_credentials)
        return credentials.get("access_token")

    try:
        # Re-check after acquiring lock (another process may have finished)
        await db.refresh(connector)
        credentials = decrypt_credentials(connector.encrypted_credentials)
        if time.time() < (credentials.get("expires_at", 0) - 60):
            return credentials["access_token"]

        from app.services.netsuite_oauth_service import refresh_tokens_with_client

        token_data = await refresh_tokens_with_client(account_id, refresh_token, client_id)
        credentials["access_token"] = token_data["access_token"]
        credentials["refresh_token"] = token_data.get("refresh_token", refresh_token)
        credentials["expires_at"] = time.time() + int(token_data.get("expires_in", 3600))

        connector.encrypted_credentials = encrypt_credentials(credentials)
        await db.commit()

        logger.info("mcp_client.oauth2.token_refreshed", connector_id=str(connector.id))
        return credentials["access_token"]
    except Exception:
        logger.exception("mcp_client.oauth2.refresh_failed", connector_id=str(connector.id))
        return None  # Fail explicitly so callers know auth is broken
    finally:
        release_lock(lock_key)


async def _build_headers(connector: McpConnector, db: AsyncSession | None = None) -> dict[str, str]:
    """Build auth headers from decrypted connector credentials."""
    headers: dict[str, str] = {}

    if connector.auth_type == "none" or not connector.encrypted_credentials:
        return headers

    if connector.auth_type == "oauth2":
        token = await _get_oauth2_token(connector, db)
        if not token:
            raise RuntimeError(
                f"MCP connector {connector.id}: OAuth 2.0 token expired and refresh failed. "
                "User must re-authorize the NetSuite connection."
            )
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

    result = None
    try:
        async with streamablehttp_client(url=connector.server_url, headers=headers) as (
            read_stream,
            write_stream,
            _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
    except (BaseExceptionGroup, ExceptionGroup):
        if result is None:
            raise

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

    if tool_params is None:
        tool_params = {}

    # --- GOVERNANCE INTERCEPT ---
    if tool_name == "ns_runCustomSuiteQL" and "sqlQuery" in tool_params:
        sql = tool_params["sqlQuery"].strip().rstrip(";")
        sql_upper = sql.upper()

        if "ROWNUM" not in sql_upper and "FETCH" not in sql_upper:
            sql = f"{sql} FETCH FIRST 50 ROWS ONLY"
            tool_params["sqlQuery"] = sql

    # Coerce ns_runReport params — LLM sometimes sends strings instead of numbers
    # and may hallucinate extra params like "filters"
    if tool_name == "ns_runReport":
        _ALLOWED_REPORT_PARAMS = {"reportId", "dateTo", "dateFrom", "subsidiaryId"}
        tool_params = {k: v for k, v in tool_params.items() if k in _ALLOWED_REPORT_PARAMS}
        for num_field in ("reportId", "subsidiaryId"):
            if num_field in tool_params:
                try:
                    tool_params[num_field] = int(float(str(tool_params[num_field])))
                except (ValueError, TypeError):
                    pass
        print(f"[EXT_MCP] ns_runReport coerced params: {tool_params}", flush=True)

    # Coerce ns_runSavedSearch numeric params
    if tool_name == "ns_runSavedSearch":
        for num_field in ("searchId", "range_start", "range_end"):
            if num_field in tool_params:
                try:
                    tool_params[num_field] = int(float(str(tool_params[num_field])))
                except (ValueError, TypeError):
                    pass
    # ----------------------------

    headers = await _build_headers(connector, db)

    # Reports and saved searches can take longer than simple queries
    # SuiteQL needs 60s too — systemnote and complex JOINs can exceed 15s
    timeout = _tool_timeout_seconds(tool_name)
    result = None

    try:
        async with streamablehttp_client(url=connector.server_url, headers=headers) as (
            read_stream,
            write_stream,
            _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                try:
                    result = await asyncio.wait_for(session.call_tool(tool_name, tool_params), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.error("mcp_client.tool_timeout", server_url=connector.server_url, tool_name=tool_name)
                    # Marked here because this is the ONLY place that knows the
                    # call timed out rather than failed. Downstream must not
                    # re-derive it from the message text: a timeout says what
                    # our patience ran out on, never what NetSuite committed,
                    # and on 2026-08-27 exactly this case created customer
                    # 5264348 while the app reported failure and offered to
                    # run the same payload again. See write_outcome.py.
                    return {
                        "error": f"Tool execution exceeded {int(timeout)}-second timeout limit",
                        INDETERMINATE_KEY: True,
                    }
    except (BaseExceptionGroup, ExceptionGroup) as eg:
        # The MCP streamable HTTP client sometimes raises on cleanup even
        # after a successful call_tool. If we got a result, use it.
        if result is not None:
            logger.warning(
                "mcp_client.cleanup_error_after_success",
                tool_name=tool_name,
                error=str(eg),
            )
        else:
            # Falls through to the `result is None` return below, which marks
            # the outcome INDETERMINATE. This used to `raise`, and the raise was
            # a hole: the dispatcher's blanket `except Exception` in
            # chat/tools.py turned it into a plain {"error": ...} with no
            # marker, so a connection dropped mid-write classified as a
            # NetSuite REJECTION and re-entered the repair loop — the exact
            # duplicate-inviting path this file's timeout branch was patched to
            # close, still open on the commoner failure. Found by review, not
            # by the fix that claimed to have closed it.
            logger.error(
                "mcp_client.transport_failed_outcome_unknown",
                server_url=connector.server_url,
                tool_name=tool_name,
                error=str(eg),
            )
    except Exception as exc:
        # Any other failure of the transport itself. Reaching here means we
        # never read a response, so we cannot know whether NetSuite acted —
        # which is the definition of indeterminate. Deliberately broad: the
        # asymmetry is not close. A spurious "check NetSuite" costs a glance;
        # a missed one costs a duplicate in a customer's general ledger.
        #
        # Pre-flight failures (unknown/disabled connector) return their own
        # dicts before this point and never reach here, so this does not
        # mislabel calls that were never sent.
        logger.error(
            "mcp_client.transport_failed_outcome_unknown",
            server_url=connector.server_url,
            tool_name=tool_name,
            error=str(exc),
        )
        return {
            "error": f"Tool '{tool_name}' failed in transport ({type(exc).__name__}: {exc})",
            INDETERMINATE_KEY: True,
        }

    if result is None:
        # Reached when the transport raised on cleanup with no result in hand
        # (see the ExceptionGroup branch above). Same epistemic position as a
        # timeout: the call may or may not have been delivered.
        return {"error": f"Tool '{tool_name}' returned no result", INDETERMINATE_KEY: True}

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
