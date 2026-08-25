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
        assert "report_compose" in names
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
        assert _LOCAL_NAME_MAP["report_compose"] == "report.compose"

    def test_no_disallowed_tools(self):
        """Map should not contain disallowed tools."""
        assert "schedule_create" not in _LOCAL_NAME_MAP
        assert "recon_run" not in _LOCAL_NAME_MAP


# ---------------------------------------------------------------------------
# Category stamping integration
# ---------------------------------------------------------------------------


class TestCategoryStamping:
    def test_every_local_tool_is_categorizable(self):
        """Every tool name from build_local_tool_definitions must resolve to a valid category."""
        from typing import get_args

        from app.services.chat.tool_categories import Category, categorize

        tools = build_local_tool_definitions()
        assert tools, "build_local_tool_definitions returned no tools"
        # Derive the valid set from the closed Category union so this stays in
        # sync as new categories (e.g. "report") are added.
        valid = set(get_args(Category))
        for t in tools:
            name = t.get("name", "")
            category = categorize(name)
            assert category in valid, f"{name} resolved to unexpected category {category!r}"


# ---------------------------------------------------------------------------
# Celigo read-only enforcement (two layers)
# ---------------------------------------------------------------------------


class TestCeligoReadOnlyEnforcement:
    """Celigo writes must be unreachable at BOTH layers.

    Layer 1 keeps them out of the model's inventory. Layer 2 is the dispatcher —
    agent-graph.md #3 records that execute_tool_call has 7 callers and only one
    of them consults classify_mutation, so definition-time filtering alone is a
    hole. Both are tested because either one alone is insufficient.
    """

    class _FakeConnector:
        def __init__(self, provider, tools, is_enabled=True):
            import uuid as _uuid

            self.id = _uuid.UUID("11111111-1111-1111-1111-111111111111")
            self.provider = provider
            self.discovered_tools = tools
            self.is_enabled = is_enabled

    def test_layer1_write_tools_absent_from_definitions(self):
        from app.services.chat.tools import build_external_tool_definitions

        connector = self._FakeConnector(
            "celigo_mcp",
            [
                {"name": "list_flows", "description": "List flows"},
                {"name": "upsert_flow", "description": "Create or update a flow"},
                {"name": "delete_resource", "description": "Delete a resource"},
                {"name": "run_flow", "description": "Run a flow now"},
            ],
        )
        names = [t["name"] for t in build_external_tool_definitions([connector])]

        assert any(n.endswith("__list_flows") for n in names)
        assert not any(n.endswith("__upsert_flow") for n in names)
        assert not any(n.endswith("__delete_resource") for n in names)
        assert not any(n.endswith("__run_flow") for n in names)

    def test_layer1_netsuite_connector_unfiltered(self):
        """The filter must apply ONLY to Celigo providers."""
        from app.services.chat.tools import build_external_tool_definitions

        connector = self._FakeConnector(
            "netsuite_mcp",
            [
                {"name": "ns_createRecord", "description": "Create"},
                {"name": "ns_getRecord", "description": "Read"},
            ],
        )
        names = [t["name"] for t in build_external_tool_definitions([connector])]

        assert any(n.endswith("__ns_createRecord") for n in names)
        assert any(n.endswith("__ns_getRecord") for n in names)

    @pytest.mark.asyncio
    async def test_layer2_dispatcher_denies_write(self, monkeypatch):
        """Even called directly — bypassing definitions entirely — a write is refused."""
        import uuid as _uuid

        from app.services.chat import tools as tools_mod

        connector = self._FakeConnector("celigo_mcp", [])

        async def _fake_get(db, connector_id, tenant_id):
            return connector

        called = {"n": 0}

        async def _fake_call(*args, **kwargs):
            called["n"] += 1
            return {"ok": True}

        monkeypatch.setattr("app.services.mcp_connector_service.get_mcp_connector", _fake_get, raising=False)
        monkeypatch.setattr("app.services.mcp_client_service.call_external_mcp_tool", _fake_call, raising=False)

        result = await tools_mod._execute_external_tool(
            connector_id=connector.id,
            raw_tool_name="delete_resource",
            tool_input={"_id": "abc"},
            tenant_id=_uuid.uuid4(),
            db=None,
        )

        assert "error" in result
        assert "read-only" in result["error"].lower()
        assert called["n"] == 0, "the write reached the Celigo MCP server"

    def _stub_flag(self, monkeypatch, enabled: bool):
        """The dispatcher also enforces the `celigo` kill switch (round-4 fix),
        so a unit test about the READ-ONLY policy has to say which side of the
        flag it is on. Stubbed rather than seeded: these tests run without a DB.
        """

        async def _flag(db, tenant_id, flag_key):
            return enabled

        monkeypatch.setattr("app.services.feature_flag_service.is_enabled", _flag, raising=False)

    @pytest.mark.asyncio
    async def test_layer2_dispatcher_allows_read(self, monkeypatch):
        import uuid as _uuid

        from app.services.chat import tools as tools_mod

        connector = self._FakeConnector("celigo_mcp", [])

        async def _fake_get(db, connector_id, tenant_id):
            return connector

        async def _fake_call(*args, **kwargs):
            return {"items": []}

        monkeypatch.setattr("app.services.mcp_connector_service.get_mcp_connector", _fake_get, raising=False)
        monkeypatch.setattr("app.services.mcp_client_service.call_external_mcp_tool", _fake_call, raising=False)
        self._stub_flag(monkeypatch, True)

        result = await tools_mod._execute_external_tool(
            connector_id=connector.id,
            raw_tool_name="list_flows",
            tool_input={},
            tenant_id=_uuid.uuid4(),
            db=None,
        )

        assert result == {"items": []}

    @pytest.mark.asyncio
    async def test_layer2_dispatcher_denies_read_when_the_flag_is_off(self, monkeypatch):
        """The kill switch is enforced at this layer too, for the same reason
        the read-only policy is: the dispatcher has several callers and a
        tool_use block can outlive the inventory it was built from."""
        import uuid as _uuid

        from app.services.chat import tools as tools_mod

        connector = self._FakeConnector("celigo_mcp", [])

        async def _fake_get(db, connector_id, tenant_id):
            return connector

        called = {"n": 0}

        async def _fake_call(*args, **kwargs):
            called["n"] += 1
            return {"items": []}

        monkeypatch.setattr("app.services.mcp_connector_service.get_mcp_connector", _fake_get, raising=False)
        monkeypatch.setattr("app.services.mcp_client_service.call_external_mcp_tool", _fake_call, raising=False)
        self._stub_flag(monkeypatch, False)

        result = await tools_mod._execute_external_tool(
            connector_id=connector.id,
            raw_tool_name="list_flows",
            tool_input={},
            tenant_id=_uuid.uuid4(),
            db=None,
        )

        assert "error" in result
        assert called["n"] == 0, "the read reached the Celigo MCP server after the kill switch"
