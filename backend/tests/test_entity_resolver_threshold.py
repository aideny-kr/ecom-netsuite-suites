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
    The resolver does TWO db.execute calls per entity (name_query + script_query).
    We return the match on the name_query and None on the script_query.
    After all entity lookups, a final execute() returns empty learned rules.
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

        # name_query result — returns the match
        name_result = MagicMock()
        name_result.first.return_value = row
        results.append(name_result)

        # script_query result — no match
        script_result = MagicMock()
        script_result.first.return_value = None
        results.append(script_result)

    # Learned rules query — empty
    rules_result = MagicMock()
    rules_result.first.return_value = None
    rules_scalars = MagicMock()
    rules_scalars.all.return_value = []
    rules_result.scalars.return_value = rules_scalars

    db.execute = AsyncMock(side_effect=[*results, rules_result])
    return db


class TestEntityResolverThreshold:
    """High-confidence matches should be included, low-confidence filtered."""

    @pytest.mark.asyncio
    async def test_high_confidence_included(self):
        """Match with sim >= 0.70 should appear in the XML output."""
        adapter = _make_adapter(["Panurgy"])
        db = _make_db_with_matches(
            [
                {
                    "script_id": "custbody_location",
                    "entity_type": "custom_field",
                    "sim": 0.85,
                    "description": "Repair location",
                },
            ]
        )

        result = await TenantEntityResolver.resolve_entities("RMAs at Panurgy", TENANT_ID, db, adapter, "haiku")

        assert "custbody_location" in result
        assert "<confidence_score>0.85</confidence_score>" in result

    @pytest.mark.asyncio
    async def test_low_confidence_filtered(self):
        """Match with sim < 0.70 should NOT appear in the XML output."""
        adapter = _make_adapter(["platform"])
        db = _make_db_with_matches(
            [
                {
                    "script_id": "custitem_fw_platform",
                    "entity_type": "custom_field",
                    "sim": 0.45,
                    "description": "FW platform field",
                },
            ]
        )

        result = await TenantEntityResolver.resolve_entities("show me open POs", TENANT_ID, db, adapter, "haiku")

        # Low confidence: filtered out, no entities resolved, returns empty
        assert "custitem_fw_platform" not in result

    @pytest.mark.asyncio
    async def test_borderline_070_included(self):
        """Match at exactly 0.70 should be included (>= threshold)."""
        adapter = _make_adapter(["warehouse"])
        db = _make_db_with_matches(
            [
                {
                    "script_id": "custbody_warehouse",
                    "entity_type": "custom_field",
                    "sim": 0.70,
                    "description": "Warehouse field",
                },
            ]
        )

        result = await TenantEntityResolver.resolve_entities("items by warehouse", TENANT_ID, db, adapter, "haiku")

        assert "custbody_warehouse" in result

    @pytest.mark.asyncio
    async def test_mixed_confidence_filters_correctly(self):
        """Only high-confidence matches pass; low ones are dropped."""
        adapter = _make_adapter(["Panurgy", "rush"])
        # Each entity does TWO db.execute calls (name_query + script_query),
        # then one final call for learned rules = 2*2 + 1 = 5 calls
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

        # No script_id match for "Panurgy"
        no_match_result = MagicMock()
        no_match_result.first.return_value = None

        low_row = MagicMock()
        low_entity = MagicMock()
        low_entity.script_id = "custbody_rush_flag"
        low_entity.entity_type = "custom_field"
        low_entity.description = "Rush flag"
        low_row.TenantEntityMapping = low_entity
        low_row.sim = 0.55
        low_result = MagicMock()
        low_result.first.return_value = low_row

        # No script_id match for "rush"
        no_match_result2 = MagicMock()
        no_match_result2.first.return_value = None

        # Learned rules: empty
        rules_result = MagicMock()
        rules_scalars = MagicMock()
        rules_scalars.all.return_value = []
        rules_result.scalars.return_value = rules_scalars

        # Order: name("Panurgy"), script("Panurgy"), name("rush"), script("rush"), learned_rules
        db.execute = AsyncMock(side_effect=[high_result, no_match_result, low_result, no_match_result2, rules_result])

        result = await TenantEntityResolver.resolve_entities("RMAs at Panurgy rush", TENANT_ID, db, adapter, "haiku")

        assert "location_panurgy" in result
        assert "custbody_rush_flag" not in result

    @pytest.mark.asyncio
    async def test_instruction_uses_prefer_not_must(self):
        """The XML instruction should say 'prefer' not 'MUST use'."""
        adapter = _make_adapter(["Panurgy"])
        db = _make_db_with_matches(
            [
                {"script_id": "location_panurgy", "entity_type": "location", "sim": 0.90, "description": "Panurgy"},
            ]
        )

        result = await TenantEntityResolver.resolve_entities("RMAs at Panurgy", TENANT_ID, db, adapter, "haiku")

        assert "prefer" in result.lower()
        # Should NOT contain the old "MUST use" language
        assert "MUST use these exact" not in result


class TestNonQueryableEntityAdvisory:
    """Non-column matches (customlistvalue, customlist, savedsearch, script,
    scriptdeployment, workflow) must NOT be injected as authoritative filters —
    a list *value* like 'customlist_fw_cpu_platform.14' cannot go in a WHERE
    clause and caused a confident-wrong answer. They go to an advisory block.
    """

    @pytest.mark.asyncio
    async def test_customlistvalue_not_in_resolved_entities(self):
        """A customlistvalue match must not appear as a resolved <internal_script_id>."""
        adapter = _make_adapter(["Laptop 13"])
        db = _make_db_with_matches(
            [
                {
                    "script_id": "customlist_fw_cpu_platform.14",
                    "entity_type": "customlistvalue",
                    "sim": 1.0,
                    "description": "Value for list: customlist_fw_cpu_platform",
                },
            ]
        )

        result = await TenantEntityResolver.resolve_entities(
            "How many Laptop 13 did we sell?", TENANT_ID, db, adapter, "haiku"
        )

        assert "<internal_script_id>customlist_fw_cpu_platform.14</internal_script_id>" not in result
        assert "<resolved_entities>" not in result

    @pytest.mark.asyncio
    async def test_customlistvalue_emitted_as_advisory(self):
        """The matched list value is surfaced as advisory, with caution + the user term."""
        adapter = _make_adapter(["Laptop 13"])
        db = _make_db_with_matches(
            [
                {
                    "script_id": "customlist_fw_cpu_platform.14",
                    "entity_type": "customlistvalue",
                    "sim": 1.0,
                    "description": "Value for list: customlist_fw_cpu_platform",
                },
            ]
        )

        result = await TenantEntityResolver.resolve_entities(
            "How many Laptop 13 did we sell?", TENANT_ID, db, adapter, "haiku"
        )

        assert "<ambiguous_entities>" in result
        assert "Laptop 13" in result
        assert "customlist_fw_cpu_platform.14" in result  # surfaced, but advisory only
        assert "advisory" in result.lower()

    @pytest.mark.asyncio
    async def test_queryable_field_still_resolved(self):
        """Regression guard: a real custom field (queryable column) is still
        injected authoritatively in <resolved_entities>."""
        adapter = _make_adapter(["platform"])
        db = _make_db_with_matches(
            [
                {
                    "script_id": "custitem_fw_platform",
                    "entity_type": "itemcustomfield",
                    "sim": 0.9,
                    "description": "Type: SELECT",
                },
            ]
        )

        result = await TenantEntityResolver.resolve_entities("platform breakdown", TENANT_ID, db, adapter, "haiku")

        assert "<resolved_entities>" in result
        assert "<internal_script_id>custitem_fw_platform</internal_script_id>" in result
        assert "<ambiguous_entities>" not in result

    @pytest.mark.asyncio
    async def test_instruction_header_scopes_filter_authority_to_resolved(self):
        """With only an advisory match, the instruction header must scope the
        FROM/WHERE 'prefer these script IDs' guidance to resolved entities — it
        must NOT blanket-authorize using the matched (advisory) ids as filters."""
        adapter = _make_adapter(["Laptop 13"])
        db = _make_db_with_matches(
            [
                {
                    "script_id": "customlist_fw_cpu_platform.14",
                    "entity_type": "customlistvalue",
                    "sim": 1.0,
                    "description": "Value for list: customlist_fw_cpu_platform",
                },
            ]
        )

        result = await TenantEntityResolver.resolve_entities(
            "How many Laptop 13 did we sell?", TENANT_ID, db, adapter, "haiku"
        )

        # New scoped wording present...
        assert "Prefer the resolved entity script IDs" in result
        # ...and the old blanket FROM/WHERE authorization is gone.
        assert (
            "Prefer these internal script IDs and rules when constructing your SuiteQL FROM and WHERE clauses."
            not in result
        )
