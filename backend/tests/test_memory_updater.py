"""Tests for the regex-gated memory updater."""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.chat.llm_adapter import LLMResponse, TokenUsage
from app.services.chat.memory_updater import has_correction_signal, maybe_extract_correction


# ── Regex gate tests ──


class TestHasCorrectionSignal:
    def test_detects_correction_phrases(self):
        assert has_correction_signal("No, it should be EUR not USD")
        assert has_correction_signal("Actually, that field is custbody_platform")
        assert has_correction_signal("That's wrong, the order number starts with SO")
        assert has_correction_signal("Remember that platform means custitem_fw_platform")
        assert has_correction_signal("Always use BUILTIN.DF for status fields")
        assert has_correction_signal("Never show raw IDs, always show names")
        assert has_correction_signal("From now on, group by currency")
        assert has_correction_signal("Don't use foreigntotal for USD amounts")
        assert has_correction_signal("When I say platform, use custbody_platform")
        assert has_correction_signal("The field for sales channel is custbody_sales_channel")
        assert has_correction_signal("Please always include the currency column")
        assert has_correction_signal("use customrecord_foo for that")

    def test_skips_normal_messages(self):
        assert not has_correction_signal("Show me today's sales orders")
        assert not has_correction_signal("What is our revenue this month?")
        assert not has_correction_signal("How many open invoices do we have?")
        assert not has_correction_signal("Tell me about customer Acme Corp")
        assert not has_correction_signal("Compare Q1 and Q2 sales")
        assert not has_correction_signal("Thanks, that looks good")
        assert not has_correction_signal("Can you show me more details?")


# ── Extraction tests ──


class TestMaybeExtractCorrection:
    @pytest.mark.asyncio
    async def test_skips_when_no_signal(self):
        """Normal messages should not trigger any LLM call."""
        adapter = AsyncMock()
        db = AsyncMock()
        result = await maybe_extract_correction(
            db=db, tenant_id=uuid.uuid4(), user_id=uuid.uuid4(),
            user_message="Show me today's orders",
            assistant_message="Here are the orders...",
            adapter=adapter, model="test",
        )
        assert result is False
        adapter.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_extracts_entity_correction(self):
        """'Use customrecord_foo' should save an entity mapping."""
        adapter = AsyncMock()
        adapter.create_message.return_value = LLMResponse(
            text_blocks=[json.dumps({
                "entity_correction": {
                    "natural_name": "inventory processor",
                    "script_id": "customrecord_r_inv_processor",
                    "entity_type": "customrecord",
                },
                "rule": None,
            })],
            tool_use_blocks=[],
            usage=TokenUsage(50, 30),
        )
        db = AsyncMock()

        with patch("app.services.chat.memory_updater._save_entity_mapping", new_callable=AsyncMock) as mock_save_entity, \
             patch("app.services.chat.memory_updater._save_learned_rule", new_callable=AsyncMock) as mock_save_rule, \
             patch("app.services.audit_service.log_event", new_callable=AsyncMock):
            mock_save_entity.return_value = True
            result = await maybe_extract_correction(
                db=db, tenant_id=uuid.uuid4(), user_id=uuid.uuid4(),
                user_message="Actually, use customrecord_r_inv_processor for inventory processor",
                assistant_message="I queried the inventory table...",
                adapter=adapter, model="test",
            )
            assert result is True
            mock_save_entity.assert_called_once()
            mock_save_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_extracts_general_rule(self):
        """'Always show currency' should save a learned rule."""
        adapter = AsyncMock()
        adapter.create_message.return_value = LLMResponse(
            text_blocks=[json.dumps({
                "entity_correction": None,
                "rule": {
                    "description": "Always include the currency column in query results",
                    "category": "output_preference",
                },
            })],
            tool_use_blocks=[],
            usage=TokenUsage(50, 30),
        )
        db = AsyncMock()

        with patch("app.services.chat.memory_updater._save_entity_mapping", new_callable=AsyncMock) as mock_save_entity, \
             patch("app.services.chat.memory_updater._save_learned_rule", new_callable=AsyncMock) as mock_save_rule, \
             patch("app.services.audit_service.log_event", new_callable=AsyncMock):
            mock_save_rule.return_value = True
            result = await maybe_extract_correction(
                db=db, tenant_id=uuid.uuid4(), user_id=uuid.uuid4(),
                user_message="Always show the currency column in results",
                assistant_message="Here are your orders...",
                adapter=adapter, model="test",
            )
            assert result is True
            mock_save_rule.assert_called_once()
            mock_save_entity.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_json_returns_false(self):
        """If LLM returns garbage, nothing should be saved."""
        adapter = AsyncMock()
        adapter.create_message.return_value = LLMResponse(
            text_blocks=["No corrections found here, just chatting."],
            tool_use_blocks=[],
            usage=TokenUsage(50, 30),
        )
        db = AsyncMock()
        result = await maybe_extract_correction(
            db=db, tenant_id=uuid.uuid4(), user_id=uuid.uuid4(),
            user_message="No, that's not what I meant",
            assistant_message="I showed you...",
            adapter=adapter, model="test",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_null_corrections_returns_false(self):
        """If LLM returns null for both, nothing should be saved."""
        adapter = AsyncMock()
        adapter.create_message.return_value = LLMResponse(
            text_blocks=[json.dumps({"entity_correction": None, "rule": None})],
            tool_use_blocks=[],
            usage=TokenUsage(50, 30),
        )
        db = AsyncMock()
        result = await maybe_extract_correction(
            db=db, tenant_id=uuid.uuid4(), user_id=uuid.uuid4(),
            user_message="No, that doesn't look right but whatever",
            assistant_message="Here is the data...",
            adapter=adapter, model="test",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_llm_exception_returns_false(self):
        """If the LLM call fails, should gracefully return False."""
        adapter = AsyncMock()
        adapter.create_message.side_effect = Exception("API error")
        db = AsyncMock()
        result = await maybe_extract_correction(
            db=db, tenant_id=uuid.uuid4(), user_id=uuid.uuid4(),
            user_message="Remember that X is Y",
            assistant_message="...",
            adapter=adapter, model="test",
        )
        assert result is False
