async def test_non_admin_forbidden(client, member_user):
    _, headers = member_user
    resp = await client.post(
        "/api/v1/metrics",
        json={
            "key": "net_margin",
            "display_name": "Net Margin",
            "definition": "x",
            "unit": "percent",
            "source_kind": "expression",
            "expression": "net_income / gross_revenue",
            "depends_on": ["net_income", "gross_revenue"],
        },
        headers=headers,
    )
    assert resp.status_code == 403


async def test_admin_can_author_tenant_metric(client, admin_user):
    _, headers = admin_user
    resp = await client.post(
        "/api/v1/metrics",
        json={
            "key": "net_margin",
            "display_name": "Net Margin",
            "definition": "x",
            "unit": "percent",
            "source_kind": "expression",
            "expression": "net_income / gross_revenue",
            "depends_on": ["net_income", "gross_revenue"],
        },
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["key"] == "net_margin"


_SYSTEM_METRIC_PAYLOAD = {
    "key": "net_margin",
    "display_name": "Net Margin",
    "definition": "x",
    "unit": "percent",
    "source_kind": "expression",
    "expression": "net_income / gross_revenue",
    "depends_on": ["net_income", "gross_revenue"],
}


async def test_tenant_admin_cannot_author_system_metric(client, admin_user):
    # A tenant admin holds metrics.manage but is NOT a superadmin: the SYSTEM
    # endpoint must reject them so cross-tenant authority stays superadmin-gated.
    _, headers = admin_user
    resp = await client.post(
        "/api/v1/metrics/system",
        json=_SYSTEM_METRIC_PAYLOAD,
        headers=headers,
    )
    assert resp.status_code == 403


async def test_superadmin_can_author_system_metric(client, superadmin_user, db):
    from sqlalchemy import delete, select

    from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
    from app.models.tenant import Tenant

    # SYSTEM-default metric rows FK to tenants.id; seed the canonical SYSTEM tenant
    # parent row (rolled back per test by the db fixture) so the insert is valid.
    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()
    # Test hygiene: writes a SYSTEM net_margin row, which collides on
    # UNIQUE(tenant_id, key) with the seeder's net_margin if the catalog is already
    # seeded. Clear the catalog first (rolled back per the db fixture).
    await db.execute(delete(MetricDefinition))
    await db.flush()

    _, headers = superadmin_user
    resp = await client.post(
        "/api/v1/metrics/system",
        json=_SYSTEM_METRIC_PAYLOAD,
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["key"] == "net_margin"

    # The row must be written under SYSTEM_TENANT_ID (cross-tenant default), not
    # the superadmin's own tenant.
    row = (await db.execute(select(MetricDefinition).where(MetricDefinition.key == "net_margin"))).scalar_one()
    assert row.tenant_id == SYSTEM_TENANT_ID


async def test_create_metric_is_self_sufficient_when_system_tenant_absent(db, monkeypatch):
    """REAL invariant (blocker #3, authoring path): on a FRESH DB the SYSTEM tenant
    row does NOT exist, so create_metric()'s INSERT INTO metric_definitions FKs to a
    missing parent and raises ForeignKeyViolationError. The API test above masks this
    by pre-inserting the SYSTEM tenant in test code (vacuous — the create_metric
    defense-in-depth ensure_system_tenant() block can be deleted and that test stays
    green). Here we target the service fn directly: DELETE the SYSTEM tenant + its
    metric rows, then call create_metric(tenant_id=SYSTEM_TENANT_ID) WITHOUT seeding
    the tenant ourselves — create_metric must upsert the SYSTEM tenant first and
    persist the row with no FK violation."""
    from sqlalchemy import delete, select

    from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
    from app.models.tenant import Tenant
    from app.services.metrics import metric_authoring
    from app.services.metrics.metric_authoring import create_metric

    # Isolate the FK-provisioning invariant from embedding availability (network).
    async def _fake_embed(_text):
        return None

    monkeypatch.setattr(metric_authoring, "embed_domain_query", _fake_embed)

    # Tear down to mimic a fresh DB: SYSTEM metric rows then the SYSTEM tenant row.
    await db.execute(delete(MetricDefinition).where(MetricDefinition.tenant_id == SYSTEM_TENANT_ID))
    await db.execute(delete(Tenant).where(Tenant.id == SYSTEM_TENANT_ID))
    await db.flush()
    assert (
        await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))
    ).scalar_one_or_none() is None  # genuinely absent — create_metric is on its own

    # No pre-seed of the SYSTEM tenant here — create_metric itself must provision it.
    metric = await create_metric(db, tenant_id=SYSTEM_TENANT_ID, payload=_SYSTEM_METRIC_PAYLOAD)
    await db.flush()

    assert metric.tenant_id == SYSTEM_TENANT_ID
    assert metric.key == "net_margin"
    # create_metric created the SYSTEM tenant parent row (defense-in-depth upsert).
    assert (
        await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))
    ).scalar_one_or_none() == SYSTEM_TENANT_ID
    persisted = (await db.execute(select(MetricDefinition).where(MetricDefinition.key == "net_margin"))).scalar_one()
    assert persisted.tenant_id == SYSTEM_TENANT_ID
