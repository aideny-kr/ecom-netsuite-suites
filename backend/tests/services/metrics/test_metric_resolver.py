# backend/tests/services/metrics/test_metric_resolver.py
from sqlalchemy import delete, select

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.models.tenant import Tenant
from app.services.metrics.metric_resolver import resolve_metrics


async def _clear_catalog(db):
    # Test hygiene: these tests insert SYSTEM rows whose keys collide on
    # UNIQUE(tenant_id, key) with the system seeder's keys if the catalog is
    # already seeded. Clear the catalog first (rolled back per the db fixture).
    await db.execute(delete(MetricDefinition))
    await db.flush()


async def _ensure_system_tenant(db):
    # SYSTEM-default metric rows FK to tenants.id; seed the canonical SYSTEM tenant
    # parent row (rolled back per test by the db fixture) so the insert is valid.
    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()


async def _add(db, tenant_id, key, synonyms=None):
    if tenant_id == SYSTEM_TENANT_ID:
        await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=tenant_id,
            key=key,
            display_name=key.replace("_", " ").title(),
            definition="x",
            unit="percent",
            source_kind="expression",
            expression="a/b",
            depends_on=["a", "b"],
            synonyms=synonyms or [],
            status="active",
            version=1,
        )
    )
    await db.flush()


async def test_resolve_returns_system_and_tenant_excludes_other(db, tenant_a, tenant_b):
    await _clear_catalog(db)
    await _add(db, SYSTEM_TENANT_ID, "gross_revenue")
    await _add(db, tenant_a.id, "net_margin", synonyms=["bottom line margin"])
    await _add(db, tenant_b.id, "secret_metric")

    keys = {m.key for m in await resolve_metrics(db, tenant_id=tenant_a.id, query="bottom line margin", top_k=10)}
    assert "net_margin" in keys  # tenant synonym match
    assert "gross_revenue" in keys  # SYSTEM default visible
    assert "secret_metric" not in keys  # other tenant excluded


async def test_tenant_override_wins_by_key(db, tenant_a):
    await _clear_catalog(db)
    await _add(db, SYSTEM_TENANT_ID, "net_margin")
    await _add(db, tenant_a.id, "net_margin")
    matches = await resolve_metrics(db, tenant_id=tenant_a.id, query="net_margin", top_k=10)
    nm = [m for m in matches if m.key == "net_margin"]
    assert len(nm) == 1 and nm[0].tenant_id == tenant_a.id
