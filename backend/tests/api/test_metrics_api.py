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


async def test_put_cross_tenant_isolation_404(client, admin_user, admin_user_b, db):
    """A metric authored by tenant B MUST be invisible to tenant A's PUT: the tenant
    route scopes on user.tenant_id, so tenant A editing tenant B's row → 404, and the
    row is NOT mutated. Guards against a cross-tenant edit via a guessed/leaked id."""
    from sqlalchemy import select

    from app.models.metric_definition import MetricDefinition

    _, headers_a = admin_user
    user_b, headers_b = admin_user_b

    created = await client.post(
        "/api/v1/metrics",
        headers=headers_b,
        json={
            "key": "rev_tenant_b",
            "display_name": "Revenue B",
            "definition": "revenue",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )
    assert created.status_code == 201, created.text
    mid = created.json()["id"]

    # Tenant A attempts to edit tenant B's row → 404 (not found in A's scope).
    resp = await client.put(
        f"/api/v1/metrics/{mid}",
        headers=headers_a,
        json={"display_name": "Hijacked"},
    )
    assert resp.status_code == 404, resp.text

    # The row is byte-identical: no display_name change, version still 1.
    row = (await db.execute(select(MetricDefinition).where(MetricDefinition.id == mid))).scalar_one()
    assert row.display_name == "Revenue B"
    assert row.version == 1
    assert row.tenant_id == user_b.tenant_id


async def test_put_system_row_via_tenant_route_404(client, superadmin_user, admin_user, db):
    """A SYSTEM-default row is owned by SYSTEM_TENANT_ID, not the tenant. Editing it
    through the TENANT route (scoped on user.tenant_id) must 404 — only the superadmin
    /system route may touch it. Guards against a tenant editing a cross-tenant default."""
    from sqlalchemy import delete, select

    from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
    from app.models.tenant import Tenant

    # Seed the SYSTEM tenant parent row (FK target) + clear catalog for a clean insert.
    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()
    await db.execute(delete(MetricDefinition))
    await db.flush()

    _, su_headers = superadmin_user
    created = await client.post(
        "/api/v1/metrics/system",
        headers=su_headers,
        json={
            "key": "sys_rev",
            "display_name": "System Revenue",
            "definition": "revenue",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )
    assert created.status_code == 201, created.text
    system_id = created.json()["id"]

    # A tenant admin tries to edit the SYSTEM row via the TENANT route → 404.
    _, admin_headers = admin_user
    resp = await client.put(
        f"/api/v1/metrics/{system_id}",
        headers=admin_headers,
        json={"display_name": "Hijacked"},
    )
    assert resp.status_code == 404, resp.text

    # Unchanged + still owned by SYSTEM.
    row = (await db.execute(select(MetricDefinition).where(MetricDefinition.id == system_id))).scalar_one()
    assert row.display_name == "System Revenue"
    assert row.tenant_id == SYSTEM_TENANT_ID


async def test_put_persists_synonyms(client, admin_user, db):
    """REAL bug (#1): synonyms is accepted in MetricUpdate but was silently dropped on
    PUT. A PUT carrying synonyms must persist them to the row (verified via direct DB
    read — there is no GET route and MetricResponse doesn't expose synonyms)."""
    from sqlalchemy import select

    from app.models.metric_definition import MetricDefinition

    _, headers = admin_user
    created = await client.post(
        "/api/v1/metrics",
        headers=headers,
        json={
            "key": "rev_syn",
            "display_name": "Revenue",
            "definition": "revenue",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )
    assert created.status_code == 201, created.text
    mid = created.json()["id"]

    resp = await client.put(
        f"/api/v1/metrics/{mid}",
        headers=headers,
        json={"synonyms": ["rev", "topline"]},
    )
    assert resp.status_code == 200, resp.text

    row = (await db.execute(select(MetricDefinition).where(MetricDefinition.id == mid))).scalar_one()
    await db.refresh(row)
    assert row.synonyms == ["rev", "topline"]


async def test_put_rejects_invalid_status(client, admin_user, db):
    """REAL bug (#2): MetricUpdate.status was a bare str and accepted "garbage". It is
    now a Literal of the lifecycle states, so an invalid status → 422 (schema reject)."""
    from sqlalchemy import select

    from app.models.metric_definition import MetricDefinition

    _, headers = admin_user
    created = await client.post(
        "/api/v1/metrics",
        headers=headers,
        json={
            "key": "rev_status",
            "display_name": "Revenue",
            "definition": "revenue",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )
    assert created.status_code == 201, created.text
    mid = created.json()["id"]

    resp = await client.put(
        f"/api/v1/metrics/{mid}",
        headers=headers,
        json={"status": "garbage"},
    )
    assert resp.status_code == 422, resp.text

    # The row's status is untouched (still the create-time "active"), version not bumped.
    row = (await db.execute(select(MetricDefinition).where(MetricDefinition.id == mid))).scalar_one()
    assert row.status == "active"
    assert row.version == 1


async def test_put_refreshes_intent_embedding_on_text_change(client, admin_user, db, monkeypatch):
    """REAL bug (#3): the intent embedding (used by resolve) went stale on PUT —
    create_metric derives it from display_name|definition|synonyms but update_metric
    never recomputed it. A PUT changing display_name must REFRESH the stored vector to
    the embedding of the NEW merged text. We stub embed_domain_query to a deterministic
    text-keyed vector so 'embedding == embedding(new text)' is observable (the real
    embedder returns None in tests, which would mask the staleness)."""
    from sqlalchemy import select

    from app.models.metric_definition import MetricDefinition
    from app.services.metrics import metric_authoring

    # Deterministic, text-sensitive fake: maps text → a 1536-d vector (the column is
    # Vector(1536)) whose first cell is a stable hash of the text. Distinct text ⇒
    # distinct vector ⇒ staleness is observable. Stable across calls for the same text.
    def _vec(text):
        return [float(hash(text) % 1000)] + [0.0] * 1535

    async def _fake_embed(text):
        return _vec(text)

    monkeypatch.setattr(metric_authoring, "embed_domain_query", _fake_embed)

    _, headers = admin_user
    created = await client.post(
        "/api/v1/metrics",
        headers=headers,
        json={
            "key": "rev_embed",
            "display_name": "Revenue",
            "definition": "revenue",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )
    assert created.status_code == 201, created.text
    mid = created.json()["id"]

    row = (await db.execute(select(MetricDefinition).where(MetricDefinition.id == mid))).scalar_one()
    await db.refresh(row)
    embed_before = list(row.intent_embedding)
    # Sanity: the create-time embedding is the embedding of the create-time text.
    assert embed_before == await _fake_embed("Revenue | revenue")

    resp = await client.put(
        f"/api/v1/metrics/{mid}",
        headers=headers,
        json={"display_name": "Total Revenue"},
    )
    assert resp.status_code == 200, resp.text

    row = (await db.execute(select(MetricDefinition).where(MetricDefinition.id == mid))).scalar_one()
    await db.refresh(row)
    embed_after = list(row.intent_embedding)
    # The embedding moved AND now equals the embedding of the NEW merged text.
    assert embed_after != embed_before
    assert embed_after == await _fake_embed("Total Revenue | revenue")


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


async def test_create_metric_rejects_invalid_unit(client, admin_user):
    """REAL bug (Task 11): MetricCreate.unit was a bare str and accepted arbitrary values.
    It is now a Literal of the five valid units, so an invalid unit → 422 at schema validation
    (before any DB write)."""
    _, headers = admin_user
    resp = await client.post(
        "/api/v1/metrics",
        headers=headers,
        json={
            "key": "bad_unit_metric",
            "display_name": "Bad Unit",
            "definition": "x",
            "unit": "bananas",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )
    assert resp.status_code == 422, resp.text


async def test_create_metric_persists_format(client, admin_user, db):
    """REAL bug (Task 11): format is accepted in MetricCreate but was silently dropped
    (not set in MetricDefinition constructor). A POST carrying format must persist it
    to the row (verified via direct DB read — MetricResponse doesn't expose format)."""
    from sqlalchemy import select

    from app.models.metric_definition import MetricDefinition

    _, headers = admin_user
    resp = await client.post(
        "/api/v1/metrics",
        headers=headers,
        json={
            "key": "fmt_metric",
            "display_name": "Formatted Metric",
            "definition": "x",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
            "format": "$#,##0",
        },
    )
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]

    row = (await db.execute(select(MetricDefinition).where(MetricDefinition.id == mid))).scalar_one()
    await db.refresh(row)
    assert row.format == "$#,##0"


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


# ── Minor: format field length validation ─────────────────────────────────────


async def test_post_rejects_format_over_64_chars(client, admin_user):
    """format is constrained to max_length=64. A garbage long string must → 422 at the
    schema layer before any DB write."""
    _, headers = admin_user
    resp = await client.post(
        "/api/v1/metrics",
        headers=headers,
        json={
            "key": "fmt_too_long",
            "display_name": "Long Format",
            "definition": "x",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
            "format": "x" * 65,
        },
    )
    assert resp.status_code == 422, resp.text


async def test_put_rejects_format_over_64_chars(client, admin_user, db):
    """PUT with format > 64 chars must → 422 at the schema layer."""
    _, headers = admin_user
    created = await client.post(
        "/api/v1/metrics",
        headers=headers,
        json={
            "key": "fmt_too_long_put",
            "display_name": "Long Format Put",
            "definition": "x",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )
    assert created.status_code == 201, created.text
    mid = created.json()["id"]

    resp = await client.put(
        f"/api/v1/metrics/{mid}",
        headers=headers,
        json={"format": "y" * 65},
    )
    assert resp.status_code == 422, resp.text


async def test_post_accepts_normal_format(client, admin_user, db):
    """A well-formed format string like '$#,##0' (len ≤ 64) must be accepted on POST."""
    from sqlalchemy import select

    from app.models.metric_definition import MetricDefinition

    _, headers = admin_user
    resp = await client.post(
        "/api/v1/metrics",
        headers=headers,
        json={
            "key": "fmt_normal",
            "display_name": "Normal Format",
            "definition": "x",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
            "format": "$#,##0",
        },
    )
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    row = (await db.execute(select(MetricDefinition).where(MetricDefinition.id == mid))).scalar_one()
    await db.refresh(row)
    assert row.format == "$#,##0"


async def test_put_accepts_normal_format(client, admin_user, db):
    """A well-formed format string on PUT must be accepted and persisted."""
    from sqlalchemy import select

    from app.models.metric_definition import MetricDefinition

    _, headers = admin_user
    created = await client.post(
        "/api/v1/metrics",
        headers=headers,
        json={
            "key": "fmt_put_normal",
            "display_name": "Normal Put Format",
            "definition": "x",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )
    assert created.status_code == 201, created.text
    mid = created.json()["id"]

    resp = await client.put(
        f"/api/v1/metrics/{mid}",
        headers=headers,
        json={"format": "#,##0.00%"},
    )
    assert resp.status_code == 200, resp.text
    row = (await db.execute(select(MetricDefinition).where(MetricDefinition.id == mid))).scalar_one()
    await db.refresh(row)
    assert row.format == "#,##0.00%"


# ── 082: SYSTEM /system POST + PUT write the metric AND a SYSTEM audit row ──────
#
# create_metric/update_metric now set SYSTEM tenant context for SYSTEM writes, so on
# Supabase (FORCE-RLS / non-bypass app role) BOTH the metric write (metric_definitions
# WITH CHECK, 082) and the following audit_service.log_event(tenant_id=SYSTEM) write
# (audit_events WITH CHECK, 021) pass. These route-level tests prove the full chain
# (route -> create/update_metric under SYSTEM ctx -> audit log_event(SYSTEM)) succeeds.
#
# NOTE (Correction C — SET LOCAL leak in the shared db fixture): we do NOT assert on the
# inter-request GUC value. Under local BYPASSRLS the WITH CHECK is not enforced, so this
# proves functional success only; enforcement is proven by the non-bypass-role test in
# tests/services/metrics/test_metric_rls_policy.py.


async def test_post_system_metric_writes_system_audit_event(client, superadmin_user, db):
    """082: POST /metrics/system as superadmin returns 201 AND writes an audit_events row
    with tenant_id == SYSTEM_TENANT_ID. The audit write only passes audit_events' WITH
    CHECK on Supabase because create_metric set SYSTEM context first."""
    from sqlalchemy import delete, select

    from app.models.audit import AuditEvent
    from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
    from app.models.tenant import Tenant

    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()
    await db.execute(delete(MetricDefinition))
    await db.flush()

    _, su_headers = superadmin_user
    resp = await client.post(
        "/api/v1/metrics/system",
        headers=su_headers,
        json={
            "key": "sys_audit_rev",
            "display_name": "System Revenue",
            "definition": "revenue",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )
    assert resp.status_code == 201, resp.text
    metric_id = resp.json()["id"]

    row = (await db.execute(select(MetricDefinition).where(MetricDefinition.id == metric_id))).scalar_one()
    assert row.tenant_id == SYSTEM_TENANT_ID

    audit = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.resource_id == metric_id,
                AuditEvent.action == "metric.create",
            )
        )
    ).scalar_one()
    assert audit.tenant_id == SYSTEM_TENANT_ID, (
        f"audit_events row for a SYSTEM metric create must carry tenant_id=SYSTEM, got {audit.tenant_id}"
    )


async def test_put_system_metric_writes_system_audit_event(client, superadmin_user, db):
    """082: PUT /metrics/system/{id} as superadmin returns 200 AND writes an audit_events
    row with tenant_id == SYSTEM_TENANT_ID. update_metric sets SYSTEM context before the
    SELECT...FOR UPDATE so it finds the SYSTEM row and the UPDATE + audit write pass the
    WITH CHECK on Supabase."""
    from sqlalchemy import delete, select

    from app.models.audit import AuditEvent
    from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
    from app.models.tenant import Tenant

    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()
    await db.execute(delete(MetricDefinition))
    await db.flush()

    _, su_headers = superadmin_user
    created = await client.post(
        "/api/v1/metrics/system",
        headers=su_headers,
        json={
            "key": "sys_audit_cash",
            "display_name": "System Cash",
            "definition": "cash",
            "unit": "currency",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 0", "dialect": "suiteql"},
        },
    )
    assert created.status_code == 201, created.text
    metric_id = created.json()["id"]

    resp = await client.put(
        f"/api/v1/metrics/system/{metric_id}",
        headers=su_headers,
        json={"display_name": "System Cash Balance"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 2

    row = (await db.execute(select(MetricDefinition).where(MetricDefinition.id == metric_id))).scalar_one()
    assert row.tenant_id == SYSTEM_TENANT_ID
    assert row.display_name == "System Cash Balance"

    audit = (
        await db.execute(
            select(AuditEvent).where(
                AuditEvent.resource_id == metric_id,
                AuditEvent.action == "metric.update",
            )
        )
    ).scalar_one()
    assert audit.tenant_id == SYSTEM_TENANT_ID, (
        f"audit_events row for a SYSTEM metric update must carry tenant_id=SYSTEM, got {audit.tenant_id}"
    )
