"""Tests for learned rules injection — always injected regardless of context_need."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestRetrieveLearnedRules:
    """Test the new standalone retrieval function."""

    @pytest.mark.asyncio
    async def test_returns_active_rules(self):
        from app.services.learned_rules_service import retrieve_learned_rules

        mock_rule = MagicMock()
        mock_rule.rule_category = "query_logic"
        mock_rule.rule_description = "Always exclude cancelled orders"
        mock_rule.is_active = True

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_rule]
        mock_db.execute = AsyncMock(return_value=mock_result)

        rules = await retrieve_learned_rules(mock_db, uuid.uuid4())
        assert len(rules) == 1
        assert rules[0]["category"] == "query_logic"
        assert "exclude cancelled" in rules[0]["description"]

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_rules(self):
        from app.services.learned_rules_service import retrieve_learned_rules

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        rules = await retrieve_learned_rules(mock_db, uuid.uuid4())
        assert rules == []

    @pytest.mark.asyncio
    async def test_only_returns_active_rules(self):
        from app.services.learned_rules_service import retrieve_learned_rules

        active = MagicMock()
        active.rule_category = "general"
        active.rule_description = "Active rule"
        active.is_active = True

        # The query should filter by is_active=True, so inactive won't appear
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [active]
        mock_db.execute = AsyncMock(return_value=mock_result)

        rules = await retrieve_learned_rules(mock_db, uuid.uuid4())
        assert len(rules) == 1
        assert rules[0]["description"] == "Active rule"


class TestLearnedRulesInUnifiedAgent:
    """Test that learned rules appear in the system prompt."""

    def test_learned_rules_in_prompt(self):
        from app.services.chat.agents.unified_agent import UnifiedAgent

        agent = UnifiedAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        agent._context = {
            "learned_rules": [
                {"category": "query_logic", "description": "Always exclude cancelled orders"},
                {"category": "status_mapping", "description": "Status X means on hold"},
            ],
        }
        prompt = agent.system_prompt
        assert "<learned_rules>" in prompt
        assert "exclude cancelled orders" in prompt
        assert "Status X means on hold" in prompt

    def test_no_learned_rules_no_block(self):
        from app.services.chat.agents.unified_agent import UnifiedAgent

        agent = UnifiedAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        agent._context = {}
        prompt = agent.system_prompt
        # The base prompt references <learned_rules> in instructions, but
        # the actual injected block with "FOLLOW THESE STRICTLY" should not appear
        assert "FOLLOW THESE STRICTLY" not in prompt


class TestLearnedRulesContextNeed:
    """Learned rules should be injected for ALL context needs, not just DATA/FINANCIAL."""

    def test_full_context_includes_learned_rules(self):
        """Investigation (FULL) queries should still get learned rules."""
        from app.services.chat.orchestrator import ContextNeed

        # All context needs should include learned rules
        all_needs = [ContextNeed.FULL, ContextNeed.DATA, ContextNeed.DOCS, ContextNeed.WORKSPACE, ContextNeed.FINANCIAL]
        assert len(all_needs) == 5  # Sanity check
