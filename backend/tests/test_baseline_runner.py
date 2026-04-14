"""Unit tests for the Claude + Oracle NetSuite MCP baseline runner.

Tests the agentic loop, cost calculation, max_steps safety, and the
minimal-system-prompt invariant. Mocks the Anthropic client and
execute_tool_call so no real API calls happen.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.benchmarks.baseline_runner import (
    BASELINE_SYSTEM_PROMPT_TEMPLATE,
    BaselineResult,
    _calculate_cost,
    run_baseline,
)

# ---------------------------------------------------------------------------
# Helpers — build fake Anthropic responses without hitting the API
# ---------------------------------------------------------------------------


def _make_text_block(text: str):
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_tool_use_block(tool_id: str, name: str, tool_input: dict):
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = tool_input
    return block


def _make_response(content_blocks, input_tokens=100, output_tokens=50):
    response = MagicMock()
    response.content = content_blocks
    response.usage = MagicMock(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return response


@pytest.fixture
def fake_db():
    """Minimal fake AsyncSession — the runner only passes it through."""
    return MagicMock()


@pytest.fixture
def tenant_id():
    return uuid.UUID("ce3dfaad-626f-4992-84e9-500c8291ca0a")


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------


class TestCostCalculation:
    def test_sonnet_cost(self):
        # 1M input tokens @ $3/MTok + 1M output tokens @ $15/MTok = $18
        cost = _calculate_cost(model="claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost == pytest.approx(18.0, rel=1e-6)

    def test_opus_cost(self):
        # 1M input tokens @ $15/MTok + 1M output tokens @ $75/MTok = $90
        cost = _calculate_cost(model="claude-opus-4-6", input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost == pytest.approx(90.0, rel=1e-6)

    def test_small_sonnet_cost(self):
        # 10K input + 5K output @ sonnet rates = 10K * 3/1M + 5K * 15/1M = 0.03 + 0.075 = 0.105
        cost = _calculate_cost(model="claude-sonnet-4-6", input_tokens=10_000, output_tokens=5_000)
        assert cost == pytest.approx(0.105, rel=1e-6)

    def test_unknown_model_falls_back_to_sonnet(self):
        # Unknown models should not crash — fall back to sonnet pricing
        cost = _calculate_cost(model="claude-future-99", input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost == pytest.approx(18.0, rel=1e-6)


# ---------------------------------------------------------------------------
# System prompt minimalism — the whole point of the baseline
# ---------------------------------------------------------------------------


class TestSystemPromptMinimal:
    def test_prompt_is_short(self):
        # The whole rendered prompt should be tiny — no schema dump, no rules
        assert len(BASELINE_SYSTEM_PROMPT_TEMPLATE) < 2000

    def test_prompt_does_not_leak_internal_concepts(self):
        forbidden = [
            "vernacular",
            "learned_rules",
            "proven_patterns",
            "tenant_schema",
            "tenant_vernacular",
        ]
        lower = BASELINE_SYSTEM_PROMPT_TEMPLATE.lower()
        for word in forbidden:
            assert word.lower() not in lower, f"Baseline prompt must not contain '{word}'"

    def test_prompt_mentions_netsuite(self):
        # Sanity: it should at least tell Claude what domain it's in
        assert "netsuite" in BASELINE_SYSTEM_PROMPT_TEMPLATE.lower()


# ---------------------------------------------------------------------------
# Happy path: text-only response
# ---------------------------------------------------------------------------


class TestRunBaselineHappyPath:
    @pytest.mark.asyncio
    async def test_text_only_response(self, fake_db, tenant_id):
        """Anthropic returns a single text block — we record it and finish."""
        text_response = _make_response(
            [_make_text_block("The answer is 42 sales orders.")],
            input_tokens=200,
            output_tokens=15,
        )

        mock_create = AsyncMock(return_value=text_response)

        with (
            patch(
                "app.services.benchmarks.baseline_runner._build_baseline_tools",
                new=AsyncMock(return_value=[]),
            ),
            patch("app.services.benchmarks.baseline_runner._get_anthropic_client") as mock_get_client,
        ):
            mock_client = MagicMock()
            mock_client.messages.create = mock_create
            mock_get_client.return_value = mock_client

            result = await run_baseline(
                tenant_id=tenant_id,
                question="How many sales orders today?",
                model="claude-sonnet-4-6",
                max_steps=12,
                db=fake_db,
            )

        assert isinstance(result, BaselineResult)
        assert result.success is True
        assert result.error is None
        assert "42 sales orders" in result.answer_text
        assert result.tool_calls == []
        assert result.input_tokens == 200
        assert result.output_tokens == 15
        assert result.cost_usd > 0
        assert result.latency_ms >= 0
        # Anthropic was called exactly once
        assert mock_create.await_count == 1


# ---------------------------------------------------------------------------
# Tool use path
# ---------------------------------------------------------------------------


class TestRunBaselineToolUse:
    @pytest.mark.asyncio
    async def test_tool_use_then_final_answer(self, fake_db, tenant_id):
        """First call returns a tool_use; second call returns the final text."""
        tool_use_resp = _make_response(
            [_make_tool_use_block("tu_1", "ext__abc__ns_runCustomSuiteQL", {"sql": "SELECT 1"})],
            input_tokens=300,
            output_tokens=40,
        )
        final_resp = _make_response(
            [_make_text_block("Found 5 records.")],
            input_tokens=350,
            output_tokens=10,
        )

        mock_create = AsyncMock(side_effect=[tool_use_resp, final_resp])
        mock_execute = AsyncMock(return_value='{"data": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}]}')

        with (
            patch(
                "app.services.benchmarks.baseline_runner._build_baseline_tools",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "ext__abc__ns_runCustomSuiteQL",
                            "description": "x",
                            "input_schema": {"type": "object", "properties": {}},
                        }
                    ]
                ),
            ),
            patch("app.services.benchmarks.baseline_runner._get_anthropic_client") as mock_get_client,
            patch(
                "app.services.benchmarks.baseline_runner.execute_tool_call",
                new=mock_execute,
            ),
        ):
            mock_client = MagicMock()
            mock_client.messages.create = mock_create
            mock_get_client.return_value = mock_client

            result = await run_baseline(
                tenant_id=tenant_id,
                question="How many records?",
                model="claude-sonnet-4-6",
                max_steps=12,
                db=fake_db,
            )

        assert result.success is True
        assert result.error is None
        assert "Found 5 records" in result.answer_text
        # Tool was invoked once and recorded
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "ext__abc__ns_runCustomSuiteQL"
        assert result.tool_calls[0]["input"] == {"sql": "SELECT 1"}
        assert "result_preview" in result.tool_calls[0]
        # Both Anthropic calls happened
        assert mock_create.await_count == 2
        assert mock_execute.await_count == 1
        # Token counts are accumulated across both turns
        assert result.input_tokens == 300 + 350
        assert result.output_tokens == 40 + 10

    @pytest.mark.asyncio
    async def test_tool_call_records_preview_truncated(self, fake_db, tenant_id):
        """Long tool results should be truncated in result_preview."""
        big_result = "x" * 5000
        tool_use_resp = _make_response(
            [_make_tool_use_block("tu_1", "ext__abc__ns_runCustomSuiteQL", {"sql": "SELECT 1"})],
            input_tokens=300,
            output_tokens=40,
        )
        final_resp = _make_response([_make_text_block("done")], input_tokens=10, output_tokens=2)

        mock_create = AsyncMock(side_effect=[tool_use_resp, final_resp])
        mock_execute = AsyncMock(return_value=big_result)

        with (
            patch(
                "app.services.benchmarks.baseline_runner._build_baseline_tools",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "ext__abc__ns_runCustomSuiteQL",
                            "description": "x",
                            "input_schema": {"type": "object", "properties": {}},
                        }
                    ]
                ),
            ),
            patch("app.services.benchmarks.baseline_runner._get_anthropic_client") as mock_get_client,
            patch(
                "app.services.benchmarks.baseline_runner.execute_tool_call",
                new=mock_execute,
            ),
        ):
            mock_client = MagicMock()
            mock_client.messages.create = mock_create
            mock_get_client.return_value = mock_client

            result = await run_baseline(
                tenant_id=tenant_id,
                question="Big query",
                db=fake_db,
            )

        assert result.success is True
        # Preview should be capped well below the original 5000 chars
        assert len(result.tool_calls[0]["result_preview"]) < 2000


# ---------------------------------------------------------------------------
# Max steps safety
# ---------------------------------------------------------------------------


class TestRunBaselineMaxSteps:
    @pytest.mark.asyncio
    async def test_max_steps_exhausted(self, fake_db, tenant_id):
        """If Anthropic keeps returning tool_use forever, the loop must stop."""
        forever_tool_use = _make_response(
            [_make_tool_use_block("tu_X", "ext__abc__ns_runCustomSuiteQL", {"sql": "SELECT 1"})],
            input_tokens=100,
            output_tokens=30,
        )
        # Return tool_use every time
        mock_create = AsyncMock(return_value=forever_tool_use)
        mock_execute = AsyncMock(return_value='{"ok": true}')

        with (
            patch(
                "app.services.benchmarks.baseline_runner._build_baseline_tools",
                new=AsyncMock(
                    return_value=[
                        {
                            "name": "ext__abc__ns_runCustomSuiteQL",
                            "description": "x",
                            "input_schema": {"type": "object", "properties": {}},
                        }
                    ]
                ),
            ),
            patch("app.services.benchmarks.baseline_runner._get_anthropic_client") as mock_get_client,
            patch(
                "app.services.benchmarks.baseline_runner.execute_tool_call",
                new=mock_execute,
            ),
        ):
            mock_client = MagicMock()
            mock_client.messages.create = mock_create
            mock_get_client.return_value = mock_client

            result = await run_baseline(
                tenant_id=tenant_id,
                question="Loop forever",
                model="claude-sonnet-4-6",
                max_steps=3,
                db=fake_db,
            )

        assert result.success is False
        assert result.error is not None
        assert "max_steps" in result.error
        # We should have called Anthropic exactly max_steps times
        assert mock_create.await_count == 3
        # And executed the tool at each step
        assert mock_execute.await_count == 3


# ---------------------------------------------------------------------------
# API error path
# ---------------------------------------------------------------------------


class TestRunBaselineApiError:
    @pytest.mark.asyncio
    async def test_anthropic_error_returns_failure(self, fake_db, tenant_id):
        """If the Anthropic API raises, we return success=False with the error."""
        mock_create = AsyncMock(side_effect=RuntimeError("rate_limit_error"))

        with (
            patch(
                "app.services.benchmarks.baseline_runner._build_baseline_tools",
                new=AsyncMock(return_value=[]),
            ),
            patch("app.services.benchmarks.baseline_runner._get_anthropic_client") as mock_get_client,
        ):
            mock_client = MagicMock()
            mock_client.messages.create = mock_create
            mock_get_client.return_value = mock_client

            result = await run_baseline(
                tenant_id=tenant_id,
                question="anything",
                db=fake_db,
            )

        assert result.success is False
        assert result.error is not None
        assert "rate_limit_error" in result.error
        # No retries
        assert mock_create.await_count == 1
