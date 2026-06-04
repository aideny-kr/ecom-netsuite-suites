# backend/app/services/chat/tool_categories.py
"""Map tool names to high-level capability categories.

Categories drive: (1) tool-result interception (financial reports + data
tables use different SSE events), (2) confidence scoring (did the agent
actually call a data tool?), and (3) source-pin auto-update (which data
source was just queried?). Centralized here so adding a new tool requires
exactly one edit — declaring its category in tools.py."""

from __future__ import annotations

from typing import Literal

Category = Literal[
    "financial",
    "data_table",
    "bigquery",
    "rag",
    "workspace",
    "mutation",
    "sheets",
    "pricing",
    "other",
]

_EXACT: dict[str, Category] = {
    "netsuite_suiteql": "data_table",
    "netsuite.suiteql": "data_table",
    "pivot_query_result": "data_table",
    "pivot.query_result": "data_table",
    "cross_source_query": "data_table",
    "cross_source.query": "data_table",
    "netsuite_financial_report": "financial",
    "netsuite.financial_report": "financial",
    "bigquery_sql": "bigquery",
    "bigquery.sql": "bigquery",
    "bigquery_schema": "bigquery",
    "bigquery_cost_estimate": "bigquery",
    "rag_search": "rag",
    "web_search": "rag",
    "workspace_list_files": "workspace",
    "workspace_read_file": "workspace",
    "workspace_search": "workspace",
    "workspace_propose_patch": "workspace",
    "sheets_create": "sheets",
    "sheets.create": "sheets",
    "sheets_write_range": "sheets",
    "sheets.write_range": "sheets",
    "sheets_read_range": "sheets",
    "sheets.read_range": "sheets",
    "docs_create": "sheets",
    "docs.create": "sheets",
    "drive.read_doc": "rag",
    "drive_read_doc": "rag",
    "pricing_convert": "pricing",
    "pricing.convert": "pricing",
    "pricing_export": "pricing",
    "pricing.export": "pricing",
    "pricing_revise": "pricing",
    "pricing.revise": "pricing",
    "pricing_config_read": "pricing",
    "pricing.config_read": "pricing",
    "pricing_config_update": "pricing",
    "pricing.config_update": "pricing",
    "pricing_to_sheets": "pricing",
    "pricing.to_sheets": "pricing",
}


def categorize(tool_name: str) -> Category:
    """Return the category for a tool name.

    Handles both the LLM-facing underscore name (bigquery_sql) and the
    registry dotted name (bigquery.sql). External MCP tools carry the
    tool kind in the middle of the name (ext__<kind>__<connector_id>)
    and are categorized from that kind when recognizable.
    """
    if tool_name in _EXACT:
        return _EXACT[tool_name]

    if tool_name.startswith("ext__"):
        # Lazy import to avoid circular dependency.
        from app.services.chat.mutation_guard import is_mutation_tool

        if is_mutation_tool(tool_name):
            return "mutation"

        lowered = tool_name.lower()
        if "runreport" in lowered:
            return "financial"
        if "runcustomsuiteql" in lowered or "runsuiteql" in lowered:
            return "data_table"

    return "other"
