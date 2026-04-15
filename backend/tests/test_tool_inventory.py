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
        block = build_tool_inventory_block([_tool("netsuite_suiteql", "Run SuiteQL against NetSuite.", "data_table")])
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
        block = build_tool_inventory_block([_tool("netsuite_suiteql", "SuiteQL.", "data_table")])
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


class TestBuildMcpExecutionGuidance:
    def test_no_mcp_tools_returns_empty(self):
        from app.services.chat.tool_inventory import build_mcp_execution_guidance

        assert (
            build_mcp_execution_guidance(
                [
                    {"name": "netsuite_suiteql", "description": "...", "category": "data_table"},
                ]
            )
            == ""
        )

    def test_ns_runreport_triggers_reports_guidance(self):
        from app.services.chat.tool_inventory import build_mcp_execution_guidance

        guidance = build_mcp_execution_guidance(
            [
                {"name": "ext__connector1__ns_runReport", "description": "[ns] Run report", "category": "financial"},
            ]
        )
        assert "FINANCIAL REPORTS" in guidance
        assert "ext__connector1__ns_runReport" in guidance
        assert "EXECUTION PRIORITY" in guidance

    def test_ns_runsavedsearch_triggers_savedsearch_guidance(self):
        from app.services.chat.tool_inventory import build_mcp_execution_guidance

        guidance = build_mcp_execution_guidance(
            [
                {"name": "ext__c1__ns_runSavedSearch", "description": "[ns] saved search", "category": "data_table"},
            ]
        )
        assert "SAVED SEARCHES" in guidance
        assert "ext__c1__ns_runSavedSearch" in guidance

    def test_other_ext_tool_listed_under_other_systems(self):
        from app.services.chat.tool_inventory import build_mcp_execution_guidance

        guidance = build_mcp_execution_guidance(
            [
                {"name": "ext__shopify_xyz__list_orders", "description": "[shopify] orders", "category": "other"},
            ]
        )
        assert "OTHER CONNECTED SYSTEM TOOLS" in guidance
        assert "ext__shopify_xyz__list_orders" in guidance

    def test_multiple_mcp_tools_combined_guidance(self):
        from app.services.chat.tool_inventory import build_mcp_execution_guidance

        guidance = build_mcp_execution_guidance(
            [
                {"name": "ext__c__ns_runReport", "description": "", "category": "financial"},
                {"name": "ext__c__ns_runCustomSuiteQL", "description": "", "category": "data_table"},
                {"name": "ext__c__ns_listAllReports", "description": "", "category": "other"},
            ]
        )
        assert "FINANCIAL REPORTS" in guidance
        assert "SUITEQL (MCP)" in guidance
        assert "DISCOVER REPORTS" in guidance
        assert "EXECUTION PRIORITY" in guidance

    def test_other_tools_render_each_on_own_line(self):
        from app.services.chat.tool_inventory import build_mcp_execution_guidance

        guidance = build_mcp_execution_guidance(
            [
                {"name": "ext__a__list_orders", "description": "[shopify] orders", "category": "other"},
                {"name": "ext__a__get_product", "description": "[shopify] product", "category": "other"},
            ]
        )
        # Bullets must be separated by newlines, not run together.
        assert "\n- ext__a__list_orders" in guidance
        assert "\n- ext__a__get_product" in guidance
