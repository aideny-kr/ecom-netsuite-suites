"""Tests for AgentRegistry — lifecycle management of specialized agents."""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from app.services.chat.agents.agent_registry import AgentRegistry
from app.services.chat.agents.specialized_agent import SpecializedAgent


def _write_yaml_config(tmp_path: Path, agent_id: str, **extra) -> Path:
    data = {
        "agent_id": agent_id,
        "display_name": agent_id.replace("-", " ").title(),
        "description": f"Agent for {agent_id}",
        **extra,
    }
    path = tmp_path / f"{agent_id}.yaml"
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


class TestAgentRegistryLoading:
    def test_load_configs_from_directory(self, tmp_path):
        _write_yaml_config(tmp_path, "pricing-agent")
        _write_yaml_config(tmp_path, "inventory-agent")
        registry = AgentRegistry()
        registry.load_configs(tmp_path)
        assert len(registry.configs) == 2
        assert "pricing-agent" in registry.configs
        assert "inventory-agent" in registry.configs

    def test_load_configs_ignores_non_yaml(self, tmp_path):
        _write_yaml_config(tmp_path, "pricing-agent")
        (tmp_path / "README.md").write_text("# Not a config")
        registry = AgentRegistry()
        registry.load_configs(tmp_path)
        assert len(registry.configs) == 1


class TestAgentRegistryEnabled:
    @pytest.mark.asyncio
    async def test_get_enabled_agents_all_by_default(self, tmp_path):
        _write_yaml_config(tmp_path, "pricing-agent")
        _write_yaml_config(tmp_path, "inventory-agent")
        registry = AgentRegistry()
        registry.load_configs(tmp_path)

        # Mock DB returning no overrides for this tenant
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        enabled = await registry.get_enabled_agents(mock_db, uuid.uuid4())
        assert len(enabled) == 2

    @pytest.mark.asyncio
    async def test_get_enabled_agents_respects_db_disable(self, tmp_path):
        _write_yaml_config(tmp_path, "pricing-agent")
        _write_yaml_config(tmp_path, "inventory-agent")
        registry = AgentRegistry()
        registry.load_configs(tmp_path)

        # Mock DB returning pricing-agent as disabled
        mock_db = AsyncMock()
        mock_row = MagicMock()
        mock_row.agent_id = "pricing-agent"
        mock_row.is_enabled = False
        mock_row.override_config = {}
        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row]
        mock_db.execute = AsyncMock(return_value=mock_result)

        enabled = await registry.get_enabled_agents(mock_db, uuid.uuid4())
        agent_ids = [a.agent_id for a in enabled]
        assert "pricing-agent" not in agent_ids
        assert "inventory-agent" in agent_ids


class TestAgentRegistryInstantiate:
    def test_instantiate_returns_specialized_agent(self, tmp_path):
        _write_yaml_config(tmp_path, "pricing-agent", tool_ids=["netsuite_suiteql"])
        registry = AgentRegistry()
        registry.load_configs(tmp_path)

        agent = registry.instantiate(
            agent_id="pricing-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
        )
        assert isinstance(agent, SpecializedAgent)
        assert agent.agent_name == "pricing-agent"

    def test_instantiate_merges_tenant_overrides(self, tmp_path):
        _write_yaml_config(tmp_path, "pricing-agent", max_steps=6)
        registry = AgentRegistry()
        registry.load_configs(tmp_path)

        agent = registry.instantiate(
            agent_id="pricing-agent",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            correlation_id="test",
            overrides={"max_steps": 12},
        )
        assert agent.max_steps == 12

    def test_instantiate_unknown_agent_raises(self, tmp_path):
        registry = AgentRegistry()
        registry.load_configs(tmp_path)  # empty dir

        with pytest.raises(KeyError):
            registry.instantiate(
                agent_id="nonexistent",
                tenant_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                correlation_id="test",
            )


class TestAgentRegistryHealth:
    def test_agent_healthy_true_below_threshold(self):
        registry = AgentRegistry()
        assert registry.is_healthy(error_count=2, success_count=98) is True

    def test_agent_healthy_false_above_threshold(self):
        registry = AgentRegistry()
        assert registry.is_healthy(error_count=6, success_count=94) is False

    def test_agent_healthy_zero_calls(self):
        registry = AgentRegistry()
        assert registry.is_healthy(error_count=0, success_count=0) is True


class TestGetActiveConnectors:
    @pytest.mark.asyncio
    async def test_returns_active_enabled_connectors(self):
        from app.services.chat.agents.agent_registry import _get_active_connectors

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [("bigquery",), ("netsuite_mcp",)]
        mock_db.execute = AsyncMock(return_value=mock_result)

        connectors = await _get_active_connectors(mock_db, uuid.uuid4())
        assert connectors == {"bigquery", "netsuite_mcp"}

    @pytest.mark.asyncio
    async def test_returns_empty_set_when_no_connectors(self):
        from app.services.chat.agents.agent_registry import _get_active_connectors

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        connectors = await _get_active_connectors(mock_db, uuid.uuid4())
        assert connectors == set()

    @pytest.mark.asyncio
    async def test_query_filters_by_tenant_enabled_and_active(self):
        """The SQL query must filter by tenant_id, is_enabled=True, status='active'."""
        from app.services.chat.agents.agent_registry import _get_active_connectors

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        tenant_id = uuid.uuid4()
        await _get_active_connectors(mock_db, tenant_id)

        assert mock_db.execute.call_count == 1
        from sqlalchemy.dialects import postgresql

        stmt = mock_db.execute.call_args[0][0]
        compiled = str(stmt.compile(dialect=postgresql.dialect()))
        assert "mcp_connectors" in compiled
        assert "provider" in compiled
        assert "tenant_id" in compiled
        assert "is_enabled" in compiled
        assert "status" in compiled


class TestGetEnabledAgentsConnectorFilter:
    @pytest.mark.asyncio
    async def test_agent_included_when_required_connector_active(self, tmp_path):
        """Tenant has active BigQuery → bi-agent included."""
        _write_yaml_config(tmp_path, "bi-agent", requires_connector=["bigquery"])
        _write_yaml_config(tmp_path, "other-agent")
        registry = AgentRegistry()
        registry.load_configs(tmp_path)

        mock_db = AsyncMock()
        overrides_result = MagicMock()
        overrides_result.all.return_value = []
        connectors_result = MagicMock()
        connectors_result.all.return_value = [("bigquery",)]

        mock_db.execute = AsyncMock(side_effect=[overrides_result, connectors_result])

        enabled = await registry.get_enabled_agents(mock_db, uuid.uuid4())
        agent_ids = {a.agent_id for a in enabled}
        assert "bi-agent" in agent_ids
        assert "other-agent" in agent_ids

    @pytest.mark.asyncio
    async def test_agent_filtered_when_required_connector_missing(self, tmp_path):
        """Tenant has NO BigQuery → bi-agent excluded."""
        _write_yaml_config(tmp_path, "bi-agent", requires_connector=["bigquery"])
        _write_yaml_config(tmp_path, "other-agent")
        registry = AgentRegistry()
        registry.load_configs(tmp_path)

        mock_db = AsyncMock()
        overrides_result = MagicMock()
        overrides_result.all.return_value = []
        connectors_result = MagicMock()
        connectors_result.all.return_value = []

        mock_db.execute = AsyncMock(side_effect=[overrides_result, connectors_result])

        enabled = await registry.get_enabled_agents(mock_db, uuid.uuid4())
        agent_ids = {a.agent_id for a in enabled}
        assert "bi-agent" not in agent_ids
        assert "other-agent" in agent_ids

    @pytest.mark.asyncio
    async def test_any_of_semantics_one_match_enables(self, tmp_path):
        """Agent requires [bigquery, snowflake], tenant has only snowflake → included."""
        _write_yaml_config(tmp_path, "warehouse-agent", requires_connector=["bigquery", "snowflake"])
        registry = AgentRegistry()
        registry.load_configs(tmp_path)

        mock_db = AsyncMock()
        overrides_result = MagicMock()
        overrides_result.all.return_value = []
        connectors_result = MagicMock()
        connectors_result.all.return_value = [("snowflake",)]

        mock_db.execute = AsyncMock(side_effect=[overrides_result, connectors_result])

        enabled = await registry.get_enabled_agents(mock_db, uuid.uuid4())
        assert "warehouse-agent" in {a.agent_id for a in enabled}

    @pytest.mark.asyncio
    async def test_resolver_failure_fails_open(self, tmp_path, caplog):
        """Resolver raises → filter skipped → agents included (fail-open)."""
        import logging

        _write_yaml_config(tmp_path, "bi-agent", requires_connector=["bigquery"])
        registry = AgentRegistry()
        registry.load_configs(tmp_path)

        mock_db = AsyncMock()
        overrides_result = MagicMock()
        overrides_result.all.return_value = []

        mock_db.execute = AsyncMock(side_effect=[overrides_result, RuntimeError("connection lost")])

        with caplog.at_level(logging.WARNING):
            enabled = await registry.get_enabled_agents(mock_db, uuid.uuid4())

        assert "bi-agent" in {a.agent_id for a in enabled}
        assert any("skipping connector filter" in rec.message.lower() for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_override_of_requires_connector_is_honored(self, tmp_path):
        """DB override that clears requires_connector re-enables a filtered agent."""
        _write_yaml_config(tmp_path, "bi-agent", requires_connector=["bigquery"])
        registry = AgentRegistry()
        registry.load_configs(tmp_path)

        mock_db = AsyncMock()
        # Overrides row: is_enabled=True, override clears requires_connector to []
        override_row = MagicMock()
        override_row.agent_id = "bi-agent"
        override_row.is_enabled = True
        override_row.override_config = {"requires_connector": []}
        overrides_result = MagicMock()
        overrides_result.all.return_value = [override_row]

        # No active connectors — bi-agent would be filtered WITHOUT the override
        connectors_result = MagicMock()
        connectors_result.all.return_value = []

        mock_db.execute = AsyncMock(side_effect=[overrides_result, connectors_result])

        enabled = await registry.get_enabled_agents(mock_db, uuid.uuid4())
        # With the override clearing requires_connector, bi-agent is enabled
        # even though no active connectors exist. Locks in merge-before-filter order.
        assert "bi-agent" in {a.agent_id for a in enabled}
