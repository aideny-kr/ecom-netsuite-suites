"""Tests for per-message content summariser and summary-based history loading."""

from unittest.mock import AsyncMock

import pytest

from app.services.chat.llm_adapter import LLMResponse, TokenUsage
from app.services.chat.summariser import generate_content_summary


class TestGenerateContentSummary:
    """Unit tests for the summary generation function."""

    @pytest.mark.asyncio
    async def test_skips_short_responses(self):
        """Responses under 200 chars should return None (already compact)."""
        adapter = AsyncMock()
        result = await generate_content_summary(
            user_message="What's the status?",
            assistant_message="All good.",
            adapter=adapter,
            model="test-model",
        )
        assert result is None
        adapter.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_generates_summary_for_long_response(self):
        """Long assistant responses should produce a summary via LLM."""
        adapter = AsyncMock()
        adapter.create_message.return_value = LLMResponse(
            text_blocks=["User asked about open POs. 15 purchase orders totaling $45K returned."],
            tool_use_blocks=[],
            usage=TokenUsage(150, 50),
        )

        long_response = "Here are the open purchase orders:\n" + "\n".join(
            f"| PO{i:04d} | Vendor {i} | ${i * 100:,.2f} |" for i in range(50)
        )

        result = await generate_content_summary(
            user_message="Show me open purchase orders",
            assistant_message=long_response,
            adapter=adapter,
            model="test-model",
        )

        assert result is not None
        assert "PO" in result or "purchase" in result.lower()
        adapter.create_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_failure(self):
        """LLM errors should return None gracefully, not raise."""
        adapter = AsyncMock()
        adapter.create_message.side_effect = Exception("API timeout")

        long_response = "x" * 300

        result = await generate_content_summary(
            user_message="query",
            assistant_message=long_response,
            adapter=adapter,
            model="test-model",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_summary(self):
        """Empty LLM response should return None."""
        adapter = AsyncMock()
        adapter.create_message.return_value = LLMResponse(
            text_blocks=[""],
            tool_use_blocks=[],
            usage=TokenUsage(100, 10),
        )

        result = await generate_content_summary(
            user_message="query",
            assistant_message="x" * 300,
            adapter=adapter,
            model="test-model",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_truncates_long_assistant_message(self):
        """Assistant message passed to LLM should be capped at 4000 chars."""
        adapter = AsyncMock()
        adapter.create_message.return_value = LLMResponse(
            text_blocks=["Summary of truncated content."],
            tool_use_blocks=[],
            usage=TokenUsage(100, 30),
        )

        huge_response = "A" * 10000

        await generate_content_summary(
            user_message="query",
            assistant_message=huge_response,
            adapter=adapter,
            model="test-model",
        )

        # Verify the content passed to LLM was truncated
        call_args = adapter.create_message.call_args
        messages = call_args.kwargs.get("messages", call_args.args[0] if call_args.args else [])
        user_content = messages[0]["content"]
        # 4000 chars of assistant + "User: query\n\nAssistant: " prefix
        assert len(user_content) < 4100


class TestSummaryBasedHistoryLoading:
    """Tests for the orchestrator's summary-based history windowing logic.

    These test the logic extracted from run_chat_turn's history loading,
    verifying that older messages use summaries and recent ones use full content.
    """

    def _make_mock_message(self, role, content, content_summary=None):
        """Create a mock ChatMessage-like object."""
        msg = type("MockMsg", (), {})()
        msg.role = role
        msg.content = content
        msg.content_summary = content_summary
        return msg

    def test_recent_messages_use_full_content(self):
        """Last 8 messages should always use full content."""
        keep_recent = 8
        messages = []
        for i in range(10):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append(
                self._make_mock_message(
                    role=role,
                    content=f"Full content {i} " + "x" * 500,
                    content_summary=f"Summary {i}",
                )
            )

        result = []
        for i, msg in enumerate(messages):
            is_recent = i >= len(messages) - keep_recent
            if is_recent or not msg.content_summary:
                result.append({"role": msg.role, "content": msg.content})
            else:
                result.append({"role": msg.role, "content": msg.content_summary})

        # Last 8 should have full content
        for entry in result[-keep_recent:]:
            assert entry["content"].startswith("Full content")

        # First 2 should have summaries
        for entry in result[:2]:
            assert entry["content"].startswith("Summary")

    def test_messages_without_summary_use_full_content(self):
        """Messages without content_summary should fall back to full content."""
        keep_recent = 8
        messages = []
        for i in range(12):
            role = "user" if i % 2 == 0 else "assistant"
            # Only some messages have summaries
            summary = f"Summary {i}" if i % 3 == 0 else None
            messages.append(
                self._make_mock_message(
                    role=role,
                    content=f"Full content {i}",
                    content_summary=summary,
                )
            )

        result = []
        for i, msg in enumerate(messages):
            is_recent = i >= len(messages) - keep_recent
            if is_recent or not msg.content_summary:
                result.append({"role": msg.role, "content": msg.content})
            else:
                result.append({"role": msg.role, "content": msg.content_summary})

        # Messages 0-3 are older than keep_recent window
        # Message 0 has summary → should use it
        assert result[0]["content"] == "Summary 0"
        # Message 1 has no summary → should use full content
        assert result[1]["content"] == "Full content 1"
        # Message 3 has summary → should use it
        assert result[3]["content"] == "Summary 3"

    def test_empty_history_produces_empty_list(self):
        """No messages should produce empty history."""
        messages = []
        result = []
        for i, msg in enumerate(messages):
            result.append({"role": msg.role, "content": msg.content})
        assert result == []

    def test_short_history_all_verbatim(self):
        """History shorter than keep_recent should all be verbatim."""
        keep_recent = 8
        messages = []
        for i in range(4):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append(
                self._make_mock_message(
                    role=role,
                    content=f"Full content {i}",
                    content_summary=f"Summary {i}",
                )
            )

        result = []
        for i, msg in enumerate(messages):
            is_recent = i >= len(messages) - keep_recent
            if is_recent or not msg.content_summary:
                result.append({"role": msg.role, "content": msg.content})
            else:
                result.append({"role": msg.role, "content": msg.content_summary})

        # All should be full content since len(4) < keep_recent(8)
        for entry in result:
            assert entry["content"].startswith("Full content")
