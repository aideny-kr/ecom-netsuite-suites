"""Tests for composite confidence scoring service."""

from app.services.confidence_service import CompositeScorer


def test_all_perfect_signals_returns_5():
    scorer = CompositeScorer(
        llm_score=1.0,
        query_pattern_similarity=1.0,
        query_pattern_success_count=50,
        domain_knowledge_similarity=1.0,
        entity_resolution_confidence=1.0,
        tool_success_rate=1.0,
        num_tool_calls=3,
    )
    assert scorer.compute() == 5.0


def test_all_zero_signals_returns_1():
    scorer = CompositeScorer()
    assert scorer.compute() == 1.0


def test_llm_only_high_returns_moderate():
    scorer = CompositeScorer(llm_score=1.0)
    result = scorer.compute()
    assert 2.5 <= result <= 3.0  # 0.4 weight * 4 + 1 = 2.6


def test_missing_tools_penalty():
    scorer = CompositeScorer(llm_score=0.8, required_tool_calls=True, num_tool_calls=0)
    result = scorer.compute()
    no_penalty = CompositeScorer(llm_score=0.8, required_tool_calls=False, num_tool_calls=0)
    assert result < no_penalty.compute()


def test_tool_failure_penalty():
    scorer = CompositeScorer(
        llm_score=0.8,
        tool_success_rate=0.33,
        num_tool_calls=3,
    )
    result = scorer.compute()
    perfect = CompositeScorer(
        llm_score=0.8,
        tool_success_rate=1.0,
        num_tool_calls=3,
    )
    assert result < perfect.compute()


def test_score_clamped_to_1_5_range():
    scorer = CompositeScorer(
        llm_score=0.0,
        required_tool_calls=True,
        num_tool_calls=0,
        tool_success_rate=0.0,
    )
    result = scorer.compute()
    assert result >= 1.0
    assert result <= 5.0


def test_pattern_success_boost_caps_at_01():
    scorer = CompositeScorer(query_pattern_success_count=1000)
    scorer2 = CompositeScorer(query_pattern_success_count=50)
    assert scorer.compute() == scorer2.compute()
