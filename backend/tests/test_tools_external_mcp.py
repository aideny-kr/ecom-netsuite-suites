"""Tests for external MCP tool definition building.

Regression: 2026-04-09 — the backend was hard-truncating external MCP tool
descriptions at 1024 chars in `build_external_tool_definitions`. Oracle's
`ns_runCustomSuiteQL` tool ships with ~4,800 chars of SuiteQL dialect rules
(string concatenation, date literals, ANSI joins, no CTE support, etc.)
baked into the description — ~80% of that expert knowledge was being thrown
away before the agent ever saw it. This directly degraded the agent's ability
to construct working queries. The new cap is 8,192 chars, enough to preserve
Oracle's full description while still bounding against misbehaving MCP
servers.
"""

import uuid

from app.services.chat.tools import build_external_tool_definitions


class _FakeConnector:
    """Minimal stand-in for an `McpConnector` with `discovered_tools`."""

    def __init__(self, tools: list[dict], provider: str = "netsuite"):
        self.id = uuid.UUID("12345678-1234-1234-1234-123456789012")
        self.provider = provider
        self.discovered_tools = tools


def test_short_description_passes_through():
    conn = _FakeConnector(
        [{"name": "short_tool", "description": "A short description", "input_schema": {"type": "object", "properties": {}}}]
    )
    tools = build_external_tool_definitions([conn])
    assert len(tools) == 1
    # The [provider] prefix is prepended but nothing is truncated
    assert tools[0]["description"] == "[netsuite] A short description"


def test_oracle_full_suiteql_description_preserved():
    """Oracle's ns_runCustomSuiteQL ships ~4,800 chars of dialect rules.
    This test simulates that length and asserts nothing is lost above 1024."""
    oracle_desc = (
        "Runs a custom SuiteQL query. "
        "Bold: String concatenation uses the || operator. "
        "Bold: WITH/CTE not supported — inline subqueries. "
        "Bold: Date literals use TO_DATE('YYYY-MM-DD'). "
        "Bold: ANSI JOIN required, no (+) outer join operator. "
        "Bold: Mixed join syntax disallowed. "
    ) * 20  # roughly 5,000 chars
    assert len(oracle_desc) > 1024, "test setup: description must be > old 1024 cap"
    assert len(oracle_desc) < 8192, "test setup: description must fit under new 8192 cap"

    conn = _FakeConnector(
        [{"name": "ns_runCustomSuiteQL", "description": oracle_desc, "input_schema": {"type": "object", "properties": {}}}]
    )
    tools = build_external_tool_definitions([conn])
    assert len(tools) == 1

    out_desc = tools[0]["description"]
    # Full description should survive (plus the `[netsuite] ` prefix)
    assert len(out_desc) > 1024, (
        f"Description was truncated to {len(out_desc)} chars — "
        f"Oracle's SuiteQL dialect rules are being thrown away"
    )
    # Key dialect rules must appear in the output
    assert "|| operator" in out_desc
    assert "TO_DATE" in out_desc
    assert "ANSI JOIN" in out_desc


def test_description_capped_at_8192_for_misbehaving_server():
    """Protects against a misbehaving MCP server sending a massive description."""
    bloated = "X" * 20_000
    conn = _FakeConnector(
        [{"name": "bloated_tool", "description": bloated, "input_schema": {"type": "object", "properties": {}}}]
    )
    tools = build_external_tool_definitions([conn])
    assert len(tools[0]["description"]) <= 8192
    # The safety cap should kick in
    assert len(tools[0]["description"]) == 8192


def test_tool_name_format_unchanged():
    conn = _FakeConnector(
        [{"name": "ns_runCustomSuiteQL", "description": "x", "input_schema": {"type": "object", "properties": {}}}]
    )
    tools = build_external_tool_definitions([conn])
    # ext__{32-char hex}__{raw_name}
    assert tools[0]["name"].startswith("ext__")
    assert tools[0]["name"].endswith("__ns_runCustomSuiteQL")
    assert len(tools[0]["name"].split("__")[1]) == 32  # hex of UUID
