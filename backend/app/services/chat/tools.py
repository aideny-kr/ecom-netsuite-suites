"""Tool definitions and execution dispatcher for agentic chat.

Converts local MCP tools and external MCP connector tools into
Anthropic-compatible tool definitions, and provides a unified
execution dispatcher that routes calls to the appropriate backend.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING

import structlog

from app.mcp.registry import TOOL_REGISTRY
from app.mcp.server import mcp_server
from app.services.chat.nodes import ALLOWED_CHAT_TOOLS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# structlog, NOT logging.getLogger: the kwargs-style calls below crash a stdlib
# logger with TypeError wherever INFO is enabled — silently fine under uvicorn
# (WARNING default) but fatal in the celery worker (root hijacked to INFO),
# which broke every worker-side tool dispatch (report auto-refresh).
logger = structlog.get_logger(__name__)

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

        tools.append(
            {
                "name": name.replace(".", "_"),  # Anthropic requires alphanumeric + underscores
                "description": tool["description"],
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
        )

    # Synthetic control tool: a Layer-2 reasoning-depth escalation signal. Not a
    # data tool and not routed to mcp_server — execute_tool_call special-cases it
    # and the agent loop bumps current_thinking_level when the model calls it.
    tools.append(
        {
            "name": "escalate_reasoning",
            "description": (
                "Call this when the current question needs deeper, more careful "
                "reasoning than a quick answer — multi-step logic, ambiguous "
                "requirements, reconciling conflicting data, or tricky SuiteQL. "
                "Calling it increases your reasoning depth for the rest of this "
                "turn. Use it sparingly, only when genuinely warranted."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "rationale": {
                        "type": "string",
                        "description": "One short phrase on why deeper reasoning is needed.",
                    }
                },
                "required": [],
            },
        }
    )
    return tools


# Mapping from Anthropic-safe local tool name back to MCP tool name
_LOCAL_NAME_MAP: dict[str, str] = {name.replace(".", "_"): name for name in TOOL_REGISTRY if name in ALLOWED_CHAT_TOOLS}


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
    rest = name[len(_EXT_PREFIX) :]
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
    """Convert discovered MCP connector tools to Anthropic tool format.

    Tool descriptions are passed through unchanged — no truncation. Oracle's
    NetSuite MCP Standard Tools SuiteApp ships expert SuiteQL dialect rules
    (string concatenation, date literals, ANSI joins, no CTE support, etc.)
    baked directly into tool descriptions. Any local truncation here is a
    direct handicap relative to the Claude-direct + MCP baseline — our
    agent's north star is to match or beat that baseline, so we pass
    descriptions through as-is. The Anthropic API enforces its own limits
    and will reject requests that exceed them; we let that be the
    authoritative bound rather than imposing our own arbitrary cap.
    """
    # Sort for byte-stable output: connectors by UUID, tools within each by raw
    # name. The Anthropic prompt-cache breakpoint is stamped on the last tool,
    # so a non-deterministic order shifts the breakpoint and silently invalidates
    # the cache.
    tools = []
    for connector in sorted(connectors, key=lambda c: str(c.id)):
        if not connector.discovered_tools:
            continue
        sorted_discovered = sorted(connector.discovered_tools, key=lambda t: t.get("name", ""))
        for tool in sorted_discovered:
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

            tools.append(
                {
                    "name": anthropic_name,
                    "description": f"[{connector.provider}] {desc}",
                    "input_schema": input_schema,
                }
            )
    return tools


# Tools that require an active connector to be included (provider → tool name prefixes)
_CONNECTOR_GATED_TOOLS: dict[str, set[str]] = {
    "bigquery": {"bigquery_sql", "bigquery_schema", "bigquery_cost_estimate"},
    "google_sheets": {"sheets_create", "sheets_write_range", "sheets_read_range"},
}


async def build_all_tool_definitions(
    db: "AsyncSession",
    tenant_id: uuid.UUID,
    plan_mode_enabled: bool = False,
) -> list[dict]:
    """Build combined local + external tool definitions for Claude.

    When ``plan_mode_enabled`` is True, the ``clarify`` tool is appended so the
    LLM has access to it on financial-ambiguous turns. The gate that ACTIVATES
    clarify (filters inventory to clarify-only + force tool_choice) lives in
    the orchestrator + unified_agent — this builder just registers the tool.
    """
    tools = build_local_tool_definitions()

    try:
        from app.services.mcp_connector_service import get_active_connectors_for_tenant

        connectors = await get_active_connectors_for_tenant(db, tenant_id)

        # Determine which connector-gated tools to include
        active_providers = {c.provider for c in connectors} if connectors else set()
        gated_tools_to_remove: set[str] = set()
        for provider, tool_names in _CONNECTOR_GATED_TOOLS.items():
            if provider not in active_providers:
                gated_tools_to_remove.update(tool_names)

        if gated_tools_to_remove:
            tools = [t for t in tools if t["name"] not in gated_tools_to_remove]

        if connectors:
            # Skip connectors whose tools are registered locally (e.g. BigQuery)
            _LOCAL_TOOL_PROVIDERS = set(_CONNECTOR_GATED_TOOLS.keys())
            external = [c for c in connectors if c.provider not in _LOCAL_TOOL_PROVIDERS]
            tools.extend(build_external_tool_definitions(external))
    except Exception:
        logger.warning("Failed to fetch external MCP connectors for tools", exc_info=True)

    from app.mcp.tools.result_reference_tool import TOOL_DEFINITION as _REF_RESULT_TOOL

    tools.append(dict(_REF_RESULT_TOOL))

    if plan_mode_enabled:
        from app.services.chat.plan_mode.clarify_tool import get_clarify_tool

        clarify = get_clarify_tool(plan_mode_enabled)
        if clarify is not None:
            # Copy to avoid shared-mutation surprises (callers may stamp
            # category/cache_control onto returned tool dicts).
            tools.append(dict(clarify))

    return tools


async def execute_tool_call(
    tool_name: str,
    tool_input: dict,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    correlation_id: str,
    db: "AsyncSession",
    context_need: str | None = None,
    session_id: str | None = None,
    actor_type: str = "user",
) -> str:
    """Execute a tool call and return the result as a JSON string.

    Routes to local MCP server or external MCP client based on tool name prefix.
    """
    start = time.monotonic()

    if tool_name == "escalate_reasoning":
        # Control signal handled by the agent loop (it bumps thinking depth).
        # Returning a terse ack keeps the tool-result contract intact.
        return json.dumps({"ok": True, "message": "Reasoning depth increased for this turn."})

    if tool_name == "reference_previous_result":
        from app.mcp.tools.result_reference_tool import execute_reference_previous_result

        return await execute_reference_previous_result(
            conversation_id=session_id or "",
            message_id=tool_input.get("message_id"),
        )

    # Check if it's an external tool
    ext_parsed = parse_external_tool_name(tool_name)
    if ext_parsed is not None:
        connector_id, raw_tool_name = ext_parsed
        result = await _execute_external_tool(connector_id, raw_tool_name, tool_input, tenant_id, db)
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
            # a system actor (report auto-refresh sweep) is None — str(None) == "None"
            # is truthy and governance's uuid.UUID(actor_id) would raise on it
            actor_id=str(actor_id) if actor_id is not None else None,
            actor_type=actor_type,
            correlation_id=correlation_id,
            db=db,
            context_need=context_need,
            session_id=session_id,
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
    print(f"[EXT_MCP] Calling {raw_tool_name} with params: {tool_input}", flush=True)
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
