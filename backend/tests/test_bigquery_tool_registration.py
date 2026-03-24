"""Tests that BigQuery tools are properly registered and discoverable."""


class TestBigqueryToolRegistration:
    def test_bigquery_sql_in_registry(self):
        from app.mcp.registry import TOOL_REGISTRY

        assert "bigquery.sql" in TOOL_REGISTRY

    def test_bigquery_schema_in_registry(self):
        from app.mcp.registry import TOOL_REGISTRY

        assert "bigquery.schema" in TOOL_REGISTRY

    def test_bigquery_cost_estimate_in_registry(self):
        from app.mcp.registry import TOOL_REGISTRY

        assert "bigquery.cost_estimate" in TOOL_REGISTRY

    def test_bigquery_tools_in_allowlist(self):
        from app.services.chat.nodes import ALLOWED_CHAT_TOOLS

        assert "bigquery.sql" in ALLOWED_CHAT_TOOLS
        assert "bigquery.schema" in ALLOWED_CHAT_TOOLS
        assert "bigquery.cost_estimate" in ALLOWED_CHAT_TOOLS

    def test_bigquery_sql_params_schema(self):
        from app.mcp.registry import TOOL_REGISTRY

        schema = TOOL_REGISTRY["bigquery.sql"]["params_schema"]
        assert "query" in schema
        assert schema["query"]["required"] is True
        assert "max_rows" in schema

    def test_tool_names_in_local_definitions(self):
        from app.services.chat.tools import build_local_tool_definitions

        tools = build_local_tool_definitions()
        tool_names = {t["name"] for t in tools}
        assert "bigquery_sql" in tool_names
        assert "bigquery_schema" in tool_names
        assert "bigquery_cost_estimate" in tool_names

    def test_bigquery_tools_in_security_allowlist(self):
        """Both allowlists must include BigQuery tools."""
        from app.services.chat.nodes import ALLOWED_CHAT_TOOLS as ORCH_ALLOWLIST

        assert "bigquery.sql" in ORCH_ALLOWLIST
        assert "bigquery.schema" in ORCH_ALLOWLIST
        assert "bigquery.cost_estimate" in ORCH_ALLOWLIST
