"""Tests for chat orchestrator nodes with mocked LLM."""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.nodes import (
    ALLOWED_CHAT_TOOLS,
    OrchestratorState,
    db_reader_node,
    router_node,
    synthesizer_node,
    tool_caller_node,
)


def _make_state(**overrides) -> OrchestratorState:
    """Create a minimal OrchestratorState for testing."""
    defaults = {
        "user_message": "What are my recent orders?",
        "tenant_id": uuid.uuid4(),
        "actor_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
    }
    defaults.update(overrides)
    return OrchestratorState(**defaults)


def _mock_anthropic_response(text: str):
    """Create a mock Anthropic API response."""
    mock_response = MagicMock()
    mock_content = MagicMock()
    mock_content.text = text
    mock_response.content = [mock_content]
    return mock_response


class TestRouterNode:
    """Test the router_node."""

    @pytest.mark.asyncio
    async def test_valid_json_route(self):
        """Router should parse valid JSON route from LLM."""
        route = {
            "needs_docs": False,
            "needs_db": True,
            "db_tables": ["orders"],
            "needs_tool": False,
            "tool_name": None,
            "tool_params": None,
            "direct_answer": False,
        }
        mock_response = _mock_anthropic_response(json.dumps(route))

        state = _make_state()
        with patch("app.services.chat.nodes._get_anthropic_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(return_value=mock_response)
            await router_node(state)

        assert state.route is not None
        assert state.route["needs_db"] is True
        assert state.route["db_tables"] == ["orders"]

    @pytest.mark.asyncio
    async def test_json_in_code_block(self):
        """Router should extract JSON from markdown code blocks."""
        route = {"needs_docs": True, "needs_db": False, "db_tables": [], "direct_answer": True}
        text = f"```json\n{json.dumps(route)}\n```"
        mock_response = _mock_anthropic_response(text)

        state = _make_state()
        with patch("app.services.chat.nodes._get_anthropic_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(return_value=mock_response)
            await router_node(state)

        assert state.route is not None
        assert state.route["needs_docs"] is True

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_json(self):
        """Router should use fallback route on invalid JSON."""
        mock_response = _mock_anthropic_response("This is not JSON at all")

        state = _make_state()
        with patch("app.services.chat.nodes._get_anthropic_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(return_value=mock_response)
            await router_node(state)

        assert state.route == {"needs_docs": True, "direct_answer": True}


class TestDbReaderNode:
    """Test the db_reader_node."""

    @pytest.mark.asyncio
    async def test_skips_unknown_tables(self):
        """db_reader should skip table names not in TABLE_MODEL_MAP."""
        state = _make_state(
            route={"needs_db": True, "db_tables": ["nonexistent_table", "fake_table"]},
        )
        db = AsyncMock(spec=AsyncSession)
        await db_reader_node(state, db)
        # No DB queries should have been made for unknown tables
        assert state.db_results == {}

    @pytest.mark.asyncio
    async def test_skips_when_not_needed(self):
        """db_reader should skip when needs_db is False."""
        state = _make_state(route={"needs_db": False, "db_tables": []})
        db = AsyncMock(spec=AsyncSession)
        await db_reader_node(state, db)
        assert state.db_results is None

    @pytest.mark.asyncio
    async def test_limits_to_three_tables(self):
        """db_reader should only query max 3 tables."""
        state = _make_state(
            route={
                "needs_db": True,
                "db_tables": ["orders", "payments", "refunds", "payouts", "disputes"],
            },
        )
        db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=mock_result)

        await db_reader_node(state, db)
        # Should have called execute at most 3 times (for first 3 valid tables)
        assert db.execute.call_count <= 3


class TestToolCallerNode:
    """Test the tool_caller_node."""

    @pytest.mark.asyncio
    async def test_blocked_tool(self):
        """Disallowed tools should return error without executing."""
        state = _make_state(
            route={"needs_tool": True, "tool_name": "schedule.create", "tool_params": {}},
        )
        db = AsyncMock(spec=AsyncSession)

        await tool_caller_node(state, db, "test-correlation-id")

        assert state.tool_results is not None
        assert "error" in state.tool_results[0]
        assert "not allowed" in state.tool_results[0]["error"]

    @pytest.mark.asyncio
    async def test_allowed_tool_calls_mcp(self):
        """Allowed tools should route through mcp_server.call_tool."""
        state = _make_state(
            route={
                "needs_tool": True,
                "tool_name": "data.sample_table_read",
                "tool_params": {"table": "orders"},
            },
        )
        db = AsyncMock(spec=AsyncSession)
        mock_result = {"data": [{"id": "1"}]}

        with patch("app.services.chat.nodes.mcp_server") as mock_mcp:
            mock_mcp.call_tool = AsyncMock(return_value=mock_result)
            await tool_caller_node(state, db, "test-correlation-id")

        assert state.tool_results is not None
        assert state.tool_results[0]["result"] == mock_result
        assert state.tool_calls_log is not None
        assert state.tool_calls_log[0]["tool"] == "data.sample_table_read"
        assert "duration_ms" in state.tool_calls_log[0]

    @pytest.mark.asyncio
    async def test_skips_when_not_needed(self):
        """tool_caller should skip when needs_tool is False."""
        state = _make_state(route={"needs_tool": False})
        db = AsyncMock(spec=AsyncSession)
        await tool_caller_node(state, db, "test-correlation-id")
        assert state.tool_results is None


class TestSynthesizerNode:
    """Test the synthesizer_node."""

    @pytest.mark.asyncio
    async def test_generates_response(self):
        """Synthesizer should produce a response from context."""
        state = _make_state(
            doc_chunks=[{"title": "API Guide", "content": "Orders are synced daily.", "source_path": "api.md"}],
            db_results={"orders": [{"id": "1", "status": "active"}]},
        )
        mock_response = _mock_anthropic_response("Based on the documentation, orders are synced daily.")

        with patch("app.services.chat.nodes._get_anthropic_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(return_value=mock_response)
            await synthesizer_node(state)

        assert state.response == "Based on the documentation, orders are synced daily."
        assert state.citations is not None
        assert len(state.citations) == 2  # 1 doc + 1 table

    @pytest.mark.asyncio
    async def test_no_context(self):
        """Synthesizer should work with no context."""
        state = _make_state()
        mock_response = _mock_anthropic_response("I don't have enough information to answer that.")

        with patch("app.services.chat.nodes._get_anthropic_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(return_value=mock_response)
            await synthesizer_node(state)

        assert state.response is not None
        assert state.citations is None
