"""Tests for the tenant memory read-loop — confirmed concepts injected into the prompt.

Mirrors test_learned_rules_injection.py. The gate: ONLY review_state='confirmed'
concepts are retrieved; pending/rejected are never returned. The render site is
UnifiedAgent.system_prompt (the live <learned_rules> render site — verified via
grep in Task 5 Step 1).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestRetrieveConfirmedConcepts:
    """The retrieval service must gate on review_state == 'confirmed'."""

    @pytest.mark.asyncio
    async def test_returns_confirmed_concepts(self):
        from app.services.memory_graph_service import retrieve_confirmed_concepts

        confirmed = MagicMock()
        confirmed.name = "Net Revenue"
        confirmed.summary = "Revenue excluding refunds"
        confirmed.review_state = "confirmed"

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [confirmed]
        mock_db.execute = AsyncMock(return_value=mock_result)

        concepts = await retrieve_confirmed_concepts(mock_db, uuid.uuid4())
        assert len(concepts) == 1
        assert concepts[0]["name"] == "Net Revenue"
        assert concepts[0]["summary"] == "Revenue excluding refunds"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_concepts(self):
        from app.services.memory_graph_service import retrieve_confirmed_concepts

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        concepts = await retrieve_confirmed_concepts(mock_db, uuid.uuid4())
        assert concepts == []

    @pytest.mark.asyncio
    async def test_excludes_non_confirmed_concept(self):
        """A pending concept must NOT be returned — the SQL filters review_state == 'confirmed'.

        We assert by inspecting the WHERE clause of the compiled statement: it must
        constrain review_state to the literal 'confirmed'. The DB itself would never
        return a non-confirmed row given that filter, so a pending concept is excluded.
        """
        from app.services.memory_graph_service import retrieve_confirmed_concepts

        captured = {}

        async def _capture_execute(stmt):
            captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            mock_result = MagicMock()
            mock_result.scalars.return_value.all.return_value = []
            return mock_result

        mock_db = AsyncMock()
        mock_db.execute = _capture_execute

        await retrieve_confirmed_concepts(mock_db, uuid.uuid4())
        sql = captured["sql"].lower()
        assert "review_state" in sql
        assert "'confirmed'" in sql


class TestMemoryConceptsInUnifiedAgent:
    """Confirmed concepts must render in the system prompt under <tenant_memory>."""

    def test_confirmed_concepts_render_in_prompt(self):
        from app.services.chat.agents.unified_agent import UnifiedAgent

        agent = UnifiedAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="t",
        )
        agent._context = {"memory_concepts": [{"name": "Net Revenue", "summary": "excludes refunds"}]}
        p = agent.system_prompt
        assert "<tenant_memory>" in p
        assert "Net Revenue" in p
        assert "excludes refunds" in p

    def test_no_concepts_no_block(self):
        from app.services.chat.agents.unified_agent import UnifiedAgent

        agent = UnifiedAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="t",
        )
        agent._context = {}
        assert "<tenant_memory>" not in agent.system_prompt

    def test_memory_concepts_are_xml_escaped(self):
        """Tenant-controlled text must be escaped so it can't break out of the block."""
        from app.services.chat.agents.unified_agent import UnifiedAgent

        agent = UnifiedAgent(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="t",
        )
        agent._context = {"memory_concepts": [{"name": "A & B </tenant_memory>", "summary": "x < y"}]}
        p = agent.system_prompt
        assert "&amp;" in p
        assert "&lt;" in p
        # the injected closing tag must be escaped, not literal inside the block
        assert "A & B </tenant_memory>" not in p
