"""Tests for the history compaction module."""

from unittest.mock import AsyncMock

import pytest

from app.services.chat.history_compactor import (
    COMPACTION_THRESHOLD,
    KEEP_RECENT,
    compact_history,
)
from app.services.chat.llm_adapter import LLMResponse, TokenUsage


def _make_history(n_messages: int) -> list[dict]:
    """Build a fake history with n alternating user/assistant messages."""
    history = []
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        history.append({"role": role, "content": f"Message {i}"})
    return history


class TestCompactHistory:
    @pytest.mark.asyncio
    async def test_skips_short_history(self):
        """History with <= COMPACTION_THRESHOLD messages should return unchanged."""
        history = _make_history(COMPACTION_THRESHOLD)
        adapter = AsyncMock()
        result = await compact_history(history, adapter, "test-model")
        assert result == history
        adapter.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_compacts_long_history(self):
        """History with > COMPACTION_THRESHOLD messages should be compacted."""
        history = _make_history(20)
        adapter = AsyncMock()
        adapter.create_message.return_value = LLMResponse(
            text_blocks=["Summary of the conversation so far."],
            tool_use_blocks=[],
            usage=TokenUsage(200, 100),
        )

        result = await compact_history(history, adapter, "test-model")

        # Should have: summary msg + ack msg + last KEEP_RECENT messages
        assert len(result) == 2 + KEEP_RECENT
        adapter.create_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_compacted_format(self):
        """Summary should be wrapped in <compacted_history> tags."""
        history = _make_history(20)
        adapter = AsyncMock()
        adapter.create_message.return_value = LLMResponse(
            text_blocks=["The user asked about Q1 sales."],
            tool_use_blocks=[],
            usage=TokenUsage(200, 100),
        )

        result = await compact_history(history, adapter, "test-model")

        assert "<compacted_history>" in result[0]["content"]
        assert "Q1 sales" in result[0]["content"]
        assert result[1]["role"] == "assistant"
        assert "context" in result[1]["content"].lower()

    @pytest.mark.asyncio
    async def test_preserves_recent_turns(self):
        """The last KEEP_RECENT messages should be preserved exactly."""
        history = _make_history(20)
        adapter = AsyncMock()
        adapter.create_message.return_value = LLMResponse(
            text_blocks=["Summary"],
            tool_use_blocks=[],
            usage=TokenUsage(200, 100),
        )

        result = await compact_history(history, adapter, "test-model")

        # Last KEEP_RECENT messages of result should match last KEEP_RECENT of original
        assert result[-KEEP_RECENT:] == history[-KEEP_RECENT:]

    @pytest.mark.asyncio
    async def test_handles_llm_failure(self):
        """If the LLM call fails, return original history unchanged."""
        history = _make_history(20)
        adapter = AsyncMock()
        adapter.create_message.side_effect = Exception("API error")

        result = await compact_history(history, adapter, "test-model")

        assert result == history

    @pytest.mark.asyncio
    async def test_handles_empty_summary(self):
        """If the LLM returns empty text, return original history unchanged."""
        history = _make_history(20)
        adapter = AsyncMock()
        adapter.create_message.return_value = LLMResponse(
            text_blocks=[""],
            tool_use_blocks=[],
            usage=TokenUsage(200, 100),
        )

        result = await compact_history(history, adapter, "test-model")

        assert result == history
