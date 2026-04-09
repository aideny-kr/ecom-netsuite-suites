"""Tests for external MCP tool definition building.

Regression: 2026-04-09 — the backend was hard-truncating external MCP tool
descriptions at 1024 chars in `build_external_tool_definitions`. Oracle's
`ns_runCustomSuiteQL` tool ships with ~4,800 chars of SuiteQL dialect rules
(string concatenation, date literals, ANSI joins, no CTE support, etc.)
baked into the description — ~80% of that expert knowledge was being thrown
away before the agent ever saw it. This directly handicapped the agent
relative to the Claude-direct + MCP baseline (our north star).

Policy: DO NOT impose a local cap on MCP tool descriptions. Any cap is a
direct restriction on the agent's capability relative to the baseline.
The Anthropic API enforces its own limits and should be the authoritative
bound, not our code.
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
    This test simulates that length and asserts the description passes
    through unchanged."""
    oracle_desc = (
        "Runs a custom SuiteQL query. "
        "Bold: String concatenation uses the || operator. "
        "Bold: WITH/CTE not supported — inline subqueries. "
        "Bold: Date literals use TO_DATE('YYYY-MM-DD'). "
        "Bold: ANSI JOIN required, no (+) outer join operator. "
        "Bold: Mixed join syntax disallowed. "
    ) * 20  # roughly 5,000 chars
    assert len(oracle_desc) > 1024, "test setup: description must be > old 1024 cap"

    conn = _FakeConnector(
        [{"name": "ns_runCustomSuiteQL", "description": oracle_desc, "input_schema": {"type": "object", "properties": {}}}]
    )
    tools = build_external_tool_definitions([conn])
    assert len(tools) == 1

    out_desc = tools[0]["description"]
    # Full description must pass through — any truncation is a handicap
    # versus the Claude-direct + MCP baseline.
    prefix = "[netsuite] "
    assert out_desc == prefix + oracle_desc, (
        f"Description was altered: expected {len(prefix) + len(oracle_desc)} chars, got {len(out_desc)}"
    )
    # Key dialect rules must appear in the output
    assert "|| operator" in out_desc
    assert "TO_DATE" in out_desc
    assert "ANSI JOIN" in out_desc


def test_no_local_cap_on_descriptions():
    """Regression guard: we must NEVER impose a local cap on MCP tool
    descriptions. Any cap is a direct restriction on agent capability
    relative to the Claude-direct + MCP baseline. Only Anthropic's API
    should bound the size.
    """
    huge_desc = "X" * 20_000
    conn = _FakeConnector(
        [{"name": "huge_tool", "description": huge_desc, "input_schema": {"type": "object", "properties": {}}}]
    )
    tools = build_external_tool_definitions([conn])
    # The description should pass through in full (plus the [provider] prefix).
    expected = "[netsuite] " + huge_desc
    assert tools[0]["description"] == expected, (
        f"Description was truncated from {len(expected)} to {len(tools[0]['description'])} chars — "
        f"we must not impose any local cap on MCP descriptions."
    )


def test_tool_name_format_unchanged():
    conn = _FakeConnector(
        [{"name": "ns_runCustomSuiteQL", "description": "x", "input_schema": {"type": "object", "properties": {}}}]
    )
    tools = build_external_tool_definitions([conn])
    # ext__{32-char hex}__{raw_name}
    assert tools[0]["name"].startswith("ext__")
    assert tools[0]["name"].endswith("__ns_runCustomSuiteQL")
    assert len(tools[0]["name"].split("__")[1]) == 32  # hex of UUID
