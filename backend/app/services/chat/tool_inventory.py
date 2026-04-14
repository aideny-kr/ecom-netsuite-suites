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
