"""Unit tests for build_tool_inventory_block.

The helper is the single producer of the <available_tools> block that
gets injected into every agent's system prompt. It must faithfully
reflect the real tool schema so the LLM's self-model never disagrees
with what it can actually call.
"""

from app.services.chat.tool_inventory import build_tool_inventory_block


def _tool(name: str, description: str, category: str = "other") -> dict:
    return {"name": name, "description": description, "category": category}


class TestBuildToolInventoryBlock:
    def test_empty_list_returns_empty_string(self):
        assert build_tool_inventory_block([]) == ""

    def test_single_tool_renders_name_and_description(self):
        block = build_tool_inventory_block(
            [_tool("netsuite_suiteql", "Run SuiteQL against NetSuite.", "data_table")]
        )
        assert "<available_tools>" in block
        assert "</available_tools>" in block
        assert "netsuite_suiteql" in block
        assert "Run SuiteQL against NetSuite." in block

    def test_bigquery_tools_trigger_dialect_warning(self):
        block = build_tool_inventory_block(
            [
                _tool("netsuite_suiteql", "SuiteQL.", "data_table"),
                _tool("bigquery_sql", "BigQuery SQL.", "bigquery"),
            ]
        )
        assert "BigQuery uses Standard SQL" in block
        assert "SuiteQL" in block

    def test_no_bigquery_warning_when_no_bigquery_tool(self):
        block = build_tool_inventory_block(
            [_tool("netsuite_suiteql", "SuiteQL.", "data_table")]
        )
        assert "BigQuery" not in block

    def test_external_mcp_tool_listed_with_prefix_hint(self):
        block = build_tool_inventory_block(
            [_tool("ext__shopify_list_orders", "[shopify_mcp] List Shopify orders.", "other")]
        )
        assert "ext__shopify_list_orders" in block
        assert "shopify_mcp" in block

    def test_output_is_deterministic_for_same_input(self):
        tools = [
            _tool("netsuite_suiteql", "SuiteQL.", "data_table"),
            _tool("bigquery_sql", "BQ.", "bigquery"),
            _tool("rag_search", "Search docs.", "rag"),
        ]
        assert build_tool_inventory_block(tools) == build_tool_inventory_block(tools)
