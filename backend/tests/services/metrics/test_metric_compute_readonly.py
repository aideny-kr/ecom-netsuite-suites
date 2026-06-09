# backend/tests/services/metrics/test_metric_compute_readonly.py
import uuid

import pytest
from sqlalchemy import delete, select

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.models.tenant import Tenant
from app.services.metrics.metric_compute import compute_metric

pytestmark = pytest.mark.asyncio


async def _ensure_system_tenant(db):
    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()


async def _seed_failing_system_metric(db):
    await _ensure_system_tenant(db)
    # Clear the catalog so we do not collide with the seeder's committed rows on
    # UNIQUE(tenant_id, key). The db fixture rolls this back after the test.
    await db.execute(delete(MetricDefinition))
    await db.flush()
    m = MetricDefinition(
        tenant_id=SYSTEM_TENANT_ID,
        key="cash",
        display_name="Cash",
        definition="cash",
        unit="currency",
        source_kind="suiteql",
        blessed_spec={"query": "SELECT 1 FROM nonexistent_table_xyz", "dialect": "suiteql"},
        params_schema={"period": {"type": "period"}},
        status="active",
        version=1,
        provenance={"author": "test"},
    )
    db.add(m)
    await db.flush()
    return m


async def test_compute_failure_does_not_mutate_status(db):
    tenant = uuid.uuid4()
    m = await _seed_failing_system_metric(db)
    result = await compute_metric(db, tenant_id=tenant, key="cash", params={"period": "this_month"}, context={})
    assert result["error"] == "blessed_query_failed"
    await db.refresh(m)
    assert m.status == "active", "compute must NOT flip a SYSTEM metric out of active (D1: read-only)"
