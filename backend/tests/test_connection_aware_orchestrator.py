"""Tests for connection-aware orchestrator (Fix 1 — 10x Agent Quality).

The orchestrator should check connection health before the agentic loop and:
1. Strip tools for dead connections (REST or MCP)
2. Inject a warning into the system prompt
3. Skip checks during onboarding
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.orchestrator import (
    _check_connection_health,
)


TENANT_ID = uuid.uuid4()


# ── Helper to build fake DB rows ──

def _make_conn_row(label: str, status: str, error_reason: str | None = None):
    """Simulate a Connection row (label, status, error_reason)."""
    row = MagicMock()
    row.label = label
    row.status = status
    row.error_reason = error_reason
    return row


def _make_mcp_row(label: str, status: str):
    """Simulate an McpConnector row (label, status)."""
    row = MagicMock()
    row.label = label
    row.status = status
    return row


class TestCheckConnectionHealth:
    """Unit tests for _check_connection_health()."""

    @pytest.mark.asyncio
    async def test_all_healthy_returns_empty(self):
        """When all connections are active, no warnings are returned."""
        db = AsyncMock()
        # REST: active
        rest_result = MagicMock()
        rest_result.all.return_value = [_make_conn_row("NetSuite REST", "active")]
        # MCP: active
        mcp_result = MagicMock()
        mcp_result.all.return_value = [_make_mcp_row("NetSuite MCP", "active")]

        db.execute = AsyncMock(side_effect=[rest_result, mcp_result])

        warnings = await _check_connection_health(db, TENANT_ID)
        assert warnings == []

    @pytest.mark.asyncio
    async def test_rest_needs_reauth(self):
        """REST connection needing reauth should produce a warning."""
        db = AsyncMock()
        rest_result = MagicMock()
        rest_result.all.return_value = [_make_conn_row("NetSuite REST", "needs_reauth")]
        mcp_result = MagicMock()
        mcp_result.all.return_value = []

        db.execute = AsyncMock(side_effect=[rest_result, mcp_result])

        warnings = await _check_connection_health(db, TENANT_ID)
        assert len(warnings) == 1
        assert "REST API" in warnings[0]
        assert "needs_reauth" in warnings[0]

    @pytest.mark.asyncio
    async def test_mcp_error(self):
        """MCP connection in error state should produce a warning."""
        db = AsyncMock()
        rest_result = MagicMock()
        rest_result.all.return_value = []
        mcp_result = MagicMock()
        mcp_result.all.return_value = [_make_mcp_row("NetSuite MCP", "error")]

        db.execute = AsyncMock(side_effect=[rest_result, mcp_result])

        warnings = await _check_connection_health(db, TENANT_ID)
        assert len(warnings) == 1
        assert "MCP" in warnings[0]
        assert "error" in warnings[0]

    @pytest.mark.asyncio
    async def test_both_broken(self):
        """Both REST and MCP broken should produce two warnings."""
        db = AsyncMock()
        rest_result = MagicMock()
        rest_result.all.return_value = [_make_conn_row("NetSuite REST", "expired")]
        mcp_result = MagicMock()
        mcp_result.all.return_value = [_make_mcp_row("NetSuite MCP", "needs_reauth")]

        db.execute = AsyncMock(side_effect=[rest_result, mcp_result])

        warnings = await _check_connection_health(db, TENANT_ID)
        assert len(warnings) == 2

    @pytest.mark.asyncio
    async def test_no_connections(self):
        """Tenant with no connections should return no warnings."""
        db = AsyncMock()
        rest_result = MagicMock()
        rest_result.all.return_value = []
        mcp_result = MagicMock()
        mcp_result.all.return_value = []

        db.execute = AsyncMock(side_effect=[rest_result, mcp_result])

        warnings = await _check_connection_health(db, TENANT_ID)
        assert warnings == []

    @pytest.mark.asyncio
    async def test_db_error_returns_empty(self):
        """If DB query fails, return empty (fail-open — don't block chat)."""
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=Exception("DB connection lost"))

        warnings = await _check_connection_health(db, TENANT_ID)
        assert warnings == []


class TestFilterToolsForDeadConnections:
    """Tests for tool stripping logic."""

    def _make_tool(self, name: str) -> dict:
        return {"name": name, "description": f"Tool {name}"}

    def test_rest_dead_strips_local_tools(self):
        """When REST is dead, local SuiteQL and financial report tools are removed."""
        from app.services.chat.orchestrator import _filter_tools_for_dead_connections

        tools = [
            self._make_tool("netsuite_suiteql"),
            self._make_tool("netsuite_financial_report"),
            self._make_tool("rag_search"),
            self._make_tool("ext__ns_runCustomSuiteQL"),
        ]
        warnings = ["REST API (NetSuite REST): needs_reauth"]

        filtered = _filter_tools_for_dead_connections(tools, warnings)
        names = [t["name"] for t in filtered]

        assert "netsuite_suiteql" not in names
        assert "netsuite_financial_report" not in names
        assert "rag_search" in names
        assert "ext__ns_runCustomSuiteQL" in names

    def test_mcp_dead_strips_ext_tools(self):
        """When MCP is dead, all ext__ prefixed tools are removed."""
        from app.services.chat.orchestrator import _filter_tools_for_dead_connections

        tools = [
            self._make_tool("netsuite_suiteql"),
            self._make_tool("ext__ns_runCustomSuiteQL"),
            self._make_tool("ext__ns_runReport"),
            self._make_tool("rag_search"),
        ]
        warnings = ["MCP (NetSuite MCP): error"]

        filtered = _filter_tools_for_dead_connections(tools, warnings)
        names = [t["name"] for t in filtered]

        assert "ext__ns_runCustomSuiteQL" not in names
        assert "ext__ns_runReport" not in names
        assert "netsuite_suiteql" in names
        assert "rag_search" in names

    def test_both_dead_strips_all_netsuite(self):
        """When both REST and MCP are dead, all NetSuite tools are stripped."""
        from app.services.chat.orchestrator import _filter_tools_for_dead_connections

        tools = [
            self._make_tool("netsuite_suiteql"),
            self._make_tool("netsuite_financial_report"),
            self._make_tool("ext__ns_runCustomSuiteQL"),
            self._make_tool("ext__ns_runReport"),
            self._make_tool("rag_search"),
            self._make_tool("web_search"),
        ]
        warnings = [
            "REST API (NetSuite REST): expired",
            "MCP (NetSuite MCP): needs_reauth",
        ]

        filtered = _filter_tools_for_dead_connections(tools, warnings)
        names = [t["name"] for t in filtered]

        assert "netsuite_suiteql" not in names
        assert "netsuite_financial_report" not in names
        assert "ext__ns_runCustomSuiteQL" not in names
        assert "ext__ns_runReport" not in names
        assert "rag_search" in names
        assert "web_search" in names

    def test_no_warnings_returns_all_tools(self):
        """When no warnings, all tools pass through unchanged."""
        from app.services.chat.orchestrator import _filter_tools_for_dead_connections

        tools = [
            self._make_tool("netsuite_suiteql"),
            self._make_tool("ext__ns_runCustomSuiteQL"),
            self._make_tool("rag_search"),
        ]

        filtered = _filter_tools_for_dead_connections(tools, [])
        assert filtered == tools


class TestBuildConnectionWarningBlock:
    """Tests for the warning text injected into system prompt."""

    def test_warning_block_mentions_settings(self):
        """Warning should direct user to Settings > Connections."""
        from app.services.chat.orchestrator import _build_connection_warning_block

        block = _build_connection_warning_block(
            ["REST API (NetSuite REST): needs_reauth"]
        )
        assert "CONNECTION STATUS" in block
        assert "Settings" in block
        assert "needs_reauth" in block

    def test_warning_block_lists_all_broken(self):
        """All broken connections should appear in the warning."""
        from app.services.chat.orchestrator import _build_connection_warning_block

        block = _build_connection_warning_block([
            "REST API (NetSuite REST): expired",
            "MCP (NetSuite MCP): needs_reauth",
        ])
        assert "REST API" in block
        assert "MCP" in block

    def test_empty_warnings_returns_empty(self):
        """No warnings should produce empty string."""
        from app.services.chat.orchestrator import _build_connection_warning_block

        block = _build_connection_warning_block([])
        assert block == ""
