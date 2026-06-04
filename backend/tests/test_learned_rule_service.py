"""Tests for learned_rule_service — list/create/update/delete tenant learned rules.

Motivated by the 2026-06-04 incident where a broken learned rule rotted unseen
for 3 months because there was no surface to view or disable rules.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import learned_rule_service

TENANT = uuid.uuid4()
OTHER_TENANT = uuid.uuid4()
RULE_ID = uuid.uuid4()
ACTOR = uuid.uuid4()


def _db_returning(scalar=None, scalar_list=None) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    scalars = MagicMock()
    scalars.all.return_value = scalar_list or []
    result.scalars.return_value = scalars
    db.execute.return_value = result
    return db


class TestListRules:
    @pytest.mark.asyncio
    async def test_returns_all_rows_for_tenant(self):
        r1, r2 = MagicMock(), MagicMock()
        db = _db_returning(scalar_list=[r1, r2])

        rules = await learned_rule_service.list_rules(db, TENANT)

        assert rules == [r1, r2]
        db.execute.assert_awaited_once()


class TestCreateRule:
    @pytest.mark.asyncio
    async def test_adds_model_with_fields(self):
        db = _db_returning()

        rule = await learned_rule_service.create_rule(
            db, TENANT, rule_description="count laptops by class", rule_category="query_logic", created_by=ACTOR
        )

        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert added.tenant_id == TENANT
        assert added.rule_description == "count laptops by class"
        assert added.rule_category == "query_logic"
        assert added.is_active is True
        assert added.created_by == ACTOR
        assert rule is added
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_defaults_category_to_general(self):
        db = _db_returning()
        await learned_rule_service.create_rule(db, TENANT, rule_description="x", rule_category=None, created_by=ACTOR)
        assert db.add.call_args[0][0].rule_category == "general"


class TestUpdateRule:
    @pytest.mark.asyncio
    async def test_mutates_only_provided_fields(self):
        existing = MagicMock()
        existing.rule_description = "old"
        existing.rule_category = "general"
        existing.is_active = True
        db = _db_returning(scalar=existing)

        result = await learned_rule_service.update_rule(db, TENANT, RULE_ID, is_active=False)

        assert existing.is_active is False
        assert existing.rule_description == "old"  # untouched
        assert result is existing
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        db = _db_returning(scalar=None)
        result = await learned_rule_service.update_rule(db, TENANT, RULE_ID, is_active=False)
        assert result is None
        db.flush.assert_not_awaited()


class TestDeleteRule:
    @pytest.mark.asyncio
    async def test_deletes_when_found(self):
        existing = MagicMock()
        db = _db_returning(scalar=existing)

        ok = await learned_rule_service.delete_rule(db, TENANT, RULE_ID)

        assert ok is True
        db.delete.assert_awaited_once_with(existing)

    @pytest.mark.asyncio
    async def test_returns_false_when_not_found(self):
        db = _db_returning(scalar=None)
        ok = await learned_rule_service.delete_rule(db, TENANT, RULE_ID)
        assert ok is False
        db.delete.assert_not_awaited()
