"""Default for the chat orchestration step cap.

Locks in the bump: investigation-mode step budget 15 -> 40 in the unified
agent, validator ceiling 20 -> 50 (headroom above 40) for yaml-loaded
configs, and CHAT_MAX_TOOL_CALLS_PER_TURN default 5 -> 40 in Settings.
"""

import uuid

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig
from app.services.chat.agents.unified_agent import UnifiedAgent


def test_settings_chat_max_tool_calls_per_turn_default_is_40():
    value = Settings().CHAT_MAX_TOOL_CALLS_PER_TURN
    assert value == 40


def test_agent_yaml_config_validator_allows_max_steps_40():
    cfg = AgentYAMLConfig(
        agent_id="test-agent",
        display_name="Test",
        description="x",
        max_steps=40,
    )
    assert cfg.max_steps == 40


def test_unified_agent_max_steps_investigation_mode_is_40():
    agent = UnifiedAgent(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        correlation_id="test",
        context_need="full",
    )
    assert agent.max_steps == 40


def test_unified_agent_max_steps_normal_mode_unchanged():
    agent = UnifiedAgent(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        correlation_id="test",
        context_need="LIGHT",
    )
    assert agent.max_steps == 12


def test_agent_yaml_config_validator_still_rejects_excessive_max_steps():
    with pytest.raises(ValidationError):
        AgentYAMLConfig(
            agent_id="test-agent",
            display_name="Test",
            description="x",
            max_steps=999,
        )
