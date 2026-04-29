"""Verify resume turn filters tool inventory to chosen source."""

from app.services.chat.plan_mode.short_circuit import filter_tools_for_chosen_source

_TOOLS = [
    {"name": "bigquery_sql", "description": "..."},
    {"name": "bigquery_schema", "description": "..."},
    {"name": "netsuite_suiteql", "description": "..."},
    {"name": "ext__abc__ns_getRecord", "description": "..."},
    {"name": "shopify_orders", "description": "..."},
    {"name": "stripe_payouts", "description": "..."},
    {"name": "pivot_query_result", "description": "..."},
    {"name": "docs_create", "description": "..."},
    {"name": "drive_read_doc", "description": "..."},
    {"name": "clarify", "description": "..."},
    {"name": "reference_previous_result", "description": "..."},
]


def test_chose_netsuite_drops_other_data_sources():
    filtered = filter_tools_for_chosen_source(_TOOLS, "netsuite")
    names = {t["name"] for t in filtered}
    assert "bigquery_sql" not in names
    assert "bigquery_schema" not in names
    assert "shopify_orders" not in names
    assert "stripe_payouts" not in names
    assert "netsuite_suiteql" in names
    assert "ext__abc__ns_getRecord" in names


def test_cross_source_tools_always_included():
    """pivot, docs_create, drive_read_doc, clarify, reference_previous_result work across sources."""
    filtered = filter_tools_for_chosen_source(_TOOLS, "netsuite")
    names = {t["name"] for t in filtered}
    assert "pivot_query_result" in names
    assert "docs_create" in names
    assert "drive_read_doc" in names
    assert "clarify" in names
    assert "reference_previous_result" in names


def test_chose_bigquery_drops_netsuite():
    filtered = filter_tools_for_chosen_source(_TOOLS, "bigquery")
    names = {t["name"] for t in filtered}
    assert "netsuite_suiteql" not in names
    assert "ext__abc__ns_getRecord" not in names
    assert "bigquery_sql" in names
    assert "bigquery_schema" in names


def test_chose_shopify():
    filtered = filter_tools_for_chosen_source(_TOOLS, "shopify")
    names = {t["name"] for t in filtered}
    assert "shopify_orders" in names
    assert "netsuite_suiteql" not in names
    assert "bigquery_sql" not in names


def test_unknown_source_keeps_only_cross_source():
    """Defensive: unknown source returns just the cross-source tools (no data tools)."""
    filtered = filter_tools_for_chosen_source(_TOOLS, "unknown_source")
    names = {t["name"] for t in filtered}
    assert "netsuite_suiteql" not in names
    assert "bigquery_sql" not in names
    # Cross-source tools still allowed (agent can still summarize prior data, etc.)
    assert "pivot_query_result" in names
    assert "clarify" in names


def test_empty_input():
    assert filter_tools_for_chosen_source([], "netsuite") == []


def test_preserves_order():
    """Filter is order-stable — important for prompt cache consistency."""
    filtered = filter_tools_for_chosen_source(_TOOLS, "netsuite")
    assert filtered == [
        {"name": "netsuite_suiteql", "description": "..."},
        {"name": "ext__abc__ns_getRecord", "description": "..."},
        {"name": "pivot_query_result", "description": "..."},
        {"name": "docs_create", "description": "..."},
        {"name": "drive_read_doc", "description": "..."},
        {"name": "clarify", "description": "..."},
        {"name": "reference_previous_result", "description": "..."},
    ]
