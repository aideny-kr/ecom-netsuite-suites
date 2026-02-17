"""Tool definitions and execution dispatcher for agentic chat.

Converts local MCP tools and external MCP connector tools into
Anthropic-compatible tool definitions, and provides a unified
execution dispatcher that routes calls to the appropriate backend.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from app.mcp.registry import TOOL_REGISTRY
from app.mcp.server import mcp_server
from app.services.chat.nodes import ALLOWED_CHAT_TOOLS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Max length for Anthropic tool names (alphanumeric + underscores)
_MAX_TOOL_NAME_LEN = 64
_EXT_PREFIX = "ext__"


def _schema_property_to_anthropic(name: str, spec: dict) -> dict:
    """Convert a single MCP params_schema entry to JSON Schema property."""
    prop: dict = {}
    typ = spec.get("type", "string")
    if typ == "integer":
        prop["type"] = "integer"
    elif typ == "array":
        prop["type"] = "array"
    elif typ == "object":
        prop["type"] = "object"
    else:
        prop["type"] = "string"
    if "description" in spec:
        prop["description"] = spec["description"]
    if "default" in spec:
        prop["default"] = spec["default"]
    return prop


def build_local_tool_definitions() -> list[dict]:
    """Convert allowed local MCP tools to Anthropic tool format."""
    tools = []
    for name, tool in TOOL_REGISTRY.items():
        if name not in ALLOWED_CHAT_TOOLS:
            continue
        properties = {}
        required = []
        for param_name, param_spec in tool.get("params_schema", {}).items():
            properties[param_name] = _schema_property_to_anthropic(param_name, param_spec)
            if param_spec.get("required", False):
                required.append(param_name)

        tools.append({
            "name": name.replace(".", "_"),  # Anthropic requires alphanumeric + underscores
            "description": tool["description"],
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        })
    return tools


# Mapping from Anthropic-safe local tool name back to MCP tool name
_LOCAL_NAME_MAP: dict[str, str] = {
    name.replace(".", "_"): name for name in TOOL_REGISTRY if name in ALLOWED_CHAT_TOOLS
}


def _make_ext_tool_name(connector_id: uuid.UUID, raw_name: str) -> str:
    """Create an Anthropic-safe external tool name.

    Format: ext__{connector_id_hex}__{tool_name}
    Truncates tool_name if the result would exceed _MAX_TOOL_NAME_LEN.
    """
    hex_id = connector_id.hex  # 32 chars
    prefix = f"{_EXT_PREFIX}{hex_id}__"  # 38 chars
    max_name_len = _MAX_TOOL_NAME_LEN - len(prefix)
    # Sanitize: replace non-alphanumeric chars with underscores
    safe_name = "".join(c if c.isalnum() or c == "_" else "_" for c in raw_name)
    return prefix + safe_name[:max_name_len]


def parse_external_tool_name(name: str) -> tuple[uuid.UUID, str] | None:
    """Reverse the external tool naming. Returns (connector_id, raw_tool_name) or None."""
    if not name.startswith(_EXT_PREFIX):
        return None
    rest = name[len(_EXT_PREFIX):]
    # hex_id is 32 chars, followed by "__"
    if len(rest) < 34 or rest[32:34] != "__":
        return None
    hex_id = rest[:32]
    raw_name = rest[34:]
    try:
        connector_id = uuid.UUID(hex_id)
    except ValueError:
        return None
    return connector_id, raw_name


def build_external_tool_definitions(connectors: list) -> list[dict]:
    """Convert discovered MCP connector tools to Anthropic tool format."""
    tools = []
    for connector in connectors:
        if not connector.discovered_tools:
            continue
        for tool in connector.discovered_tools:
            raw_name = tool.get("name", "unknown")
            anthropic_name = _make_ext_tool_name(connector.id, raw_name)
            desc = tool.get("description", "") or ""
            # Use the tool's input_schema if available, otherwise empty
            input_schema = tool.get("input_schema") or {
                "type": "object",
                "properties": {},
            }
            # Ensure it has required top-level fields
            if "type" not in input_schema:
                input_schema["type"] = "object"

            tools.append({
                "name": anthropic_name,
                "description": f"[{connector.provider}] {desc}"[:1024],
                "input_schema": input_schema,
            })
    return tools


async def build_all_tool_definitions(db: "AsyncSession", tenant_id: uuid.UUID) -> list[dict]:
    """Build combined local + external tool definitions for Claude."""
    tools = build_local_tool_definitions()

    try:
        from app.services.mcp_connector_service import get_active_connectors_for_tenant

        connectors = await get_active_connectors_for_tenant(db, tenant_id)
        if connectors:
            tools.extend(build_external_tool_definitions(connectors))
    except Exception:
        logger.warning("Failed to fetch external MCP connectors for tools", exc_info=True)

    return tools


async def execute_tool_call(
    tool_name: str,
    tool_input: dict,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    correlation_id: str,
    db: "AsyncSession",
) -> str:
    """Execute a tool call and return the result as a JSON string.

    Routes to local MCP server or external MCP client based on tool name prefix.
    """
    start = time.monotonic()

    # Check if it's an external tool
    ext_parsed = parse_external_tool_name(tool_name)
    if ext_parsed is not None:
        connector_id, raw_tool_name = ext_parsed
        result = await _execute_external_tool(
            connector_id, raw_tool_name, tool_input, tenant_id, db
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "tool_executed",
            tool=tool_name,
            source="external",
            duration_ms=duration_ms,
        )
        return json.dumps(result, default=str)

    # Local tool — reverse the name sanitization
    mcp_name = _LOCAL_NAME_MAP.get(tool_name)
    if mcp_name is None:
        return json.dumps({"error": f"Tool '{tool_name}' is not allowed in chat."})

    try:
        result = await mcp_server.call_tool(
            tool_name=mcp_name,
            params=tool_input,
            tenant_id=str(tenant_id),
            actor_id=str(actor_id),
            correlation_id=correlation_id,
            db=db,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "tool_executed",
            tool=mcp_name,
            source="local",
            duration_ms=duration_ms,
        )
        return json.dumps(result, default=str)
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning("Local tool %s failed", mcp_name, exc_info=True)
        return json.dumps({"error": f"Tool '{mcp_name}' execution failed: {exc}"})


async def _execute_external_tool(
    connector_id: uuid.UUID,
    raw_tool_name: str,
    tool_input: dict,
    tenant_id: uuid.UUID,
    db: "AsyncSession",
) -> dict:
    """Execute a tool on an external MCP connector."""
    try:
        from app.services.mcp_connector_service import get_mcp_connector

        connector = await get_mcp_connector(db, connector_id, tenant_id)
        if not connector or not connector.is_enabled:
            return {"error": f"Connector '{connector_id}' not found or disabled"}

        from app.services.mcp_client_service import call_external_mcp_tool

        return await call_external_mcp_tool(connector, raw_tool_name, tool_input, db=db)
    except Exception as exc:
        logger.warning(
            "External tool %s on connector %s failed",
            raw_tool_name,
            connector_id,
            exc_info=True,
        )
        return {"error": f"External tool '{raw_tool_name}' execution failed: {exc}"}
