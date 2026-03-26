"""Tests for the reference_previous_result tool."""
import json
import pytest
from unittest.mock import patch, AsyncMock
from app.mcp.tools.result_reference_tool import execute_reference_previous_result


class TestReferencePreviousResult:
    @pytest.mark.asyncio
    async def test_returns_cached_data(self):
        from app.services.chat.result_cache import CachedResult
        mock_result = CachedResult(
            message_id="msg-1", conversation_id="conv-1", result_type="suiteql",
            columns=["name", "amount"], rows=[["Widget", 100], ["Gadget", 200]], row_count=2,
        )
        with patch("app.mcp.tools.result_reference_tool.get_latest_result", new_callable=AsyncMock, return_value=mock_result):
            result = await execute_reference_previous_result("conv-1")
        parsed = json.loads(result)
        assert parsed["success"] is True
        assert parsed["columns"] == ["name", "amount"]
        assert len(parsed["rows"]) == 2

    @pytest.mark.asyncio
    async def test_no_cache_returns_error(self):
        with patch("app.mcp.tools.result_reference_tool.get_latest_result", new_callable=AsyncMock, return_value=None):
            result = await execute_reference_previous_result("conv-1")
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "no cached" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_specific_message_id(self):
        from app.services.chat.result_cache import CachedResult
        mock_result = CachedResult(
            message_id="msg-5", conversation_id="conv-1", result_type="bigquery",
            columns=["date", "revenue"], rows=[["2026-01", 50000]], row_count=1,
        )
        with patch("app.mcp.tools.result_reference_tool.get_result_by_message", new_callable=AsyncMock, return_value=mock_result):
            result = await execute_reference_previous_result("conv-1", message_id="msg-5")
        parsed = json.loads(result)
        assert parsed["success"] is True
        assert parsed["result_type"] == "bigquery"
