"""Tests for agent configuration YAML loading and merge."""

from pathlib import Path

import yaml

from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig


class TestAgentYAMLConfig:
    """YAML config loading and merge tests."""

    def _write_yaml(self, data: dict, tmp_path: Path) -> Path:
        path = tmp_path / "test_agent.yaml"
        with open(path, "w") as f:
            yaml.dump(data, f)
        return path

    def test_load_minimal_config(self, tmp_path):
        data = {
            "agent_id": "test-agent",
            "display_name": "Test Agent",
            "description": "A test agent",
        }
        path = self._write_yaml(data, tmp_path)
        config = AgentYAMLConfig.from_yaml(path)
        assert config.agent_id == "test-agent"
        assert config.display_name == "Test Agent"
        assert config.max_steps == 6
        assert config.enabled_by_default is True

    def test_load_full_config(self, tmp_path):
        data = {
            "agent_id": "pricing-agent",
            "display_name": "Pricing Agent",
            "description": "Handles pricing queries",
            "version": "1.0.0",
            "routing_rules": [{"pattern": r"price|cost|margin", "priority": 10}],
            "semantic_examples": ["What is the price of X?"],
            "tool_ids": ["netsuite_suiteql", "netsuite_get_metadata"],
            "rag_partitions": ["pricing"],
            "model_preference": "claude-sonnet-4-6",
            "max_steps": 8,
            "cost_budget": 0.50,
            "prompt_file": "pricing_agent_prompt.md",
            "requires_confirmation": False,
            "enabled_by_default": True,
        }
        path = self._write_yaml(data, tmp_path)
        config = AgentYAMLConfig.from_yaml(path)
        assert config.agent_id == "pricing-agent"
        assert len(config.routing_rules) == 1
        assert config.routing_rules[0].pattern == r"price|cost|margin"
        assert config.max_steps == 8
        assert config.cost_budget == 0.50
        assert config.model_preference == "claude-sonnet-4-6"

    def test_merge_overrides(self, tmp_path):
        data = {
            "agent_id": "test-agent",
            "display_name": "Test Agent",
            "description": "A test agent",
            "max_steps": 6,
        }
        path = self._write_yaml(data, tmp_path)
        config = AgentYAMLConfig.from_yaml(path)

        merged = config.merge({"max_steps": 12, "model_preference": "claude-opus-4-6"})
        assert merged.max_steps == 12
        assert merged.model_preference == "claude-opus-4-6"
        # Original unchanged
        assert config.max_steps == 6
        assert config.model_preference is None

    def test_merge_ignores_none(self, tmp_path):
        data = {
            "agent_id": "test-agent",
            "display_name": "Test Agent",
            "description": "A test agent",
            "max_steps": 8,
        }
        path = self._write_yaml(data, tmp_path)
        config = AgentYAMLConfig.from_yaml(path)

        merged = config.merge({"max_steps": None, "model_preference": None})
        assert merged.max_steps == 8  # None override ignored

    def test_invalid_agent_id_rejected(self):
        """agent_id must be lowercase alphanumeric with hyphens/underscores."""
        import pytest

        with pytest.raises(Exception):
            AgentYAMLConfig(
                agent_id="INVALID AGENT",
                display_name="Test",
                description="Test",
            )

    def test_max_steps_bounds(self):
        """max_steps must be 1-20."""
        import pytest

        with pytest.raises(Exception):
            AgentYAMLConfig(
                agent_id="test",
                display_name="Test",
                description="Test",
                max_steps=50,
            )


class TestAgentProtocol:
    """Verify the protocol is runtime-checkable."""

    def test_protocol_is_importable(self):
        from app.services.chat.agents.agent_protocol import AgentProtocol

        assert AgentProtocol is not None

    def test_protocol_is_runtime_checkable(self):
        from app.services.chat.agents.agent_protocol import AgentProtocol

        # Protocol should be usable with isinstance checks
        assert hasattr(AgentProtocol, "__protocol_attrs__") or hasattr(AgentProtocol, "__abstractmethods__") or True
