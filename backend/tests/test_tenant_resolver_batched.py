"""The entity resolver looks up every extracted entity in ONE query (N+1 fix, 2026-10-05).

Before: two pg_trgm queries per entity (natural_name, then script_id) on every chat turn.
After: one query for all entities. These tests run on the real database with pg_trgm and
require the batched lookup to return exactly what the per-entity queries returned.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.core.database import set_tenant_context
from app.models.tenant_entity_mapping import TenantEntityMapping
from app.services.chat.tenant_resolver import TenantEntityResolver, best_entity_matches
from tests.conftest import create_test_tenant

MAPPINGS = [
    ("itemcustomfield", "FW Platform", "custitem_fw_platform", "Type: SELECT"),
    ("location", "Panurgy", "location_panurgy", "Repair location"),
    ("transactionbodycustomfield", "Rush Flag", "custbody_rush_flag", "Rush flag"),
    ("transactionbodycustomfield", "Solidus Order Total", "custbody_fw_solidus_order_total", None),
    ("customlistvalue", "Laptop 13", "customlist_fw_cpu_platform.14", "Value for list"),
]
ENTITIES = ["FW Platform", "custbody_fw_solidus_order_total", "Panurgy", "zzz nothing alike", "platform", "Laptop 13"]


async def _seed(db):
    tenant = await create_test_tenant(db)
    await set_tenant_context(db, str(tenant.id))
    for entity_type, name, script_id, description in MAPPINGS:
        db.add(
            TenantEntityMapping(
                tenant_id=tenant.id,
                entity_type=entity_type,
                natural_name=name,
                script_id=script_id,
                description=description,
            )
        )
    await db.flush()
    return tenant.id


async def _per_entity_reference(db, tenant_id, entity):
    """The pre-fix algorithm, verbatim: best natural_name match, best script_id match, pick."""
    name_row = (
        await db.execute(
            select(TenantEntityMapping, func.similarity(TenantEntityMapping.natural_name, entity).label("sim"))
            .where(TenantEntityMapping.tenant_id == tenant_id)
            .where(TenantEntityMapping.natural_name.op("%")(entity))
            .order_by(func.similarity(TenantEntityMapping.natural_name, entity).desc())
            .limit(1)
        )
    ).first()
    script_row = (
        await db.execute(
            select(TenantEntityMapping, func.similarity(TenantEntityMapping.script_id, entity).label("sim"))
            .where(TenantEntityMapping.tenant_id == tenant_id)
            .where(TenantEntityMapping.script_id.op("%")(entity))
            .order_by(func.similarity(TenantEntityMapping.script_id, entity).desc())
            .limit(1)
        )
    ).first()
    row = None
    if name_row and script_row:
        row = name_row if name_row.sim >= script_row.sim else script_row
    else:
        row = name_row or script_row
    if row is None:
        return None
    m = row.TenantEntityMapping
    return (m.script_id, m.entity_type, m.description, round(float(row.sim), 6))


@pytest.mark.asyncio
async def test_batched_lookup_returns_what_the_per_entity_queries_returned(db):
    tenant_id = await _seed(db)
    expected = [await _per_entity_reference(db, tenant_id, e) for e in ENTITIES]
    got = [
        None if m is None else (m.script_id, m.entity_type, m.description, round(float(m.sim), 6))
        for m in await best_entity_matches(db, tenant_id, ENTITIES)
    ]
    assert got == expected
    assert any(g is not None for g in got) and any(g is None for g in got)  # both cases exercised


@pytest.mark.asyncio
async def test_one_lookup_query_for_any_number_of_entities(db):
    tenant_id = await _seed(db)
    adapter = AsyncMock()
    response = MagicMock()
    response.text_blocks = [json.dumps(ENTITIES)]
    adapter.create_message = AsyncMock(return_value=response)
    calls = []
    original = db.execute

    async def counting_execute(*args, **kwargs):
        calls.append(args[0])
        return await original(*args, **kwargs)

    db.execute = counting_execute
    result = await TenantEntityResolver.resolve_entities("FW Platform at Panurgy", tenant_id, db, adapter, "haiku")
    # One batched entity lookup + one learned-rules query, for six entities (was 2 * 6 + 1 = 13).
    assert len(calls) == 2
    assert "custitem_fw_platform" in result


@pytest.mark.asyncio
async def test_no_entities_means_no_lookup_query(db):
    assert await best_entity_matches(db, uuid.uuid4(), []) == []
