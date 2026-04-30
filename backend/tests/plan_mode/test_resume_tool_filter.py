"""Verify resume turn filters tool inventory to chosen source."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.services.chat.plan_mode.short_circuit import filter_tools_for_chosen_source


@dataclass
class _FakeConnector:
    """Minimal stand-in for an MCP connector row (only the fields the filter needs)."""

    id: uuid.UUID
    provider: str


def _ext_tool_name(connector_id: uuid.UUID, raw_name: str) -> str:
    """Build an ext__<32-hex>__<name> tool name that ``parse_external_tool_name`` accepts."""
    return f"ext__{connector_id.hex}__{raw_name}"


# Realistic UUIDs for ext__ tools so ``parse_external_tool_name`` succeeds.
_NETSUITE_MCP_UUID = uuid.uuid4()
_SHOPIFY_MCP_UUID = uuid.uuid4()
_STRIPE_MCP_UUID = uuid.uuid4()

_NS_EXT_TOOL = _ext_tool_name(_NETSUITE_MCP_UUID, "ns_getRecord")
_SHOPIFY_EXT_TOOL = _ext_tool_name(_SHOPIFY_MCP_UUID, "shopify_query")
_STRIPE_EXT_TOOL = _ext_tool_name(_STRIPE_MCP_UUID, "stripe_query")

_TOOLS = [
    {"name": "bigquery_sql", "description": "..."},
    {"name": "bigquery_schema", "description": "..."},
    {"name": "netsuite_suiteql", "description": "..."},
    {"name": _NS_EXT_TOOL, "description": "..."},
    {"name": "shopify_orders", "description": "..."},
    {"name": "stripe_payouts", "description": "..."},
    {"name": "pivot_query_result", "description": "..."},
    {"name": "docs_create", "description": "..."},
    {"name": "drive_read_doc", "description": "..."},
    {"name": "clarify", "description": "..."},
    {"name": "reference_previous_result", "description": "..."},
]

_ALL_CONNECTORS = [
    _FakeConnector(id=_NETSUITE_MCP_UUID, provider="netsuite_mcp"),
    _FakeConnector(id=_SHOPIFY_MCP_UUID, provider="shopify_mcp"),
    # Round 8 Bug 2: ext__<uuid>__ tools come from MCP connectors only —
    # REST stripe (connections.provider == 'stripe') has no chat tools, so
    # canonicalization no longer accepts bare 'stripe'. Test the MCP path.
    _FakeConnector(id=_STRIPE_MCP_UUID, provider="stripe_mcp"),
]


def test_chose_netsuite_drops_other_data_sources():
    filtered = filter_tools_for_chosen_source(_TOOLS, "netsuite", active_connectors=_ALL_CONNECTORS)
    names = {t["name"] for t in filtered}
    assert "bigquery_sql" not in names
    assert "bigquery_schema" not in names
    assert "shopify_orders" not in names
    assert "stripe_payouts" not in names
    assert "netsuite_suiteql" in names
    assert _NS_EXT_TOOL in names


def test_cross_source_tools_always_included():
    """pivot, docs_create, drive_read_doc, clarify, reference_previous_result work across sources."""
    filtered = filter_tools_for_chosen_source(_TOOLS, "netsuite", active_connectors=_ALL_CONNECTORS)
    names = {t["name"] for t in filtered}
    assert "pivot_query_result" in names
    assert "docs_create" in names
    assert "drive_read_doc" in names
    assert "clarify" in names
    assert "reference_previous_result" in names


def test_chose_bigquery_drops_netsuite():
    filtered = filter_tools_for_chosen_source(_TOOLS, "bigquery", active_connectors=_ALL_CONNECTORS)
    names = {t["name"] for t in filtered}
    assert "netsuite_suiteql" not in names
    assert _NS_EXT_TOOL not in names
    assert "bigquery_sql" in names
    assert "bigquery_schema" in names


def test_chose_shopify():
    filtered = filter_tools_for_chosen_source(_TOOLS, "shopify", active_connectors=_ALL_CONNECTORS)
    names = {t["name"] for t in filtered}
    assert "shopify_orders" in names
    assert "netsuite_suiteql" not in names
    assert "bigquery_sql" not in names


def test_unknown_source_keeps_only_cross_source():
    """Defensive: unknown source returns just the cross-source tools (no data tools)."""
    filtered = filter_tools_for_chosen_source(_TOOLS, "unknown_source", active_connectors=_ALL_CONNECTORS)
    names = {t["name"] for t in filtered}
    assert "netsuite_suiteql" not in names
    assert "bigquery_sql" not in names
    # Cross-source tools still allowed (agent can still summarize prior data, etc.)
    assert "pivot_query_result" in names
    assert "clarify" in names


def test_empty_input():
    assert filter_tools_for_chosen_source([], "netsuite", active_connectors=_ALL_CONNECTORS) == []


def test_preserves_order():
    """Filter is order-stable — important for prompt cache consistency."""
    filtered = filter_tools_for_chosen_source(_TOOLS, "netsuite", active_connectors=_ALL_CONNECTORS)
    assert filtered == [
        {"name": "netsuite_suiteql", "description": "..."},
        {"name": _NS_EXT_TOOL, "description": "..."},
        {"name": "pivot_query_result", "description": "..."},
        {"name": "docs_create", "description": "..."},
        {"name": "drive_read_doc", "description": "..."},
        {"name": "clarify", "description": "..."},
        {"name": "reference_previous_result", "description": "..."},
    ]


# ---------------------------------------------------------------------------
# codex P2: ext__ prefix is too broad — must scope by connector provider.
# ---------------------------------------------------------------------------
#
# Before the fix, ``_SOURCE_TOOL_PREFIXES["netsuite"]`` matched ``"ext__"``,
# so ``chose=netsuite`` kept Shopify MCP and Stripe MCP tools too (all of
# them start with ``ext__<uuid>__``). Conversely ``chose=shopify`` dropped
# every MCP-routed Shopify tool. The fix passes connector metadata in and
# resolves each ext__ tool's UUID to its provider before filtering.


def test_chose_netsuite_drops_shopify_ext_tools():
    """ext__<shopify_uuid>__shopify_query must be dropped when chose=netsuite."""
    tools = [
        {"name": "netsuite_suiteql", "description": "..."},
        {"name": _SHOPIFY_EXT_TOOL, "description": "..."},
        {"name": _NS_EXT_TOOL, "description": "..."},
    ]
    filtered = filter_tools_for_chosen_source(
        tools,
        "netsuite",
        active_connectors=[
            _FakeConnector(id=_SHOPIFY_MCP_UUID, provider="shopify_mcp"),
            _FakeConnector(id=_NETSUITE_MCP_UUID, provider="netsuite_mcp"),
        ],
    )
    names = [t["name"] for t in filtered]
    assert names == ["netsuite_suiteql", _NS_EXT_TOOL]


def test_chose_shopify_keeps_shopify_ext_tools():
    """ext__<shopify_uuid>__shopify_query must SURVIVE when chose=shopify."""
    tools = [
        {"name": _SHOPIFY_EXT_TOOL, "description": "..."},
        {"name": _NS_EXT_TOOL, "description": "..."},
    ]
    filtered = filter_tools_for_chosen_source(
        tools,
        "shopify",
        active_connectors=[
            _FakeConnector(id=_SHOPIFY_MCP_UUID, provider="shopify_mcp"),
            _FakeConnector(id=_NETSUITE_MCP_UUID, provider="netsuite_mcp"),
        ],
    )
    names = [t["name"] for t in filtered]
    assert names == [_SHOPIFY_EXT_TOOL]


def test_chose_stripe_with_ext_tools():
    """ext__<stripe_uuid>__stripe_query must SURVIVE when chose=stripe.

    Round 8 Bug 2: bare 'stripe' (REST connections.provider) no longer
    canonicalizes — only stripe_mcp does. The ext__ tools come from the
    MCP connector regardless.
    """
    tools = [
        {"name": _STRIPE_EXT_TOOL, "description": "..."},
        {"name": _NS_EXT_TOOL, "description": "..."},
    ]
    filtered = filter_tools_for_chosen_source(
        tools,
        "stripe",
        active_connectors=[
            _FakeConnector(id=_STRIPE_MCP_UUID, provider="stripe_mcp"),
            _FakeConnector(id=_NETSUITE_MCP_UUID, provider="netsuite_mcp"),
        ],
    )
    names = [t["name"] for t in filtered]
    assert names == [_STRIPE_EXT_TOOL]


def test_no_active_connectors_falls_back_to_prefix():
    """Backward compat: when ``active_connectors=None``, non-ext__ tools still
    follow the prefix rule. ext__ tools are dropped (fail-closed) since we
    can't tell which provider they belong to."""
    tools = [
        {"name": "netsuite_suiteql", "description": "..."},
        {"name": _NS_EXT_TOOL, "description": "..."},
        {"name": "pivot_query_result", "description": "..."},
    ]
    filtered = filter_tools_for_chosen_source(tools, "netsuite", active_connectors=None)
    names = [t["name"] for t in filtered]
    # Non-ext__ tools follow the prefix; ext__ tools drop fail-closed.
    assert "netsuite_suiteql" in names
    assert "pivot_query_result" in names
    assert _NS_EXT_TOOL not in names


def test_ext_tool_for_unknown_uuid_is_dropped():
    """If an ext__ tool's UUID isn't in active_connectors, drop it (fail-closed)."""
    orphan_uuid = uuid.uuid4()
    orphan_tool = _ext_tool_name(orphan_uuid, "ns_getRecord")
    tools = [
        {"name": orphan_tool, "description": "..."},
        {"name": "netsuite_suiteql", "description": "..."},
    ]
    filtered = filter_tools_for_chosen_source(
        tools,
        "netsuite",
        active_connectors=[
            _FakeConnector(id=_NETSUITE_MCP_UUID, provider="netsuite_mcp"),
        ],
    )
    names = [t["name"] for t in filtered]
    assert names == ["netsuite_suiteql"]
