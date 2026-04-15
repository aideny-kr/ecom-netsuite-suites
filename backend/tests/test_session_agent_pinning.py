"""Tests for session-level agent pinning — follow-ups stay with the same agent."""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "app" / "services" / "chat" / "agents" / "configs"


class TestPinningInSelectAgent:
    @pytest.fixture(autouse=True)
    def _mock_active_connectors(self):
        with patch(
            "app.services.chat.agents.agent_registry._get_active_connectors",
            AsyncMock(return_value={"bigquery"}),
        ):
            yield

    @pytest.mark.asyncio
    async def test_follow_up_uses_pinned_agent(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)
            result = await _select_agent(
                query="is there another column we can use?",
                tenant_id=uuid.uuid4(),
                db=mock_db,
                adapter=AsyncMock(),
                previous_agent_id="bi-agent",
            )
            assert result == "bi-agent"
        finally:
            _agent_registry.configs.clear()

    @pytest.mark.asyncio
    async def test_no_pin_routes_normally(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)
            with patch("app.services.chat.orchestrator.SemanticRouter") as MockSem:
                MockSem.return_value.route = AsyncMock(return_value="unified-agent")
                result = await _select_agent(
                    query="hello",
                    tenant_id=uuid.uuid4(),
                    db=mock_db,
                    adapter=AsyncMock(),
                    previous_agent_id=None,
                )
            assert result is None
        finally:
            _agent_registry.configs.clear()

    @pytest.mark.asyncio
    async def test_explicit_match_overrides_pin(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)
            result = await _select_agent(
                query="what's the markup on SKU-1234?",
                tenant_id=uuid.uuid4(),
                db=mock_db,
                adapter=AsyncMock(),
                previous_agent_id="bi-agent",
            )
            assert result == "pricing-agent"
        finally:
            _agent_registry.configs.clear()

    @pytest.mark.asyncio
    async def test_unified_pin_does_not_stick(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)
            result = await _select_agent(
                query="what's our revenue by region?",
                tenant_id=uuid.uuid4(),
                db=mock_db,
                adapter=AsyncMock(),
                previous_agent_id="unified-agent",
            )
            assert result == "bi-agent"
        finally:
            _agent_registry.configs.clear()

    @pytest.mark.asyncio
    async def test_financial_ignores_pin(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            result = await _select_agent(
                query="income statement",
                tenant_id=uuid.uuid4(),
                db=mock_db,
                adapter=AsyncMock(),
                is_financial=True,
                previous_agent_id="bi-agent",
            )
            assert result is None
        finally:
            _agent_registry.configs.clear()

    @pytest.mark.asyncio
    async def test_unknown_pinned_agent_ignored(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)
            with patch("app.services.chat.orchestrator.SemanticRouter") as MockSem:
                MockSem.return_value.route = AsyncMock(return_value="unified-agent")
                result = await _select_agent(
                    query="what about this?",
                    tenant_id=uuid.uuid4(),
                    db=mock_db,
                    adapter=AsyncMock(),
                    previous_agent_id="deleted-agent",
                )
            assert result is None
        finally:
            _agent_registry.configs.clear()

    @pytest.mark.asyncio
    async def test_bigquery_followup_stays(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)
            # Turn 1
            r1 = await _select_agent(
                query="show me monthly revenue from BigQuery",
                tenant_id=uuid.uuid4(),
                db=mock_db,
                adapter=AsyncMock(),
            )
            assert r1 == "bi-agent"
            # Turn 2 with pin
            r2 = await _select_agent(
                query="can you break that down by product?",
                tenant_id=uuid.uuid4(),
                db=mock_db,
                adapter=AsyncMock(),
                previous_agent_id="bi-agent",
            )
            assert r2 == "bi-agent"
        finally:
            _agent_registry.configs.clear()

    @pytest.mark.asyncio
    async def test_pricing_followup_stays(self):
        from app.services.chat.orchestrator import _agent_registry, _select_agent

        _agent_registry.load_configs(CONFIGS_DIR)
        try:
            mock_db = AsyncMock()
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)
            result = await _select_agent(
                query="what about the tariff on that?",
                tenant_id=uuid.uuid4(),
                db=mock_db,
                adapter=AsyncMock(),
                previous_agent_id="pricing-agent",
            )
            assert result == "pricing-agent"
        finally:
            _agent_registry.configs.clear()


class TestInferPreviousAgent:
    """Tests for _infer_previous_agent — extracts agent from tool_calls on messages."""

    def _make_msg(self, role: str, tool_calls=None):
        msg = MagicMock()
        msg.role = role
        msg.tool_calls = tool_calls
        return msg

    def test_infer_from_explicit_agent_field(self):
        from app.services.chat.orchestrator import _infer_previous_agent

        messages = [
            self._make_msg("user"),
            self._make_msg("assistant", tool_calls=[{"tool": "bigquery_sql", "agent": "bi-agent"}]),
        ]
        assert _infer_previous_agent(messages) == "bi-agent"

    def test_infer_from_tool_name_fallback(self):
        from app.services.chat.orchestrator import _infer_previous_agent

        messages = [
            self._make_msg("user"),
            self._make_msg("assistant", tool_calls=[{"tool": "bigquery_schema"}]),
        ]
        assert _infer_previous_agent(messages) == "bi-agent"

    def test_no_tool_calls_returns_none(self):
        from app.services.chat.orchestrator import _infer_previous_agent

        messages = [
            self._make_msg("user"),
            self._make_msg("assistant", tool_calls=None),
        ]
        assert _infer_previous_agent(messages) is None

    def test_empty_messages_returns_none(self):
        from app.services.chat.orchestrator import _infer_previous_agent

        assert _infer_previous_agent([]) is None

    def test_unified_agent_ignored(self):
        from app.services.chat.orchestrator import _infer_previous_agent

        messages = [
            self._make_msg("assistant", tool_calls=[{"tool": "netsuite_suiteql", "agent": "unified"}]),
        ]
        assert _infer_previous_agent(messages) is None

    def test_only_checks_most_recent_assistant(self):
        from app.services.chat.orchestrator import _infer_previous_agent

        messages = [
            self._make_msg("assistant", tool_calls=[{"tool": "bigquery_sql", "agent": "bi-agent"}]),
            self._make_msg("user"),
            self._make_msg("assistant", tool_calls=[{"tool": "netsuite_suiteql", "agent": "unified"}]),
        ]
        # Should check the last assistant (unified), not the first (bi-agent)
        assert _infer_previous_agent(messages) is None

    def test_skips_user_messages_to_find_assistant(self):
        from app.services.chat.orchestrator import _infer_previous_agent

        messages = [
            self._make_msg("assistant", tool_calls=[{"tool": "bigquery_sql", "agent": "bi-agent"}]),
            self._make_msg("user"),
        ]
        assert _infer_previous_agent(messages) == "bi-agent"

    def test_non_dict_tool_calls_handled(self):
        from app.services.chat.orchestrator import _infer_previous_agent

        messages = [
            self._make_msg("assistant", tool_calls=["not-a-dict"]),
        ]
        assert _infer_previous_agent(messages) is None
