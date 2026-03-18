"""Tests for entity resolver confidence threshold (Fix 2 — 10x Agent Quality).

Low-confidence entity matches (< 0.70) should be filtered out to prevent
the resolver from injecting wrong custom fields into the agent prompt.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.chat.tenant_resolver import TenantEntityResolver

TENANT_ID = uuid.uuid4()


def _make_adapter(extracted_entities: list[str]) -> AsyncMock:
    """Build a mock LLM adapter that returns extracted entities."""
    adapter = AsyncMock()
    response = MagicMock()
    response.text_blocks = [json.dumps(extracted_entities)]
    adapter.create_message = AsyncMock(return_value=response)
    return adapter


def _make_db_with_matches(matches: list[dict]) -> AsyncMock:
    """Build a mock db that returns entity matches with given scores.

    Each match dict: {name, script_id, entity_type, sim, description}
    Matches are returned in order — one per execute() call.
    After entity matches, a final execute() returns empty learned rules.
    """
    db = AsyncMock()
    results = []
    for m in matches:
        row = MagicMock()
        entity = MagicMock()
        entity.script_id = m["script_id"]
        entity.entity_type = m["entity_type"]
        entity.description = m.get("description", "")
        row.TenantEntityMapping = entity
        row.sim = m["sim"]

        result = MagicMock()
        result.first.return_value = row
        results.append(result)

    # Append empty result for no-match entities and learned rules query
    empty_result = MagicMock()
    empty_result.first.return_value = None
    empty_scalars = MagicMock()
    empty_scalars.all.return_value = []
    empty_result.scalars.return_value = empty_scalars

    # The last execute call is for learned rules
    db.execute = AsyncMock(side_effect=[*results, empty_result])
    return db


class TestEntityResolverThreshold:
    """High-confidence matches should be included, low-confidence filtered."""

    @pytest.mark.asyncio
    async def test_high_confidence_included(self):
        """Match with sim >= 0.70 should appear in the XML output."""
        adapter = _make_adapter(["Panurgy"])
        db = _make_db_with_matches([
            {"script_id": "custbody_location", "entity_type": "custom_field",
             "sim": 0.85, "description": "Repair location"},
        ])

        result = await TenantEntityResolver.resolve_entities(
            "RMAs at Panurgy", TENANT_ID, db, adapter, "haiku"
        )

        assert "custbody_location" in result
        assert "<confidence_score>0.85</confidence_score>" in result

    @pytest.mark.asyncio
    async def test_low_confidence_filtered(self):
        """Match with sim < 0.70 should NOT appear in the XML output."""
        adapter = _make_adapter(["platform"])
        db = _make_db_with_matches([
            {"script_id": "custitem_fw_platform", "entity_type": "custom_field",
             "sim": 0.45, "description": "FW platform field"},
        ])

        result = await TenantEntityResolver.resolve_entities(
            "show me open POs", TENANT_ID, db, adapter, "haiku"
        )

        # Low confidence: filtered out, no entities resolved, returns empty
        assert "custitem_fw_platform" not in result

    @pytest.mark.asyncio
    async def test_borderline_070_included(self):
        """Match at exactly 0.70 should be included (>= threshold)."""
        adapter = _make_adapter(["warehouse"])
        db = _make_db_with_matches([
            {"script_id": "custbody_warehouse", "entity_type": "custom_field",
             "sim": 0.70, "description": "Warehouse field"},
        ])

        result = await TenantEntityResolver.resolve_entities(
            "items by warehouse", TENANT_ID, db, adapter, "haiku"
        )

        assert "custbody_warehouse" in result

    @pytest.mark.asyncio
    async def test_mixed_confidence_filters_correctly(self):
        """Only high-confidence matches pass; low ones are dropped."""
        adapter = _make_adapter(["Panurgy", "rush"])
        # Two entity lookups + learned rules query
        db = AsyncMock()

        high_row = MagicMock()
        high_entity = MagicMock()
        high_entity.script_id = "location_panurgy"
        high_entity.entity_type = "location"
        high_entity.description = "Panurgy location"
        high_row.TenantEntityMapping = high_entity
        high_row.sim = 0.92
        high_result = MagicMock()
        high_result.first.return_value = high_row

        low_row = MagicMock()
        low_entity = MagicMock()
        low_entity.script_id = "custbody_rush_flag"
        low_entity.entity_type = "custom_field"
        low_entity.description = "Rush flag"
        low_row.TenantEntityMapping = low_entity
        low_row.sim = 0.55
        low_result = MagicMock()
        low_result.first.return_value = low_row

        # Learned rules: empty
        rules_result = MagicMock()
        rules_scalars = MagicMock()
        rules_scalars.all.return_value = []
        rules_result.scalars.return_value = rules_scalars

        db.execute = AsyncMock(side_effect=[high_result, low_result, rules_result])

        result = await TenantEntityResolver.resolve_entities(
            "RMAs at Panurgy rush", TENANT_ID, db, adapter, "haiku"
        )

        assert "location_panurgy" in result
        assert "custbody_rush_flag" not in result

    @pytest.mark.asyncio
    async def test_instruction_uses_prefer_not_must(self):
        """The XML instruction should say 'prefer' not 'MUST use'."""
        adapter = _make_adapter(["Panurgy"])
        db = _make_db_with_matches([
            {"script_id": "location_panurgy", "entity_type": "location",
             "sim": 0.90, "description": "Panurgy"},
        ])

        result = await TenantEntityResolver.resolve_entities(
            "RMAs at Panurgy", TENANT_ID, db, adapter, "haiku"
        )

        assert "prefer" in result.lower()
        # Should NOT contain the old "MUST use" language
        assert "MUST use these exact" not in result
