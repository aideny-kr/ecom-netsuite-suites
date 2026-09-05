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
    "report",
    "control",
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
    "metric_compute": "data_table",
    "metric.compute": "data_table",
    "report_compose": "report",
    "report.compose": "report",
    "escalate_reasoning": "control",
    # Local celigo.* chat tools (spec docs/superpowers/specs/2026-09-04-celigo-chat-access.md
    # §6, task 3) — their output is row data over a synced snapshot, same interception need as
    # netsuite_suiteql; both spellings so a lookup on either the dotted registry name or the
    # underscored LLM-facing name lands here. See `is_celigo_source` below for why "data_table"
    # must NOT be read as "NetSuite" for these four.
    "celigo_integrations": "data_table",
    "celigo.integrations": "data_table",
    "celigo_flows": "data_table",
    "celigo.flows": "data_table",
    "celigo_flow_steps": "data_table",
    "celigo.flow_steps": "data_table",
    "celigo_flow_errors": "data_table",
    "celigo.flow_errors": "data_table",
}


def is_celigo_tool(tool_name: str) -> bool:
    """Whether *tool_name* is an external Celigo read tool.

    ``categorize`` maps these to ``data_table`` because their output is row data
    that must be intercepted into a table rather than restated by the model. But
    ``data_table`` also means "NetSuite" to ``_compute_source_pin_update``, and
    Celigo is neither NetSuite nor BigQuery -- so that consumer needs to tell the
    two apart, and asking the same question twice in two places is how they
    drift. Both call this.

    Delegates to ``celigo_tool_policy.is_read_only_celigo_tool``, which is the
    single source of truth for the Celigo tool catalog; there is no second list
    here.
    """
    if not tool_name.startswith("ext__"):
        return False
    from app.services.chat.celigo_tool_policy import is_read_only_celigo_tool
    from app.services.chat.tools import parse_external_tool_name

    parsed = parse_external_tool_name(tool_name)
    return parsed is not None and is_read_only_celigo_tool(parsed[1])


def is_celigo_source(tool_name: str) -> bool:
    """Whether *tool_name* is ANY Celigo tool -- the external hosted-API
    family (``is_celigo_tool``, ``ext__*``) or this repo's own local
    ``celigo.*``/``celigo_*`` chat tools (spec
    ``docs/superpowers/specs/2026-09-04-celigo-chat-access.md`` §6, task 3).

    ``is_celigo_tool`` alone answers "is this the external connector's read
    tool" -- it deliberately returns ``False`` for a local name, because a
    local dotted tool never goes through ``parse_external_tool_name``. That
    is the right answer for its own callers (``categorize``'s mutation-guard
    branch only ever sees ``ext__`` names), but `_compute_source_pin_update`
    (``chat/orchestrator.py``) needs the WIDER question -- "is this turn's
    data Celigo, so it must not be pinned to NetSuite or BigQuery" -- and a
    local ``celigo.flows``/``celigo_flows`` call is exactly as much "not
    NetSuite" as the external one. This is the single place both spellings
    of both families are recognized, so a caller never has to enumerate them
    itself."""
    return is_celigo_tool(tool_name) or tool_name.startswith("celigo_") or tool_name.startswith("celigo.")


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
        # Lazy imports to avoid circular dependency (tools.py pulls in the MCP
        # registry/server; mutation_guard.py itself lazy-imports tools.py for
        # the same reason).
        from app.services.chat.mutation_guard import is_mutation_tool

        if is_mutation_tool(tool_name):
            return "mutation"

        # Celigo's enumerated read catalog (celigo_tool_policy._READ_TOOLS via
        # is_celigo_tool -- the single source of truth, not a second hardcoded
        # list here) returns row data the same shape SuiteQL does. Without this,
        # these tools fall through to "other", skip _intercept_tool_result
        # entirely, and the LLM restates raw counts from the tool's JSON in
        # prose (feedback_no_llm_numbers).
        #
        # NOTE for anyone reading "data_table" as "NetSuite": it is not, and
        # _compute_source_pin_update re-checks is_celigo_tool for exactly that
        # reason.
        if is_celigo_tool(tool_name):
            return "data_table"

        lowered = tool_name.lower()
        if "runreport" in lowered:
            return "financial"
        if "runcustomsuiteql" in lowered or "runsuiteql" in lowered:
            return "data_table"

    return "other"
