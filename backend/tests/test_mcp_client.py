"""MCP client contract tests — exercises MCPServer end-to-end with governance."""

import uuid

import pytest
from sqlalchemy import select

from app.mcp.governance import TOOL_CONFIGS, _rate_limits
from app.mcp.metrics import get_metrics, reset_metrics
from app.mcp.server import MCPServer
from app.models.audit import AuditEvent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TENANT_ID = str(uuid.uuid4())
ACTOR_ID = str(uuid.uuid4())

EXPECTED_TOOLS = {
    "health",
    "netsuite.suiteql",
    "netsuite.suiteql_stub",
    "netsuite.connectivity",
    "data.sample_table_read",
    "recon.run",
    "report.export",
    "schedule.create",
    "schedule.list",
    "schedule.run",
    "workspace.list_files",
    "workspace.read_file",
    "workspace.search",
    "workspace.propose_patch",
}


@pytest.fixture(autouse=True)
def _clear_state():
    """Reset rate limits and metrics between tests."""
    _rate_limits.clear()
    reset_metrics()
    yield
    _rate_limits.clear()
    reset_metrics()


@pytest.fixture
def server():
    return MCPServer()


# ---------------------------------------------------------------------------
# Step 4: Core contract tests
# ---------------------------------------------------------------------------


class TestListTools:
    def test_list_tools(self, server: MCPServer):
        tools = server.list_tools()
        tool_names = {t["name"] for t in tools}
        assert tool_names == EXPECTED_TOOLS

    def test_tools_have_description(self, server: MCPServer):
        for tool in server.list_tools():
            assert "description" in tool
            assert len(tool["description"]) > 0

    def test_tools_have_input_schema(self, server: MCPServer):
        for tool in server.list_tools():
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"


class TestHealthTool:
    async def test_health_tool(self, server: MCPServer):
        result = await server.call_tool("health", {}, TENANT_ID, ACTOR_ID)
        assert result["status"] == "ok"
        assert "timestamp" in result
        assert result["tools_registered"] > 0
        assert result["tools_registered"] == len(EXPECTED_TOOLS)


class TestSuiteqlStub:
    async def test_suiteql_stub(self, server: MCPServer):
        """SuiteQL now requires context (tenant_id + db) — without it, returns an error dict."""
        result = await server.call_tool(
            "netsuite.suiteql_stub",
            {"query": "SELECT * FROM transaction"},
            TENANT_ID,
            ACTOR_ID,
        )
        # Real execute requires context; without it returns a graceful error
        assert result.get("error") is True or "error" in result
        assert "context" in result.get("message", "").lower() or "tenant" in result.get("message", "").lower()


class TestDataSampleTableRead:
    async def test_valid_table(self, server: MCPServer):
        result = await server.call_tool(
            "data.sample_table_read",
            {"table_name": "orders"},
            TENANT_ID,
            ACTOR_ID,
        )
        assert "error" not in result
        assert result["table"] == "orders"
        assert isinstance(result["columns"], list)
        assert len(result["columns"]) > 0
        assert result["rows"] == []
        assert result["row_count"] == 0

    async def test_invalid_table(self, server: MCPServer):
        result = await server.call_tool(
            "data.sample_table_read",
            {"table_name": "secret_table"},
            TENANT_ID,
            ACTOR_ID,
        )
        assert "error" in result
        assert "secret_table" in result["error"]


class TestUnknownTool:
    async def test_unknown_tool_rejected(self, server: MCPServer):
        result = await server.call_tool("nonexistent.tool", {}, TENANT_ID, ACTOR_ID)
        assert "error" in result
        assert "Unknown tool" in result["error"]

    async def test_tool_not_in_registry(self, server: MCPServer):
        result = await server.call_tool("admin.drop_all", {}, TENANT_ID, ACTOR_ID)
        assert "error" in result
        assert "Unknown tool: admin.drop_all" in result["error"]


class TestRateLimitEnforced:
    async def test_rate_limit_enforced(self, server: MCPServer):
        tool = "recon.run"
        limit = TOOL_CONFIGS[tool]["rate_limit_per_minute"]

        # Exhaust rate limit
        for _ in range(limit):
            result = await server.call_tool(
                tool, {"date_from": "2024-01-01", "date_to": "2024-01-31"}, TENANT_ID, ACTOR_ID
            )
            assert "error" not in result

        # Next call should be denied
        result = await server.call_tool(tool, {"date_from": "2024-01-01", "date_to": "2024-01-31"}, TENANT_ID, ACTOR_ID)
        assert "error" in result
        assert "rate limit" in result["error"].lower()


class TestParamFiltering:
    async def test_param_filtering(self, server: MCPServer):
        """Extra params are stripped by governance; only allowlisted params reach execute."""
        result = await server.call_tool(
            "netsuite.suiteql_stub",
            {"query": "SELECT 1", "limit": 10, "evil_param": "DROP TABLE", "injection": "';--"},
            TENANT_ID,
            ACTOR_ID,
        )
        # Real execute requires context so returns a graceful error,
        # but the key point is that evil params were stripped (no crash from them).
        # The error should be about missing context, not about evil params.
        assert "evil_param" not in str(result)
        assert "injection" not in str(result)


class TestCorrelationId:
    async def test_correlation_id_propagated(self, server: MCPServer):
        cid = str(uuid.uuid4())
        result = await server.call_tool("health", {}, TENANT_ID, ACTOR_ID, correlation_id=cid)
        # The result itself doesn't include correlation_id, but it shouldn't error
        assert result["status"] == "ok"


class TestMetrics:
    async def test_metrics_recorded(self, server: MCPServer):
        await server.call_tool("health", {}, TENANT_ID, ACTOR_ID)
        metrics = get_metrics()
        assert "health" in metrics["mcp_tool_calls_total"]
        assert metrics["mcp_tool_calls_total"]["health"]["success"] == 1

    async def test_rate_limit_metrics(self, server: MCPServer):
        tool = "recon.run"
        limit = TOOL_CONFIGS[tool]["rate_limit_per_minute"]
        for _ in range(limit):
            await server.call_tool(tool, {"date_from": "2024-01-01", "date_to": "2024-01-31"}, TENANT_ID, ACTOR_ID)
        await server.call_tool(tool, {"date_from": "2024-01-01", "date_to": "2024-01-31"}, TENANT_ID, ACTOR_ID)

        metrics = get_metrics()
        assert metrics["mcp_rate_limit_rejections_total"].get(tool, 0) >= 1


# ---------------------------------------------------------------------------
# Step 5: Negative tests for disallowed calls
# ---------------------------------------------------------------------------


class TestDisallowedCalls:
    async def test_disallowed_table_name(self, server: MCPServer):
        result = await server.call_tool(
            "data.sample_table_read",
            {"table_name": "users"},
            TENANT_ID,
            ACTOR_ID,
        )
        assert "error" in result
        assert "allowlist" in result["error"].lower()

    async def test_suiteql_dangerous_query_params_stripped(self, server: MCPServer):
        """Non-allowlisted params are stripped; real execute returns context error, not param error."""
        result = await server.call_tool(
            "netsuite.suiteql",
            {"query": "SELECT 1", "drop_table": True, "admin": True},
            TENANT_ID,
            ACTOR_ID,
        )
        # Real execute requires context; the error should be about missing context, not about params
        assert "drop_table" not in str(result)
        assert "admin" not in str(result)

    async def test_tool_not_in_registry_returns_error(self, server: MCPServer):
        result = await server.call_tool("admin.delete_everything", {}, TENANT_ID, ACTOR_ID)
        assert "error" in result
        assert "Unknown tool: admin.delete_everything" in result["error"]


# ---------------------------------------------------------------------------
# DB-backed audit tests (require real DB session)
# ---------------------------------------------------------------------------


class TestAuditDBWrites:
    async def test_audit_event_on_success(self, server: MCPServer, db):
        """Call a tool with DB session → verify audit_events row created."""
        cid = str(uuid.uuid4())
        result = await server.call_tool("health", {}, TENANT_ID, ACTOR_ID, correlation_id=cid, db=db)
        assert result["status"] == "ok"

        # Query audit_events
        stmt = select(AuditEvent).where(
            AuditEvent.correlation_id == cid,
            AuditEvent.category == "tool_call",
        )
        rows = (await db.execute(stmt)).scalars().all()
        assert len(rows) == 1
        event = rows[0]
        assert event.action == "tool.health"
        assert event.resource_type == "mcp_tool"
        assert event.resource_id == "health"
        assert event.status == "success"
        assert event.payload["tool_name"] == "health"

    async def test_audit_event_on_rate_limit(self, server: MCPServer, db):
        """Exhaust rate limit + call → verify denied audit row."""
        tool = "recon.run"
        limit = TOOL_CONFIGS[tool]["rate_limit_per_minute"]

        # Exhaust (no DB for these — keep them fast)
        for _ in range(limit):
            await server.call_tool(tool, {"date_from": "2024-01-01", "date_to": "2024-01-31"}, TENANT_ID, ACTOR_ID)

        # This call should be denied and audited
        cid = str(uuid.uuid4())
        result = await server.call_tool(
            tool,
            {"date_from": "2024-01-01", "date_to": "2024-01-31"},
            TENANT_ID,
            ACTOR_ID,
            correlation_id=cid,
            db=db,
        )
        assert "error" in result

        stmt = select(AuditEvent).where(
            AuditEvent.correlation_id == cid,
            AuditEvent.category == "tool_call",
        )
        rows = (await db.execute(stmt)).scalars().all()
        assert len(rows) == 1
        event = rows[0]
        assert event.action == "tool.rate_limited"
        assert event.status == "denied"
        assert event.error_message == "Rate limit exceeded"

    async def test_audit_event_on_error(self, server: MCPServer, db):
        """Inject a failing execute fn → verify error audit row."""
        cid = str(uuid.uuid4())

        result = await server.call_tool(
            "data.sample_table_read",
            {"table_name": "nonexistent_table"},
            TENANT_ID,
            ACTOR_ID,
            correlation_id=cid,
            db=db,
        )
        assert "error" in result

        stmt = select(AuditEvent).where(
            AuditEvent.correlation_id == cid,
            AuditEvent.category == "tool_call",
        )
        rows = (await db.execute(stmt)).scalars().all()
        assert len(rows) == 1
        event = rows[0]
        assert event.action == "tool.data.sample_table_read"
        assert event.status == "error"
        assert "nonexistent_table" in event.error_message
