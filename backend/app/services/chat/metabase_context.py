"""Attach Metabase analysis skills to the final, connector-gated tool inventory.

Metabase's native tools have generic names (search, query, read_resource).
Identify their connector when building tool definitions, never by those names
alone. This module adds guidance only; it does not grant tools or permissions.
"""

from __future__ import annotations

from urllib.parse import urlsplit

METABASE_SKILL_SLUGS = ("metabase_bi", "metabase_sql")
METABASE_TOOL_TAG = "metabase_mcp"


def is_metabase_connector(connector) -> bool:
    provider = getattr(connector, "provider", None)
    if provider in ("metabase", "metabase_mcp"):
        return True
    if provider != "custom":
        return False
    metadata = getattr(connector, "metadata_json", None)
    if isinstance(metadata, dict) and metadata.get("oauth_provider") == "metabase":
        return True
    server_url = getattr(connector, "server_url", None)
    if not isinstance(server_url, str):
        return False
    try:
        return urlsplit(server_url).path.rstrip("/") == "/api/metabase-mcp"
    except ValueError:
        return False


def metabase_tool_names(tool_definitions: list[dict]) -> set[str]:
    """Exact Metabase tool names identified by the locally stamped source tag."""
    from app.services.chat.tools import parse_external_tool_name

    return {
        tool["name"]
        for tool in tool_definitions
        if (tool.get("description") or "").startswith(f"[{METABASE_TOOL_TAG}]")
        and parse_external_tool_name(tool.get("name", "")) is not None
    }


def build_metabase_skill_context(tool_definitions: list[dict], *, template: str = "") -> str:
    """Load both skills when Metabase tools survived this turn's filtering.

    The tag is stamped locally by tools._connector_tag, before the remote tool's
    description. Generic tool names or remote descriptions mentioning Metabase
    must not activate this context for an unrelated connector.
    """
    from app.services.chat.skills import get_skill_instructions
    from app.services.chat.tools import parse_external_tool_name

    connector_tools: dict[str, list[str]] = {}
    for name in metabase_tool_names(tool_definitions):
        parsed = parse_external_tool_name(name)
        if parsed is not None:
            connector_tools.setdefault(parsed[0].hex, []).append(name)
    if not connector_tools:
        return ""

    parts = [
        "<metabase_analysis_context>",
        "Metabase is connected. Apply the following BI and SQL skills to questions about "
        "Metabase or its Solidus database, including follow-up questions. Explicit requests "
        "for another source still take precedence. For Metabase analysis, these skills "
        "override generic NetSuite routing, SuiteQL syntax, one-query budgets, and "
        "stop-after-the-first-result instructions. Finish discovery and validation before concluding.",
        "Use the exact available tool names and input schemas. Keep discovery, query handles, "
        "database IDs, and execution on the SAME connector:",
    ]
    for connector_id, names in sorted(connector_tools.items()):
        parts.append(f"- Connector {connector_id}: {', '.join(sorted(names))}")
    for slug in METABASE_SKILL_SLUGS:
        instructions = get_skill_instructions(slug)
        # Explicit slash skills are already injected by UnifiedAgent.
        if instructions and instructions not in template:
            parts.append(instructions)
    parts.append("</metabase_analysis_context>")
    return "\n\n".join(parts)
