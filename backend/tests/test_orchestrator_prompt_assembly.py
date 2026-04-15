"""The orchestrator must resolve {{TOOL_INVENTORY}} with the real
tool schema. Drift between prompt and schema is what caused the
BigQuery-not-available bug on 2026-04-14."""

from app.services.chat.orchestrator import _assemble_system_prompt


def _tool(name: str, description: str, category: str = "other") -> dict:
    return {"name": name, "description": description, "category": category}


class TestAssembleSystemPrompt:
    def test_placeholder_replaced_with_tool_names(self):
        template = "BEFORE\n{{TOOL_INVENTORY}}\nAFTER"
        result = _assemble_system_prompt(
            template=template,
            tool_definitions=[_tool("netsuite_suiteql", "SuiteQL.", "data_table")],
        )
        assert "{{TOOL_INVENTORY}}" not in result
        assert "netsuite_suiteql" in result
        assert "BEFORE" in result and "AFTER" in result

    def test_bigquery_only_when_bigquery_tool_present(self):
        template = "{{TOOL_INVENTORY}}"
        with_bq = _assemble_system_prompt(
            template=template,
            tool_definitions=[_tool("bigquery_sql", "BQ.", "bigquery")],
        )
        without_bq = _assemble_system_prompt(
            template=template,
            tool_definitions=[_tool("netsuite_suiteql", "SQL.", "data_table")],
        )
        assert "BigQuery uses Standard SQL" in with_bq
        assert "BigQuery" not in without_bq

    def test_placeholder_absent_template_unchanged(self):
        # Safety: if a template doesn't use the placeholder, assembly is a no-op.
        template = "No placeholder here."
        assert _assemble_system_prompt(template=template, tool_definitions=[]) == template

    def test_mcp_tool_produces_execution_priority_in_assembled_prompt(self):
        template = "{{TOOL_INVENTORY}}"
        result = _assemble_system_prompt(
            template=template,
            tool_definitions=[
                _tool("ext__c__ns_runReport", "[ns] run report", "financial"),
            ],
        )
        assert "EXECUTION PRIORITY" in result
        assert "FINANCIAL REPORTS" in result
