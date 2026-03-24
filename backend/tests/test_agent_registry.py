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
