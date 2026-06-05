# backend/tests/services/metrics/test_metric_catalog_seeder.py
from sqlalchemy import delete, func, select

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


async def test_seeder_is_self_sufficient_when_system_tenant_absent(db):
    """REAL invariant (blocker #3): on a FRESH DB the SYSTEM tenant row does NOT
    exist, so the seeder's INSERT INTO metric_definitions FKs to a missing parent
    and raises ForeignKeyViolationError. The prior test masked this by pre-inserting
    the SYSTEM tenant in test code (vacuous). Here we DELETE the SYSTEM tenant + its
    metric rows, then run the seeder WITHOUT seeding the tenant ourselves — the
    seeder must upsert the SYSTEM tenant first and seed 9 rows with no FK violation."""
    # Tear down to mimic a fresh DB: SYSTEM metric rows then the SYSTEM tenant row.
    await db.execute(delete(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID))
    await db.execute(delete(Tenant).where(Tenant.id == SYSTEM_TENANT_ID))
    await db.flush()
    assert (
        await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))
    ).scalar_one_or_none() is None  # genuinely absent — the seeder is on its own

    # No _ensure_system_tenant() here — the seeder itself must provision the parent.
    n = await seed_system_metrics(db)
    await db.flush()

    assert n == 9
    # The seeder created the SYSTEM tenant parent row (defense-in-depth upsert).
    assert (
        await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))
    ).scalar_one_or_none() == SYSTEM_TENANT_ID
    total = (
        await db.execute(
            select(func.count()).select_from(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID)
        )
    ).scalar_one()
    assert total == 9
