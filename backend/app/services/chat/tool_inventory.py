# backend/app/services/chat/tool_inventory.py
"""Single source of truth for the <available_tools> block in agent prompts.

Every agent's system prompt must reflect exactly the tool schema that will
be sent to the LLM. If the prompt enumerates different tools than the
schema, the LLM trusts the prompt and denies capabilities it actually has
(or hallucinates capabilities it doesn't). Never hardcode tool names in a
prompt string — call this helper at prompt-assembly time.
"""

from __future__ import annotations

_BIGQUERY_HINT = (
    "\nBIGQUERY DATA WAREHOUSE:\n"
    "This tenant has BigQuery connected. Use `bigquery_sql` for ad-hoc queries, "
    "`bigquery_schema` to discover datasets/tables, and `bigquery_cost_estimate` "
    "for dry-run cost checks. BigQuery uses Standard SQL — backtick identifiers "
    "(`dataset.table`) and LIMIT (NOT FETCH FIRST). Do not confuse BigQuery SQL "
    "with SuiteQL — they are different dialects."
)


def build_tool_inventory_block(tool_definitions: list[dict]) -> str:
    """Render the tool schema as a prompt block the LLM can trust.

    Format: an XML block listing every tool's name and description, followed
    by an optional BigQuery dialect hint when BigQuery tools are present.
    Returns "" for an empty tool list so callers can unconditionally inject
    the result into a prompt template.
    """
    if not tool_definitions:
        return ""

    lines: list[str] = ["<available_tools>"]
    for td in tool_definitions:
        name = td.get("name", "")
        description = td.get("description", "")
        lines.append(f"- {name}: {description}")
    lines.append("</available_tools>")

    has_bigquery = any(td.get("name", "").startswith("bigquery_") for td in tool_definitions)
    if has_bigquery:
        lines.append(_BIGQUERY_HINT)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP execution guidance
# ---------------------------------------------------------------------------
# When the tenant has external MCP tools (Oracle NetSuite MCP), the LLM
# benefits from explicit per-tool guidance about parameters and execution
# preference. This is *not* tool enumeration (that's build_tool_inventory_block);
# it's behavioral guidance the prompt author always wants the model to see
# when those tools are available. Lives here because it travels with the
# tool inventory — both are derived from the same tool_definitions input.

_MCP_TOOL_PATTERNS = {
    "runreport": "REPORTS",
    "runsavedsearch": "SAVED_SEARCHES",
    "listallreports": "REPORT_DISCOVERY",
    "listsavedsearches": "SEARCH_DISCOVERY",
    "suiteql": "SUITEQL",
    "getsuiteqlmetadata": "METADATA",
    "getsubsidiaries": "SUBSIDIARIES",
}


def _categorize_mcp_tool(tool_name: str) -> str | None:
    """Return the MCP category code for a tool name, or None for non-MCP tools."""
    if not tool_name.startswith("ext__"):
        return None
    lowered = tool_name.lower()
    for pattern, category in _MCP_TOOL_PATTERNS.items():
        if pattern in lowered:
            return category
    return None


def build_mcp_execution_guidance(tool_definitions: list[dict]) -> str:
    """Emit per-MCP-tool guidance + EXECUTION PRIORITY block.

    Returns "" when no recognized Oracle NetSuite MCP tools are present.
    Includes a separate OTHER CONNECTED SYSTEM TOOLS section for any ext__
    tool that doesn't match the Oracle MCP patterns (e.g. Shopify, Stripe).
    """
    matched: dict[str, str] = {}  # category → tool_name
    other_ext_tools: list[dict] = []

    for td in tool_definitions:
        name = td.get("name", "")
        if not name.startswith("ext__"):
            continue
        category = _categorize_mcp_tool(name)
        if category:
            matched[category] = name
        else:
            other_ext_tools.append(td)

    if not matched and not other_ext_tools:
        return ""

    sections: list[str] = []

    if matched:
        sections.append("\n\nNETSUITE MCP TOOLS (connect directly to NetSuite — prefer these for execution):")

        if "REPORTS" in matched:
            sections.append(
                f"\n• FINANCIAL REPORTS: `{matched['REPORTS']}`"
                "\n  For Income Statement, Balance Sheet, Trial Balance, Aging, GL, etc."
                '\n  Parameters: {"reportId": <number>, "dateTo": "YYYY-MM-DD", "dateFrom": "YYYY-MM-DD", "subsidiaryId": <number>}'
                "\n  → reportId must be a NUMBER (e.g. -200), not a string."
                "\n  → dateTo is always required. dateFrom is required for P&L, optional for Balance Sheet."
                "\n  → Call ns_listAllReports FIRST to get reportId and check has_subsidiary_filter / as_of_date_format."
                "\n  → If has_subsidiary_filter=true, call ns_getSubsidiaries and pass subsidiaryId."
                "\n  → NetSuite handles sign conventions, consolidation, currency natively."
            )

        if "REPORT_DISCOVERY" in matched:
            sections.append(
                f"\n• DISCOVER REPORTS: `{matched['REPORT_DISCOVERY']}`"
                "\n  Lists all available reports with IDs. Call FIRST before ns_runReport."
            )

        if "SAVED_SEARCHES" in matched:
            sections.append(
                f"\n• SAVED SEARCHES: `{matched['SAVED_SEARCHES']}`"
                "\n  Run pre-built searches with custom columns, formulas, and filters."
                '\n  Parameters: {"savedSearchId": "<id>", "filters": [...]}'
            )

        if "SEARCH_DISCOVERY" in matched:
            sections.append(
                f"\n• DISCOVER SEARCHES: `{matched['SEARCH_DISCOVERY']}`"
                "\n  Lists saved searches. Use when user asks 'do we have a report for X?'"
            )

        if "SUITEQL" in matched:
            sections.append(
                f"\n• SUITEQL (MCP): `{matched['SUITEQL']}`"
                "\n  Ad-hoc SuiteQL queries inside NetSuite. Prefer over local netsuite_suiteql."
                '\n  Parameters: {"sqlQuery": "SELECT ...", "description": "..."}'
                "\n  STILL FOLLOW all <suiteql_dialect_rules> — they apply to MCP SuiteQL too."
            )

        if "METADATA" in matched:
            sections.append(
                f"\n• SCHEMA (MCP): `{matched['METADATA']}`"
                "\n  Ground-truth column metadata from NetSuite. Use alongside netsuite_get_metadata."
            )

        if "SUBSIDIARIES" in matched:
            sections.append(
                f"\n• SUBSIDIARIES: `{matched['SUBSIDIARIES']}`\n  Subsidiary hierarchy with base currencies."
            )

        sections.append(
            "\n\nEXECUTION PRIORITY (pick the first that fits):"
            "\n  Financial statements → ns_runReport"
            "\n  Pre-built business reports → ns_runSavedSearch"
            "\n  Ad-hoc data queries → ns_runCustomSuiteQL (MCP) → netsuite_suiteql (local fallback)"
            "\n  Schema verification → ns_getSuiteQLMetadata + netsuite_get_metadata (use both)"
            "\n  Documentation/how-to → rag_search → web_search"
            "\n"
            "\nIMPORTANT: MCP tools handle EXECUTION. But you still have rich tenant context"
            "\n(entity vernacular, custom field schema, learned rules, proven patterns) injected"
            "\ninto your system prompt. USE THIS CONTEXT when constructing parameters for MCP tools."
            "\nFor example, if <tenant_vernacular> resolves 'FW' to subsidiary ID 5, pass"
            "\nsubsidiaryId: 5 to ns_runReport."
        )

    if other_ext_tools:
        sections.append("\n\nOTHER CONNECTED SYSTEM TOOLS:")
        for td in other_ext_tools:
            sections.append(f"- {td.get('name', '')}: {td.get('description', '')}")
        sections.append(
            "\nUse these tools when the user's question relates to the system they belong to. "
            "Check the tool description prefix (e.g., [shopify_mcp]) to identify which system."
        )

    return "".join(sections)
