# backend/tests/services/metrics/test_metric_catalog_seeder.py
from sqlalchemy import func, select

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.models.tenant import Tenant
from app.services.metrics.metric_catalog_seeder import seed_system_metrics


async def _ensure_system_tenant(db):
    # SYSTEM-default metric rows FK to tenants.id; seed the canonical SYSTEM tenant
    # parent row (rolled back per test by the db fixture) so the insert is valid.
    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()


async def test_seed_is_idempotent_and_system_scoped(db):
    await _ensure_system_tenant(db)
    n1 = await seed_system_metrics(db)
    await db.flush()
    n2 = await seed_system_metrics(db)  # re-seed
    await db.flush()
    assert n1 == n2 and n1 >= 8
    total = (
        await db.execute(
            select(func.count()).select_from(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID)
        )
    ).scalar_one()
    assert total == n1  # idempotent: no duplicates
