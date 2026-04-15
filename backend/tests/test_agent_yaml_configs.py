"""Tests that validate YAML agent config files parse correctly.

Only uses AgentYAMLConfig from agent_yaml_config.py — no registry needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "app" / "services" / "chat" / "agents" / "configs"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "app" / "services" / "chat" / "agents" / "prompts"


def _load_all_configs() -> list[tuple[str, AgentYAMLConfig]]:
    """Load all YAML configs from the configs directory."""
    configs = []
    if not CONFIGS_DIR.exists():
        return configs
    for yaml_file in sorted(CONFIGS_DIR.glob("*.yaml")):
        config = AgentYAMLConfig.from_yaml(yaml_file)
        configs.append((yaml_file.name, config))
    return configs


class TestAllYAMLConfigs:
    def test_all_yaml_configs_parse(self):
        """Every .yaml in agents/configs/ loads via AgentYAMLConfig.from_yaml()."""
        configs = _load_all_configs()
        assert len(configs) > 0, "No YAML config files found in configs/"

    def test_all_configs_have_unique_agent_id(self):
        """No duplicate agent_ids across all config files."""
        configs = _load_all_configs()
        agent_ids = [c.agent_id for _, c in configs]
        dupes = [aid for aid in agent_ids if agent_ids.count(aid) > 1]
        assert not dupes, f"Duplicate agent_ids found: {set(dupes)}"

    def test_all_routing_patterns_compile(self):
        """Every routing_rule.pattern compiles as valid regex."""
        for filename, config in _load_all_configs():
            for rule in config.routing_rules:
                try:
                    re.compile(rule.pattern)
                except re.error as e:
                    pytest.fail(f"{filename}: invalid regex '{rule.pattern}': {e}")

    def test_no_identical_patterns_across_agents(self):
        """No exact same regex pattern appears in 2+ configs."""
        pattern_to_agents: dict[str, list[str]] = {}
        for filename, config in _load_all_configs():
            for rule in config.routing_rules:
                pattern_to_agents.setdefault(rule.pattern, []).append(config.agent_id)
        dupes = {p: agents for p, agents in pattern_to_agents.items() if len(agents) > 1}
        assert not dupes, f"Duplicate patterns across agents: {dupes}"

    def test_all_prompt_files_exist(self):
        """Every config with prompt_file points to a real file in agents/prompts/."""
        for filename, config in _load_all_configs():
            if config.prompt_file:
                prompt_path = PROMPTS_DIR / config.prompt_file
                assert prompt_path.exists(), (
                    f"{filename}: prompt_file '{config.prompt_file}' not found at {prompt_path}"
                )

    def test_unified_agent_config_exists(self):
        """A config file with agent_id='unified-agent' exists."""
        configs = _load_all_configs()
        agent_ids = [c.agent_id for _, c in configs]
        assert "unified-agent" in agent_ids, "No config with agent_id='unified-agent' found"

    def test_unified_agent_has_no_routing_rules(self):
        """unified-agent config has empty routing_rules (it's the fallback)."""
        for _, config in _load_all_configs():
            if config.agent_id == "unified-agent":
                assert config.routing_rules == [], "unified-agent should have no routing_rules"
                return
        pytest.fail("unified-agent config not found")

    def test_specialized_agents_have_routing_rules(self):
        """Non-unified agents have >=1 routing_rule."""
        for filename, config in _load_all_configs():
            if config.agent_id == "unified-agent":
                continue
            assert len(config.routing_rules) >= 1, (
                f"{filename}: specialized agent '{config.agent_id}' has no routing_rules"
            )

    def test_max_steps_within_bounds(self):
        """All configs have 1 <= max_steps <= 20."""
        for filename, config in _load_all_configs():
            assert 1 <= config.max_steps <= 20, f"{filename}: max_steps={config.max_steps} out of bounds [1,20]"


class TestRequiresConnectorField:
    def test_default_is_empty_list(self):
        config = AgentYAMLConfig(
            agent_id="x-agent",
            display_name="X",
            description="X",
        )
        assert config.requires_connector == []

    def test_string_coerced_to_single_element_list(self):
        config = AgentYAMLConfig(
            agent_id="x-agent",
            display_name="X",
            description="X",
            requires_connector="bigquery",
        )
        assert config.requires_connector == ["bigquery"]

    def test_list_passthrough(self):
        config = AgentYAMLConfig(
            agent_id="x-agent",
            display_name="X",
            description="X",
            requires_connector=["bigquery", "snowflake"],
        )
        assert config.requires_connector == ["bigquery", "snowflake"]

    def test_none_becomes_empty_list(self):
        config = AgentYAMLConfig(
            agent_id="x-agent",
            display_name="X",
            description="X",
            requires_connector=None,
        )
        assert config.requires_connector == []

    def test_yaml_string_form_parses(self, tmp_path):
        yaml_file = tmp_path / "x.yaml"
        yaml_file.write_text(
            "agent_id: x-agent\n"
            "display_name: X\n"
            "description: X\n"
            "requires_connector: bigquery\n"
        )
        config = AgentYAMLConfig.from_yaml(yaml_file)
        assert config.requires_connector == ["bigquery"]

    def test_yaml_list_form_parses(self, tmp_path):
        yaml_file = tmp_path / "x.yaml"
        yaml_file.write_text(
            "agent_id: x-agent\n"
            "display_name: X\n"
            "description: X\n"
            "requires_connector: [bigquery, snowflake]\n"
        )
        config = AgentYAMLConfig.from_yaml(yaml_file)
        assert config.requires_connector == ["bigquery", "snowflake"]
