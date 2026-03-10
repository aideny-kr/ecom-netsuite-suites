"""Tests for AgentResult confidence_score field."""

from app.services.chat.agents.base_agent import AgentResult


def test_agent_result_accepts_confidence_score():
    result = AgentResult(success=True, data="text", confidence_score=4.2)
    assert result.confidence_score == 4.2


def test_agent_result_confidence_defaults_none():
    result = AgentResult(success=True, data="text")
    assert result.confidence_score is None
