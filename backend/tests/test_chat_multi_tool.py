"""Tests for multi-tool execution via the new tools.py module.

Replaces the old tool_caller_node tests with tests for the new
execute_tool_call dispatcher and tool definition builders.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.chat.tools import (
    _make_ext_tool_name,
    execute_tool_call,
    parse_external_tool_name,
)


class TestExecuteToolCallRouting:
    """Test that execute_tool_call routes correctly to local vs external."""

    @pytest.mark.asyncio
    async def test_local_allowed_tool(self, db):
        """Allowed local tool executes via mcp_server."""
        mock_result = {"data": [{"id": "1"}]}
        with patch("app.services.chat.tools.mcp_server") as mock_mcp:
            mock_mcp.call_tool = AsyncMock(return_value=mock_result)
            result = await execute_tool_call(
                tool_name="data_sample_table_read",
                tool_input={"table_name": "orders"},
                tenant_id=uuid.uuid4(),
                actor_id=uuid.uuid4(),
                correlation_id=str(uuid.uuid4()),
                db=db,
            )
        parsed = json.loads(result)
        assert "data" in parsed

    @pytest.mark.asyncio
    async def test_disallowed_tool_returns_error(self, db):
        """Non-allowed tool name returns error JSON."""
        result = await execute_tool_call(
            tool_name="admin_drop_all",
            tool_input={},
            tenant_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            correlation_id=str(uuid.uuid4()),
            db=db,
        )
        parsed = json.loads(result)
        assert "error" in parsed
        assert "not allowed" in parsed["error"]

    @pytest.mark.asyncio
    async def test_external_tool_nonexistent_connector(self, db):
        """External tool with non-existent connector returns error."""
        connector_id = uuid.uuid4()
        tool_name = _make_ext_tool_name(connector_id, "some_tool")
        result = await execute_tool_call(
            tool_name=tool_name,
            tool_input={},
            tenant_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            correlation_id=str(uuid.uuid4()),
            db=db,
        )
        parsed = json.loads(result)
        assert "error" in parsed
        assert "not found or disabled" in parsed["error"]


class TestToolNameRoundTrip:
    """Test external tool name creation and parsing."""

    def test_round_trip(self):
        cid = uuid.uuid4()
        name = _make_ext_tool_name(cid, "ns_runSuiteQL")
        parsed = parse_external_tool_name(name)
        assert parsed is not None
        assert parsed[0] == cid
        assert parsed[1] == "ns_runSuiteQL"

    def test_non_external_returns_none(self):
        assert parse_external_tool_name("data_sample_table_read") is None
