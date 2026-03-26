"""Tests for pivot tool BigQuery dialect support."""


def test_pivot_tool_registry_renamed():
    from app.mcp.registry import TOOL_REGISTRY

    assert "pivot.query_result" in TOOL_REGISTRY
    assert "netsuite.pivot_query_result" not in TOOL_REGISTRY


def test_pivot_tool_has_dialect_param():
    from app.mcp.registry import TOOL_REGISTRY

    schema = TOOL_REGISTRY["pivot.query_result"]["params_schema"]
    assert "dialect" in schema


def test_pivot_in_allowed_tools():
    from app.services.chat.nodes import ALLOWED_CHAT_TOOLS

    assert "pivot.query_result" in ALLOWED_CHAT_TOOLS
    assert "netsuite.pivot_query_result" not in ALLOWED_CHAT_TOOLS
