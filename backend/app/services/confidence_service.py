"""Composite confidence scoring for AI chat responses."""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Weights for each signal component
_W_LLM = 0.40
_W_PATTERN = 0.15
_W_PATTERN_BOOST = 0.10
_W_DOMAIN = 0.10
_W_ENTITY = 0.15
_W_TOOL = 0.10

# Penalties
_PENALTY_MISSING_TOOLS = -0.2
_PENALTY_TOOL_FAILURE = -0.3

# Pattern success boost cap
_PATTERN_BOOST_DENOM = 50


@dataclass
class CompositeScorer:
    """Computes a composite confidence score (1.0-5.0) from multiple signals."""

    # Input signals (all 0.0-1.0 except success_count)
    llm_score: float = 0.0
    query_pattern_similarity: float = 0.0
    query_pattern_success_count: int = 0
    domain_knowledge_similarity: float = 0.0
    entity_resolution_confidence: float = 0.0
    tool_success_rate: float = 0.0
    num_tool_calls: int = 0
    required_tool_calls: bool = False

    def compute(self) -> float:
        """Return composite confidence score 1.0-5.0."""
        weighted_sum = (
            _W_LLM * self.llm_score
            + _W_PATTERN * self.query_pattern_similarity
            + _W_PATTERN_BOOST * min(self.query_pattern_success_count / _PATTERN_BOOST_DENOM, 1.0)
            + _W_DOMAIN * self.domain_knowledge_similarity
            + _W_ENTITY * self.entity_resolution_confidence
            + _W_TOOL * self.tool_success_rate
        )

        penalties = 0.0
        if self.required_tool_calls and self.num_tool_calls == 0:
            penalties += _PENALTY_MISSING_TOOLS
        if self.num_tool_calls > 0 and self.tool_success_rate < 1.0:
            penalties += _PENALTY_TOOL_FAILURE

        raw = weighted_sum + penalties
        clamped = max(0.0, min(1.0, raw))
        score = round(clamped * 4 + 1, 1)

        logger.info(
            "confidence.computed llm=%.2f pattern_sim=%.2f pattern_count=%d "
            "domain=%.2f entity=%.2f tool_rate=%.2f tools=%d required=%s "
            "penalties=%.2f final=%.1f",
            self.llm_score,
            self.query_pattern_similarity,
            self.query_pattern_success_count,
            self.domain_knowledge_similarity,
            self.entity_resolution_confidence,
            self.tool_success_rate,
            self.num_tool_calls,
            self.required_tool_calls,
            penalties,
            score,
        )

        return score
