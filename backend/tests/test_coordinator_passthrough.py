"""Tests for coordinator pass-through synthesis logic."""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.services.chat.agents import AgentResult
from app.services.chat.coordinator import MultiAgentCoordinator
from app.services.chat.llm_adapter import LLMResponse, TokenUsage


def _make_coordinator(**kwargs):
    """Create a minimal coordinator for testing instance methods."""
    coord = MultiAgentCoordinator(
        db=AsyncMock(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        correlation_id="test",
        main_adapter=kwargs.get("main_adapter", AsyncMock()),
        main_model="test-main",
        specialist_adapter=AsyncMock(),
        specialist_model="test-spec",
    )
    return coord


def _make_result(success=True, data=None, agent_name="suiteql", error=None):
    return AgentResult(
        success=success,
        data=data,
        agent_name=agent_name,
        error=error,
        tokens_used=TokenUsage(0, 0),
    )


# ── _should_pass_through tests ──


class TestShouldPassThrough:
    def test_single_agent_table_passes_through(self):
        coord = _make_coordinator()
        table = "Found 3 orders:\n\n| Order | Amount |\n|---|---|\n| SO001 | 1,000.00 |"
        result = coord._should_pass_through([_make_result(data=table)])
        assert result is not None
        assert "SO001" in result

    def test_single_agent_no_data_passes_through(self):
        coord = _make_coordinator()
        msg = "No matching records found for sales orders on 2026-02-25."
        result = coord._should_pass_through([_make_result(data=msg)])
        assert result is not None
        assert "No matching records" in result

    def test_multi_agent_does_not_pass_through(self):
        coord = _make_coordinator()
        results = [
            _make_result(data="| A |\n|---|\n| 1 |", agent_name="suiteql"),
            _make_result(data="Analysis text", agent_name="analysis"),
        ]
        assert coord._should_pass_through(results) is None

    def test_empty_result_does_not_pass_through(self):
        coord = _make_coordinator()
        assert coord._should_pass_through([_make_result(data="")]) is None

    def test_short_result_does_not_pass_through(self):
        coord = _make_coordinator()
        assert coord._should_pass_through([_make_result(data="OK")]) is None

    def test_pass_through_strips_reasoning(self):
        coord = _make_coordinator()
        data = "<reasoning>internal</reasoning>\nFound 1:\n\n| Order |\n|---|\n| SO001 |"
        result = coord._should_pass_through([_make_result(data=data)])
        assert result is not None
        assert "<reasoning>" not in result
        assert "SO001" in result

    def test_failed_agent_does_not_pass_through(self):
        coord = _make_coordinator()
        result = coord._should_pass_through(
            [_make_result(success=False, data="| A |\n|---|\n| 1 |", error="timeout")]
        )
        assert result is None

    def test_no_results_message_variants(self):
        coord = _make_coordinator()
        for msg in [
            "0 rows returned for the given date range.",
            "No results found for customer 'Acme Corp'.",
            "No data available for Q1 2026.",
            "No matching transactions found.",
            "No records found for that order number.",
        ]:
            result = coord._should_pass_through([_make_result(data=msg)])
            assert result is not None, f"Should pass-through: {msg}"

    def test_prose_without_table_does_not_pass_through(self):
        coord = _make_coordinator()
        result = coord._should_pass_through(
            [_make_result(data="The customer has 5 open orders and a credit limit of $10,000.")]
        )
        assert result is None


# ── _synthesise integration tests ──


class TestSynthesise:
    @pytest.mark.asyncio
    async def test_bypasses_llm_for_table(self):
        main_adapter = AsyncMock()
        coord = _make_coordinator(main_adapter=main_adapter)
        table = "Found 1:\n\n| ID | Name |\n|---|---|\n| 1 | Test |"
        results = [_make_result(data=table)]

        text, usage = await coord._synthesise("query", [], results)

        assert "Test" in text
        assert usage.input_tokens == 0
        main_adapter.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_llm_for_prose(self):
        main_adapter = AsyncMock()
        main_adapter.create_message.return_value = LLMResponse(
            text_blocks=["Synthesized answer."],
            tool_use_blocks=[],
            usage=TokenUsage(100, 50),
        )
        coord = _make_coordinator(main_adapter=main_adapter)
        results = [_make_result(data="Some prose without a table that is long enough.")]

        text, usage = await coord._synthesise("query", [], results)

        assert text == "Synthesized answer."
        assert usage.input_tokens == 100
        main_adapter.create_message.assert_called_once()


# ── Utility tests ──


class TestContainsMarkdownTable:
    def test_standard_table(self):
        assert MultiAgentCoordinator._contains_markdown_table("| A | B |\n|---|---|\n| 1 | 2 |")

    def test_aligned_table(self):
        assert MultiAgentCoordinator._contains_markdown_table("| A |\n| :---: |\n| 1 |")

    def test_no_table(self):
        assert not MultiAgentCoordinator._contains_markdown_table("Text with | pipes")

    def test_empty(self):
        assert not MultiAgentCoordinator._contains_markdown_table("")

    def test_none(self):
        assert not MultiAgentCoordinator._contains_markdown_table(None)


class TestSanitizeAgentData:
    def test_strips_reasoning(self):
        result = MultiAgentCoordinator._sanitize_agent_data("<reasoning>x</reasoning>\nClean")
        assert "<reasoning>" not in result
        assert "Clean" in result

    def test_strips_function_calls(self):
        result = MultiAgentCoordinator._sanitize_agent_data(
            "Result\n<function_calls><invoke></invoke></function_calls>\nMore"
        )
        assert "<function_calls>" not in result

    def test_truncates_long_output(self):
        assert len(MultiAgentCoordinator._sanitize_agent_data("x" * 10000)) <= 8000


class TestGetSynthesisModel:
    def test_uses_synthesis_model_setting(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.chat.coordinator.settings.MULTI_AGENT_SYNTHESIS_MODEL",
            "claude-sonnet-4-5-20250929",
        )
        coord = _make_coordinator()
        assert coord._get_synthesis_model() == "claude-sonnet-4-5-20250929"

    def test_falls_back_to_main_model(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.chat.coordinator.settings.MULTI_AGENT_SYNTHESIS_MODEL", ""
        )
        coord = _make_coordinator()
        assert coord._get_synthesis_model() == "test-main"
