"""CI invariant: no tool name appears in any agent's assembled prompt
string that isn't present in build_all_tool_definitions for a tenant
with every connector type enabled.

If this test fails, someone reintroduced a hardcoded tool reference in
a prompt and the LLM will either claim a capability it doesn't have or
hide a capability it does have.
"""

from __future__ import annotations

import re

from app.services.chat import prompts
from app.services.chat.agents import unified_agent
from app.services.chat.tool_inventory import build_tool_inventory_block

# Regex that matches plausible tool names in a prompt string.
# (snake_case with at least 2 underscores → matches "netsuite_suiteql",
#  "workspace_propose_patch" but skips "tool_inventory" and similar
#  generic identifiers — though the whitelist below covers known noise.)
_TOOL_NAME_RE = re.compile(r"\b([a-z][a-z0-9_]*_[a-z][a-z0-9_]*)\b")

# These look like tool names but are XML/section markers in the prompts.
# Add to this set when a legitimate non-tool snake_case identifier trips
# the regex (e.g. <tool_inventory>, <system_directives>).
_PROMPT_NOISE_WHITELIST: set[str] = {
    # XML section / tag names used in prompt structure
    "tool_inventory",
    "system_directives",
    "core_constraints",
    "workflow_guidance",
    "domain_rules",
    "suiteql_syntax",
    "transactionline_rules",
    "tool_selection",
    "available_tools",
    "investigation_mode",
    "agentic_workflow",
    "common_queries",
    "workspace_rules",
    "output_instructions",
    "suiteql_dialect_rules",
    # Template variable placeholders
    "data_question",
    "code_question",
    "exploration_mode",
    "table_schemas",
    "tenant_schema",
    "tenant_vernacular",
    "fiscal_calendar",
    "context_recap",
    "current_date",
    "user_timezone",
    "agent_role",
    "active_skill",
    "soul_config",
    "brand_name",
    "tenant_id",
    "user_id",
    "session_id",
    "correlation_id",
    "current_task",
    "previous_results",
    "format_rules",
    "conversation_history",
    "fy_start",
    # Prompt context block names
    "tenant_context",
    "connected_systems",
    "standard_table_schemas",
    "domain_knowledge",
    "proven_patterns",
    "learned_rules",
    "systemnote_expertise",
    # Generic prompt identifiers (not tools)
    "tool_name",
    "tool_use",
    "tool_call",
    "tool_result",
    # NetSuite report type enum values referenced in prose
    "income_statement",
    "income_statement_trend",
    "balance_sheet",
    "balance_sheet_trend",
    "trial_balance",
    # Schema/query parameter names used in examples
    "report_type",
    "subsidiary_id",
    "field_name",
    "source_id",
    "pending_qty",
    # HITL write confirmation flow identifiers (JSON response field names, not tools)
    "confirmation_required",
    # NetSuite custom field prefixes referenced as schema patterns (not tools)
    "custbody_platform",
    "custbody_shopify_order",
    "custcol_tracking",
    "custitem_fw_platform",
    "customlist_name",
    "customrecord_r_inv_processor",
    "customrecord_xxx",
}


def _extract_tool_like_names(prompt_text: str) -> set[str]:
    return set(_TOOL_NAME_RE.findall(prompt_text))


def _all_known_tool_names_for_tenant_with_every_connector() -> set[str]:
    """Hard-coded against the real tool registry.
    Update when adding a NEW tool to build_local_tool_definitions or
    expanding _CONNECTOR_GATED_TOOLS in tools.py."""
    return {
        # Local tools from ALLOWED_CHAT_TOOLS (after . → _ name sanitization)
        "netsuite_suiteql",
        "netsuite_get_metadata",
        "netsuite_refresh_metadata",
        "netsuite_connectivity",
        "netsuite_financial_report",
        "data_sample_table_read",
        "rag_search",
        "web_search",
        "report_compose",
        "workspace_list_files",
        "workspace_read_file",
        "workspace_search",
        "workspace_propose_patch",
        "suitescript_sync",
        "bigquery_sql",
        "bigquery_schema",
        "bigquery_cost_estimate",
        "pivot_query_result",
        "cross_source_query",
        "tenant_save_learned_rule",
        "pricing_config_read",
        "pricing_convert",
        "pricing_export",
        "pricing_revise",
        "pricing_to_sheets",
        "sheets_create",
        "sheets_write_range",
        "sheets_read_range",
        "metric_resolve",
        "metric_compute",
        # Added by build_all_tool_definitions (result_reference_tool)
        "reference_previous_result",
    }


class TestPromptToolSync:
    def test_base_system_prompt_references_no_unknown_tool(self):
        unknown = (
            _extract_tool_like_names(prompts.SYSTEM_PROMPT)
            - _all_known_tool_names_for_tenant_with_every_connector()
            - _PROMPT_NOISE_WHITELIST
        )
        assert not unknown, (
            f"Base prompt references tool-like names not in the schema: {sorted(unknown)}. "
            "Either add them to build_local_tool_definitions, remove them from the prompt, "
            "or whitelist them in _PROMPT_NOISE_WHITELIST if they're legitimate non-tool identifiers."
        )

    def test_unified_agent_prompt_references_no_unknown_tool(self):
        text = unified_agent._SYSTEM_PROMPT
        unknown = (
            _extract_tool_like_names(text)
            - _all_known_tool_names_for_tenant_with_every_connector()
            - _PROMPT_NOISE_WHITELIST
        )
        assert not unknown, (
            f"UnifiedAgent prompt references tool-like names not in the schema: {sorted(unknown)}. "
            "Either add them to build_local_tool_definitions, remove them from the prompt, "
            "or whitelist them in _PROMPT_NOISE_WHITELIST."
        )

    def test_tool_inventory_helper_surfaces_every_known_tool(self):
        # Round-trip: if you feed every known tool in, every known tool
        # must appear in the rendered block.
        tools = [
            {"name": n, "description": f"desc for {n}", "category": "other"}
            for n in _all_known_tool_names_for_tenant_with_every_connector()
        ]
        block = build_tool_inventory_block(tools)
        for name in _all_known_tool_names_for_tenant_with_every_connector():
            assert name in block, f"{name} missing from rendered inventory block"
