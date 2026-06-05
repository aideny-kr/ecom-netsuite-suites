# backend/tests/services/metrics/test_metric_catalog_seeder.py
import pytest
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


async def test_concurrent_reseed_uses_upsert_not_unique_violation(db):
    """R3#25 — two concurrent seeders must converge via upsert, not race into a
    UNIQUE(tenant_id, key) violation.

    Within a single session (shared uncommitted state) the second seed() call
    must not raise IntegrityError / UniqueViolationError. With the old
    delete-then-insert approach the second DELETE removes what the first inserted
    and the second INSERT re-adds — that's safe within a transaction but breaks
    if two sessions race at the COMMIT boundary.  The ON CONFLICT DO UPDATE
    upsert fixes that by making the second call a no-op update, never a new row.

    This test simulates the within-transaction half of that invariant:
    seed → flush → seed again → flush → no exception + stable count.
    The across-commit half is already covered by
    test_metric_seed_commit_idempotency.py (F5).
    """
    from sqlalchemy.exc import IntegrityError

    await _ensure_system_tenant(db)

    n1 = await seed_system_metrics(db)
    await db.flush()

    # A second seed in the same uncommitted session must NOT raise.
    try:
        n2 = await seed_system_metrics(db)
        await db.flush()
    except IntegrityError as exc:
        pytest.fail(f"seeder raised IntegrityError on second call — ON CONFLICT upsert is required (R3#25): {exc}")

    assert n1 == n2, "seeder must return the same count on every call"
    total = (
        await db.execute(
            select(func.count()).select_from(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID)
        )
    ).scalar_one()
    assert total == n1, f"upsert must not double rows: expected {n1}, got {total}"


async def test_placeholder_defaults_are_draft_not_active(db):
    """D3: SELECT 0 placeholder rows must seed as draft, not active.
    §12.2: all seeded rows must carry a non-null 1536-d embedding."""
    from sqlalchemy import select

    from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
    from app.services.metrics.metric_catalog_seeder import seed_system_metrics

    await seed_system_metrics(db)
    rows = (
        (await db.execute(select(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID)))
        .scalars()
        .all()
    )
    placeholders = [r for r in rows if r.source_kind in ("suiteql", "bigquery")]
    assert placeholders, "expected seeded query-backed defaults"
    assert all(r.status == "draft" for r in placeholders), "D3: SELECT 0 placeholders must seed draft, never active"
    assert all(r.intent_embedding is not None for r in placeholders), "§12.2: seeded rows need non-null embeddings"
