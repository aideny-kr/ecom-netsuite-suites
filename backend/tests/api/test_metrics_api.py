import pytest


async def test_put_bumps_version_and_can_reactivate(client, admin_user, db):
    """PUT /metrics/{id} must bump version and allow status transitions (incl. reactivating)."""
    user, headers = admin_user
    created = await client.post(
        "/api/v1/metrics",
        headers=headers,
        json={
            "key": "rev_v2",
            "display_name": "Revenue",
            "definition": "revenue",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
            "params_schema": {"period": {"type": "period"}},
        },
    )
    assert created.status_code == 201, created.text
    mid = created.json()["id"]
    resp = await client.put(
        f"/api/v1/metrics/{mid}",
        headers=headers,
        json={
            "blessed_spec": {"query": "SELECT SUM(amount) FROM transaction", "dialect": "suiteql"},
            "status": "active",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 2
    assert body["status"] == "active"


async def test_put_returns_404_when_not_found(client, admin_user):
    """PUT /metrics/{id} must return 404 when the metric id does not belong to the tenant."""
    _, headers = admin_user
    import uuid

    fake_id = str(uuid.uuid4())
    resp = await client.put(
        f"/api/v1/metrics/{fake_id}",
        headers=headers,
        json={"blessed_spec": {"query": "SELECT 1", "dialect": "suiteql"}},
    )
    assert resp.status_code == 404, resp.text


async def test_put_non_admin_forbidden(client, member_user):
    """PUT /metrics/{id} must be gated on metrics.manage permission."""
    import uuid

    _, headers = member_user
    resp = await client.put(
        f"/api/v1/metrics/{uuid.uuid4()}",
        headers=headers,
        json={"display_name": "X"},
    )
    assert resp.status_code == 403, resp.text


async def test_put_system_metric_forbidden_for_tenant_admin(client, admin_user):
    """PUT /metrics/system/{id} must reject a tenant admin (non-superadmin)."""
    import uuid

    _, headers = admin_user
    resp = await client.put(
        f"/api/v1/metrics/system/{uuid.uuid4()}",
        headers=headers,
        json={"display_name": "X"},
    )
    assert resp.status_code == 403, resp.text


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


async def _seed_leaves(db, tenant_id):
    """Seed the two leaf metrics (net_income, gross_revenue) an expression metric
    depends on, so author-time leaf-existence passes. Authoring an expression metric
    over phantom leaves now 422s (anti-hallucination: no blessed-but-un-computable
    metric)."""
    from app.models.metric_definition import MetricDefinition

    for key in ("net_income", "gross_revenue"):
        db.add(
            MetricDefinition(
                tenant_id=tenant_id,
                key=key,
                display_name=key,
                definition="x",
                unit="currency",
                source_kind="suiteql",
                blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
                params_schema={},
                status="active",
                version=1,
            )
        )
    await db.flush()


async def test_admin_can_author_tenant_metric(client, admin_user, db):
    user, headers = admin_user
    await _seed_leaves(db, user.tenant_id)
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


async def test_author_expression_metric_over_phantom_leaves_422(client, admin_user):
    """REAL invariant at the API boundary (major #8): authoring an expression metric
    whose depends_on leaves do NOT exist in the catalog must 422, not 201. A 201 here
    would persist a blessed metric that can only ever resolve to missing_dependency —
    the catalog advertising an un-computable named metric."""
    _, headers = admin_user
    resp = await client.post(
        "/api/v1/metrics",
        json={
            "key": "net_margin",
            "display_name": "Net Margin",
            "definition": "x",
            "unit": "percent",
            "source_kind": "expression",
            "expression": "ghost_income / ghost_revenue",
            "depends_on": ["ghost_income", "ghost_revenue"],
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


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
    # Author-time leaf-existence: net_margin's leaves must exist (as SYSTEM rows here).
    await _seed_leaves(db, SYSTEM_TENANT_ID)

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
    # Use a query-backed (leafless) payload to isolate the FK-provisioning invariant
    # from author-time leaf-existence: an expression metric's leaves can't exist while
    # the SYSTEM tenant is deleted, which would conflate two checks.
    leafless_payload = {
        "key": "gross_revenue",
        "display_name": "Gross Revenue",
        "definition": "x",
        "unit": "currency",
        "source_kind": "suiteql",
        "blessed_spec": {"query": "SELECT 1", "dialect": "suiteql"},
    }
    metric = await create_metric(db, tenant_id=SYSTEM_TENANT_ID, payload=leafless_payload)
    await db.flush()

    assert metric.tenant_id == SYSTEM_TENANT_ID
    assert metric.key == "gross_revenue"
    # create_metric created the SYSTEM tenant parent row (defense-in-depth upsert).
    assert (
        await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))
    ).scalar_one_or_none() == SYSTEM_TENANT_ID
    persisted = (await db.execute(select(MetricDefinition).where(MetricDefinition.key == "gross_revenue"))).scalar_one()
    assert persisted.tenant_id == SYSTEM_TENANT_ID
