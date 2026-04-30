"""Verify Plan Mode augmentation + resume directive reach the UnifiedAgent's system prompt.

Codex P1: the orchestrator was appending `plan_mode_augmentation` and
`plan_mode_resume_directive` to a LOCAL `system_prompt` variable. But
`UnifiedAgent.system_prompt` is a property that builds its own prompt from
the agent's _SYSTEM_PROMPT template + tool inventory + metadata + connectors
+ policy. It NEVER reads the orchestrator's local variable, so the
augmentation was dead code on the UnifiedAgent path.

Fix: store the augmentation/directive on the UnifiedAgent instance via a
new attribute, and have the `system_prompt` property emit it.
"""

import uuid

from app.services.chat.agents.unified_agent import UnifiedAgent


def _make_agent() -> UnifiedAgent:
    return UnifiedAgent(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        correlation_id="test-corr",
    )


def test_plan_mode_augmentation_attribute_default_empty():
    """New attribute defaults to empty string so the property never injects junk by default."""
    agent = _make_agent()
    assert hasattr(agent, "_plan_mode_augmentation")
    assert agent._plan_mode_augmentation == ""


def test_plan_mode_resume_directive_attribute_default_empty():
    agent = _make_agent()
    assert hasattr(agent, "_plan_mode_resume_directive")
    assert agent._plan_mode_resume_directive == ""


def test_plan_mode_augmentation_reaches_agent_system_prompt():
    """When the attribute is set, the rendered system_prompt must include it."""
    agent = _make_agent()
    sentinel = "PLAN_MODE_FINANCIAL_CLARIFICATION_SENTINEL_4242"
    agent._plan_mode_augmentation = sentinel

    rendered = agent.system_prompt
    assert sentinel in rendered, "Plan Mode augmentation must reach the agent's rendered system prompt"


def test_plan_mode_resume_directive_reaches_agent_system_prompt():
    agent = _make_agent()
    sentinel = "PLAN_MODE_RESUME_DIRECTIVE_SENTINEL_8888"
    agent._plan_mode_resume_directive = sentinel

    rendered = agent.system_prompt
    assert sentinel in rendered, "Plan Mode resume directive must reach the agent's rendered system prompt"


def test_plan_mode_resume_directive_overrides_augmentation_when_both_set():
    """When both fire, the resume directive comes AFTER the augmentation.

    Per the spec: resume-time directive overrides initial-gate intent.
    """
    agent = _make_agent()
    aug = "PLAN_MODE_AUG_SENTINEL_AAAA"
    directive = "PLAN_MODE_RESUME_SENTINEL_ZZZZ"
    agent._plan_mode_augmentation = aug
    agent._plan_mode_resume_directive = directive

    rendered = agent.system_prompt
    assert aug in rendered
    assert directive in rendered
    # Ordering: augmentation first, resume directive last.
    assert rendered.index(aug) < rendered.index(directive), (
        "Resume directive should appear AFTER augmentation so it can override the initial gate intent"
    )


def test_plan_mode_attributes_omitted_when_empty():
    """Empty strings (the default) MUST NOT add stray newlines/blocks to the prompt."""
    agent_a = _make_agent()
    agent_b = _make_agent()

    # Force one with an attribute set to something distinctive, the other left default.
    agent_b._plan_mode_augmentation = "DISTINCTIVE_PLAN_MODE_TEXT_XYZ"
    rendered_a = agent_a.system_prompt
    rendered_b = agent_b.system_prompt

    assert "DISTINCTIVE_PLAN_MODE_TEXT_XYZ" not in rendered_a
    assert "DISTINCTIVE_PLAN_MODE_TEXT_XYZ" in rendered_b
