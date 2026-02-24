"""Tests for the save_learned_rule MCP tool."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp.tools.save_learned_rule import execute


@pytest.fixture
def base_context():
    return {
        "tenant_id": str(uuid.uuid4()),
        "actor_id": str(uuid.uuid4()),
        "db": AsyncMock(),
    }


class TestSaveLearnedRule:
    """Test admin-only rule persistence."""

    @pytest.mark.asyncio
    async def test_admin_can_save_rule(self, base_context):
        with patch("app.core.dependencies.has_permission", return_value=True) as mock_perm:
            result = await execute(
                {"rule_description": "Always show Value not ID", "rule_category": "output_preference"},
                context=base_context,
            )
        assert result["status"] == "saved"
        assert "rule_id" in result
        mock_perm.assert_awaited_once()
        base_context["db"].add.assert_called_once()
        base_context["db"].flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_admin_gets_session_only(self, base_context):
        with patch("app.core.dependencies.has_permission", return_value=False):
            result = await execute(
                {"rule_description": "Always show Value not ID"},
                context=base_context,
            )
        assert result["status"] == "session_only"
        base_context["db"].add.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_description_rejected(self, base_context):
        result = await execute({"rule_description": ""}, context=base_context)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_description_rejected(self, base_context):
        result = await execute({}, context=base_context)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_db_returns_error(self):
        result = await execute(
            {"rule_description": "test"},
            context={"tenant_id": str(uuid.uuid4()), "actor_id": str(uuid.uuid4())},
        )
        assert result["error"] == "Database session not available"

    @pytest.mark.asyncio
    async def test_default_category_is_general(self, base_context):
        with patch("app.core.dependencies.has_permission", return_value=True):
            result = await execute(
                {"rule_description": "Test rule"},
                context=base_context,
            )
        assert result["status"] == "saved"
        # Verify the model was created with category "general"
        call_args = base_context["db"].add.call_args
        rule = call_args[0][0]
        assert rule.rule_category == "general"
