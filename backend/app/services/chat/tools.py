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
from app.services.chat.celigo_tool_policy import is_celigo_provider, is_read_only_celigo_tool
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


# NetSuite account-id suffixes that mean "not the production books".
# `_SB<n>` is a sandbox, `_RP` a release preview. Mirrors the pattern
# `schemas/workspace.py` already validates sandbox deploy targets against.
_NON_PRODUCTION_ACCOUNT_MARKERS = ("_SB", "_RP", "-SB", "-RP", "_TSTDRV", "TSTDRV")


def netsuite_environment_of(account_id: str | None) -> str:
    """ "SANDBOX" or "PRODUCTION" for a NetSuite account id.

    Derived, never operator-asserted — `metadata_json['account_id']` is set
    from the OAuth setup flow and is the closest thing to ground truth we hold.

    Fails toward caution: an unrecognised id is called PRODUCTION. Mislabelling
    production as sandbox invites a careless write; the reverse only invites
    care.

    NOTE the scope. This is DISPLAY: it lets the model and a human reading
    tool_calls_log tell two connectors apart. It is not the enforcement in
    docs/superpowers/specs/2026-08-27-sandbox-environment-binding-design.md,
    which must refuse a write to the environment the session did not choose,
    at the dispatcher. Seeing the difference is not the same as being unable
    to get it wrong.
    """
    if not account_id:
        return "PRODUCTION"
    upper = str(account_id).upper()
    return "SANDBOX" if any(m in upper for m in _NON_PRODUCTION_ACCOUNT_MARKERS) else "PRODUCTION"


def _connector_tag(connector) -> str:
    """The bracketed prefix on every external tool description.

    NetSuite connectors additionally carry environment and account, because a
    tenant can hold more than one and the difference is production money.
    """
    provider = getattr(connector, "provider", "") or "external"
    if not str(provider).startswith("netsuite"):
        return str(provider)
    meta = getattr(connector, "metadata_json", None)
    account = meta.get("account_id") if isinstance(meta, dict) else None
    # Must be a real string. A test double or a malformed row can yield a
    # truthy non-string here, and stringifying it would stamp a mock's repr
    # into every tool description the model reads.
    if not isinstance(account, str) or not account.strip():
        return str(provider)
    account = account.strip()
    return f"{provider} · {netsuite_environment_of(account)} {account}"


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

            # Celigo is exposed READ-ONLY. Write tools never enter the model's
            # inventory. This is layer 1 of 2 — see _execute_external_tool for
            # the dispatcher guard (agent-graph.md #3: guard the choke point).
            if is_celigo_provider(connector.provider) and not is_read_only_celigo_tool(raw_name):
                continue

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
                    # Name the ENVIRONMENT and ACCOUNT, not just the provider.
                    # Two NetSuite connectors previously produced byte-identical
                    # descriptions, leaving an opaque connector UUID as the only
                    # difference — so a model asked to "test in sandbox" chose
                    # between them arbitrarily, and nobody could tell from the
                    # log which one ran.
                    "description": f"[{_connector_tag(connector)}] {desc}",
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
    human_approved: bool = False,
) -> str:
    """Execute a tool call and return the result as a JSON string.

    Routes to local MCP server or external MCP client based on tool name prefix.

    ``human_approved`` MUST stay default-False. It is the sole permission to
    execute a NetSuite mutation, and only the orchestrator's approve branch —
    which has already HMAC-verified the exact payload a human accepted — may
    pass True. Defaulting to False is the entire point: a caller added later
    that knows nothing about HITL is refused rather than trusted.
    """
    start = time.monotonic()

    # ── HITL guard at the choke point ──
    # This used to live in ONE caller (base_agent.run_streaming, which yields a
    # confirmation card instead of executing), leaving every other caller able
    # to reach the ERP unguarded. That was reachable: a chat session with a
    # workspace_id skips the guarded unified-agent block (orchestrator.py:2933,
    # which ends in `return` at 4009) and falls through to the single-agent
    # loop at 4016, whose toolset includes ns_createRecord/ns_updateRecord/
    # ns_deleteRecord and whose only gate is policy_evaluate — which inspects
    # SQL params and row limits, never mutations. `classify_mutation` appears
    # zero times in orchestrator.py.
    #
    # So the guard moves here, where `.claude/rules/agent-graph.md` #3 says it
    # belongs ("the dispatcher is the choke point... Adding a caller must not
    # be able to add a hole"). Refusing by DEFAULT is what makes that true: the
    # protection no longer depends on each caller remembering it exists.
    #
    # Narrow on purpose — NetSuite mutation verbs only. The write loop is built
    # out of ns_getRecordTypeMetadata / ns_getSubsidiaries / ns_runCustomSuiteQL,
    # and blocking those would break validation, the slot form and the posting
    # invariants.
    if not human_approved:
        from app.services.chat.mutation_guard import classify_mutation as _classify_at_chokepoint

        _verb = _classify_at_chokepoint(tool_name)
        if _verb:
            logger.warning(
                "HITL guard refused an unapproved %s via %s (tenant=%s session=%s)",
                _verb,
                tool_name,
                tenant_id,
                session_id,
            )
            return json.dumps(
                {
                    "error": (
                        f"This {_verb} was NOT executed: a NetSuite write requires explicit human "
                        "approval, and this call carried none."
                    ),
                    "hitl_required": True,
                    "instruction": (
                        "Do not retry this call. Propose the write so the user is shown a "
                        "confirmation card, and let them approve it — the approved payload is "
                        "what executes."
                    ),
                }
            )
    # ── End HITL guard ──

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
        result = await _execute_external_tool(
            connector_id, raw_tool_name, tool_input, tenant_id, db, human_approved=human_approved
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


# NetSuite tools that only READ. Anything not here is treated as a write.
#
# `classify_mutation` recognises four write verbs by name, which is a DENY-list
# — and the NetSuite tool surface is DISCOVERED AT RUNTIME from Oracle's MCP
# server (`session.list_tools()`, mcp_client_service.py:164). Oracle can expose
# a write tool tomorrow that the deny-list has never heard of; it would pass the
# HITL guard, reach the ERP, and mutate a production record with no
# confirmation card. `.claude/rules/agent-graph.md` names the rule:
# "allow-list derived from a registry, never a deny-list".
#
# The trade is deliberate and asymmetric. A NEW READ tool being refused is
# visible, recoverable, and logged loudly below — someone adds a line here. A
# NEW WRITE tool being allowed is an unapproved irreversible ERP mutation that
# nobody learns about until afterwards.
_NETSUITE_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "ns_getRecord",
        "ns_getRecordTypeMetadata",
        "ns_getSuiteQLMetadata",
        "ns_getSubsidiaries",
        "ns_getAccountingBooks",
        "ns_getAccountingContexts",
        "ns_getNexusIds",
        "ns_runCustomSuiteQL",
        "ns_runReport",
        "ns_runSavedSearch",
        "ns_listAllReports",
        "ns_listSavedSearches",
        # *_app tools open NetSuite-hosted UI panels; they mutate nothing here.
        # ns_selector_app is additionally intercepted upstream (see
        # selector_app_redirect) because this client cannot render it.
        "ns_selector_app",
        "ns_report_filters_app",
        "ns_prompt_library_app",
    }
)


def is_netsuite_provider(provider: str | None) -> bool:
    return bool(provider) and str(provider).startswith("netsuite")


async def _execute_external_tool(
    connector_id: uuid.UUID,
    raw_tool_name: str,
    tool_input: dict,
    tenant_id: uuid.UUID,
    db: "AsyncSession",
    human_approved: bool = False,
) -> dict:
    """Execute a tool on an external MCP connector."""
    print(f"[EXT_MCP] Calling {raw_tool_name} with params: {tool_input}", flush=True)
    try:
        from app.services.mcp_connector_service import get_mcp_connector

        connector = await get_mcp_connector(db, connector_id, tenant_id)
        if not connector or not connector.is_enabled:
            return {"error": f"Connector '{connector_id}' not found or disabled"}

        # Layer 2 of 2 — the dispatcher is the choke point. execute_tool_call has
        # several callers and only one consults classify_mutation, so filtering
        # tool definitions alone leaves a hole any new caller can walk through
        # (.claude/rules/agent-graph.md #3). Refuse here regardless of how we got
        # called. Celigo's own delete_resource never blocks server-side.
        #
        # Provider check FIRST so nothing below costs a non-Celigo tool call
        # anything: this runs for every external tool call in every chat turn.
        # NetSuite: allow-list inversion. See _NETSUITE_READ_ONLY_TOOLS.
        # `human_approved` is threaded from execute_tool_call so an operator can
        # still approve an unrecognised tool — fail-closed, not fail-permanently.
        if is_netsuite_provider(connector.provider) and not human_approved:
            if raw_tool_name not in _NETSUITE_READ_ONLY_TOOLS:
                logger.warning(
                    "netsuite_unrecognised_tool_refused tool=%s connector=%s — not on the "
                    "read-only allow-list. If this is a legitimate READ tool, add it to "
                    "_NETSUITE_READ_ONLY_TOOLS; if it writes, it correctly requires approval.",
                    raw_tool_name,
                    connector_id,
                )
                return {
                    "error": (
                        f"'{raw_tool_name}' was NOT executed: it is not a recognised read-only "
                        "NetSuite tool, so it is treated as a write and requires explicit human "
                        "approval."
                    ),
                    "hitl_required": True,
                    "instruction": (
                        "Do not retry this call. If a record must change, propose the write so "
                        "the user is shown a confirmation card and can approve it."
                    ),
                }

        if is_celigo_provider(connector.provider):
            if not is_read_only_celigo_tool(raw_tool_name):
                logger.warning(
                    "celigo_write_blocked",
                    tool=raw_tool_name,
                    connector_id=str(connector_id),
                )
                return {
                    "error": (
                        f"'{raw_tool_name}' is not available. This Celigo connection is "
                        f"read-only — it can read flows, scripts, and errors, but cannot "
                        f"create, change, run, or delete anything."
                    )
                }

            # The `celigo` flag is the kill switch for the whole Celigo surface,
            # and it needs both layers for the same reason the read-only policy
            # does. get_active_connectors_for_tenant enforces it when the tool
            # INVENTORY is built, but a tool_use block emitted before an operator
            # flipped the flag off — or any caller that does not re-derive the
            # inventory — arrives here holding a connector id and would otherwise
            # read live Celigo data after the switch was thrown.
            from app.services.feature_flag_service import is_enabled as _flag_is_enabled

            if not await _flag_is_enabled(db, tenant_id, "celigo"):
                logger.warning(
                    "celigo_disabled_tool_blocked",
                    tool=raw_tool_name,
                    connector_id=str(connector_id),
                )
                return {
                    "error": (
                        f"'{raw_tool_name}' is not available. The Celigo integration is turned off for this workspace."
                    )
                }

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
