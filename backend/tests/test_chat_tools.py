"""Tests for chat tool definition builders and execution dispatcher."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.tools import (
    _LOCAL_NAME_MAP,
    _make_ext_tool_name,
    build_external_tool_definitions,
    build_local_tool_definitions,
    execute_tool_call,
    parse_external_tool_name,
)

# ---------------------------------------------------------------------------
# build_local_tool_definitions
# ---------------------------------------------------------------------------


class TestBuildLocalToolDefinitions:
    def test_returns_only_allowed_tools(self):
        """Only ALLOWED_CHAT_TOOLS should appear in definitions."""
        defs = build_local_tool_definitions()
        names = {d["name"] for d in defs}
        # All names should be sanitized (dots -> underscores)
        for name in names:
            assert "." not in name, f"Tool name '{name}' still contains dots"
        # Should include allowed tools
        assert "netsuite_suiteql" in names
        assert "data_sample_table_read" in names
        assert "report_export" in names
        assert "netsuite_connectivity" in names
        # Should NOT include disallowed tools
        assert "schedule_create" not in names
        assert "recon_run" not in names
        assert "health" not in names

    def test_anthropic_format(self):
        """Each definition should have name, description, input_schema."""
        defs = build_local_tool_definitions()
        for d in defs:
            assert "name" in d
            assert "description" in d
            assert "input_schema" in d
            schema = d["input_schema"]
            assert schema["type"] == "object"
            assert "properties" in schema
            assert "required" in schema

    def test_required_params_correct(self):
        """Required parameters should be marked correctly."""
        defs = build_local_tool_definitions()
        suiteql = next(d for d in defs if d["name"] == "netsuite_suiteql")
        assert "query" in suiteql["input_schema"]["required"]


# ---------------------------------------------------------------------------
# build_external_tool_definitions
# ---------------------------------------------------------------------------


class TestBuildExternalToolDefinitions:
    def test_namespaces_correctly(self):
        """External tools should be namespaced with connector ID."""
        connector = MagicMock()
        connector.id = uuid.uuid4()
        connector.provider = "netsuite_mcp"
        connector.discovered_tools = [
            {
                "name": "ns_runSuiteQL",
                "description": "Run a SuiteQL query",
                "input_schema": {
                    "type": "object",
                    "properties": {"sqlQuery": {"type": "string"}},
                    "required": ["sqlQuery"],
                },
            }
        ]

        defs = build_external_tool_definitions([connector])
        assert len(defs) == 1
        assert defs[0]["name"].startswith("ext__")
        assert connector.id.hex in defs[0]["name"]
        assert "ns_runSuiteQL" in defs[0]["name"]

    def test_includes_input_schema(self):
        """External tool definitions should preserve input_schema."""
        connector = MagicMock()
        connector.id = uuid.uuid4()
        connector.provider = "netsuite_mcp"
        connector.discovered_tools = [
            {
                "name": "tool1",
                "description": "Test tool",
                "input_schema": {
                    "type": "object",
                    "properties": {"param1": {"type": "string"}},
                },
            }
        ]

        defs = build_external_tool_definitions([connector])
        assert defs[0]["input_schema"]["properties"]["param1"]["type"] == "string"

    def test_empty_connectors(self):
        """No connectors should return empty list."""
        assert build_external_tool_definitions([]) == []

    def test_connector_without_tools(self):
        """Connector with no discovered_tools should be skipped."""
        connector = MagicMock()
        connector.discovered_tools = None
        assert build_external_tool_definitions([connector]) == []

    def test_description_includes_provider(self):
        """Description should include the provider name."""
        connector = MagicMock()
        connector.id = uuid.uuid4()
        connector.provider = "netsuite_mcp"
        connector.discovered_tools = [
            {"name": "tool1", "description": "Does stuff"},
        ]

        defs = build_external_tool_definitions([connector])
        assert "[netsuite_mcp]" in defs[0]["description"]


# ---------------------------------------------------------------------------
# parse_external_tool_name
# ---------------------------------------------------------------------------


class TestParseExternalToolName:
    def test_round_trip(self):
        """Creating and parsing should round-trip the connector ID and name."""
        cid = uuid.uuid4()
        name = _make_ext_tool_name(cid, "ns_runCustomSuiteQL")
        parsed = parse_external_tool_name(name)
        assert parsed is not None
        assert parsed[0] == cid
        assert parsed[1] == "ns_runCustomSuiteQL"

    def test_non_external_returns_none(self):
        assert parse_external_tool_name("netsuite_suiteql") is None

    def test_invalid_hex_returns_none(self):
        assert parse_external_tool_name("ext__not_a_hex_string_at_all____tool") is None

    def test_truncates_long_names(self):
        """Long tool names should be truncated to fit within 64 chars."""
        cid = uuid.uuid4()
        long_name = "a" * 100
        ext_name = _make_ext_tool_name(cid, long_name)
        assert len(ext_name) <= 64


# ---------------------------------------------------------------------------
# execute_tool_call
# ---------------------------------------------------------------------------


class TestExecuteToolCall:
    @pytest.mark.asyncio
    async def test_local_tool_execution(self, db):
        """Local allowed tool should execute via mcp_server."""
        mock_result = {"rows": [{"id": 1}]}
        with patch("app.services.chat.tools.mcp_server") as mock_mcp:
            mock_mcp.call_tool = AsyncMock(return_value=mock_result)
            result = await execute_tool_call(
                tool_name="netsuite_suiteql",
                tool_input={"query": "SELECT 1"},
                tenant_id=uuid.uuid4(),
                actor_id=uuid.uuid4(),
                correlation_id="test-corr",
                db=db,
            )

        parsed = json.loads(result)
        assert parsed["rows"] == [{"id": 1}]

    @pytest.mark.asyncio
    async def test_disallowed_tool(self, db):
        """Disallowed tool name should return error without executing."""
        result = await execute_tool_call(
            tool_name="schedule_create",
            tool_input={},
            tenant_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            correlation_id="test-corr",
            db=db,
        )
        parsed = json.loads(result)
        assert "error" in parsed
        assert "not allowed" in parsed["error"]

    @pytest.mark.asyncio
    async def test_local_tool_failure_returns_error(self, db):
        """Local tool that throws should return error JSON, not raise."""
        with patch("app.services.chat.tools.mcp_server") as mock_mcp:
            mock_mcp.call_tool = AsyncMock(side_effect=Exception("MCP down"))
            result = await execute_tool_call(
                tool_name="data_sample_table_read",
                tool_input={"table_name": "orders"},
                tenant_id=uuid.uuid4(),
                actor_id=uuid.uuid4(),
                correlation_id="test-corr",
                db=db,
            )

        parsed = json.loads(result)
        assert "error" in parsed
        assert "failed" in parsed["error"]

    @pytest.mark.asyncio
    async def test_external_tool_routes_correctly(self, db):
        """External tool name should be dispatched to _execute_external_tool."""
        connector_id = uuid.uuid4()
        tool_name = _make_ext_tool_name(connector_id, "test_tool")

        with patch("app.services.chat.tools._execute_external_tool", new_callable=AsyncMock) as mock_ext:
            mock_ext.return_value = {"data": "ok"}
            result = await execute_tool_call(
                tool_name=tool_name,
                tool_input={"param": "value"},
                tenant_id=uuid.uuid4(),
                actor_id=uuid.uuid4(),
                correlation_id="test-corr",
                db=db,
            )

        parsed = json.loads(result)
        assert parsed == {"data": "ok"}
        mock_ext.assert_called_once()


# ---------------------------------------------------------------------------
# _LOCAL_NAME_MAP
# ---------------------------------------------------------------------------


class TestLocalNameMap:
    def test_maps_sanitized_to_original(self):
        """Map should convert underscore names back to dotted MCP names."""
        assert _LOCAL_NAME_MAP["netsuite_suiteql"] == "netsuite.suiteql"
        assert _LOCAL_NAME_MAP["data_sample_table_read"] == "data.sample_table_read"
        assert _LOCAL_NAME_MAP["report_export"] == "report.export"

    def test_no_disallowed_tools(self):
        """Map should not contain disallowed tools."""
        assert "schedule_create" not in _LOCAL_NAME_MAP
        assert "recon_run" not in _LOCAL_NAME_MAP
