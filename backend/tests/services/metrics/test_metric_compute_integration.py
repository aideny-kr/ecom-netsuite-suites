# backend/tests/services/metrics/test_metric_compute_integration.py
from sqlalchemy import delete, select

from app.models.audit import AuditEvent
from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.models.tenant import Tenant
from app.services.metrics.metric_compute import compute_metric

# A fixed 1536-d query embedding (the seeder uses 1536-d intent_embedding).
_DIM = 1536
_QUERY_VEC = [1.0] + [0.0] * (_DIM - 1)  # unit vector along axis 0
_NEAR_VEC = list(_QUERY_VEC)  # cosine distance 0 → ranks FIRST
_FAR_VEC = [0.0, 1.0] + [0.0] * (_DIM - 2)  # orthogonal → cosine distance 1, ranks last


async def _ensure_system_tenant(db):
    # SYSTEM-default metric rows FK to tenants.id; seed the canonical SYSTEM tenant
    # parent row (rolled back per test by the db fixture) so the insert is valid.
    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()
    # Test hygiene: every test below inserts SYSTEM rows whose keys (net_margin,
    # gross_revenue, net_income, net_revenue, ...) collide on UNIQUE(tenant_id, key)
    # with the system seeder's keys if the catalog is already seeded. Clear it first
    # (rolled back per the db fixture).
    await db.execute(delete(MetricDefinition))
    await db.flush()


async def test_expression_metric_computes_as_one_row_data_table(db, tenant_a, monkeypatch):
    await _ensure_system_tenant(db)
    # Two single-source leaves with stubbed execution + one expression metric.
    for key, val in [("net_income", 30.0), ("gross_revenue", 120.0)]:
        db.add(
            MetricDefinition(
                tenant_id=SYSTEM_TENANT_ID,
                key=key,
                display_name=key,
                definition="x",
                unit="currency",
                source_kind="suiteql",
                blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
                params_schema={"period": {"type": "period"}},
                status="active",
                version=1,
            )
        )
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="net_margin",
            display_name="Net Margin",
            definition="x",
            unit="percent",
            source_kind="expression",
            expression="net_income / gross_revenue",
            depends_on=["net_income", "gross_revenue"],
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    # Stub leaf SQL execution so the test is hermetic (NetSuite/BQ not reachable in CI).
    async def _fake_scalar(db, tenant_id, metric, coerced, context):
        return {"net_income": 30.0, "gross_revenue": 120.0}[metric.key]

    monkeypatch.setattr("app.services.metrics.metric_compute._execute_scalar_query", _fake_scalar)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="net_margin",
        params={"period": "last_quarter"},
        context={"fiscal_year_start_month": 1},
    )
    assert out["row_count"] == 1
    assert out["columns"] == ["Metric", "Value", "Unit", "Period"]
    assert out["rows"][0][0] == "Net Margin"
    assert round(out["rows"][0][1], 4) == 0.25


async def test_exact_key_survives_embedding_decoy_eviction(db, tenant_a, monkeypatch):
    """Production repro: with seeded 1536-d intent_embeddings, an exact-key compute
    request must NOT be evicted by a sibling metric whose embedding ranks nearer to
    the key string. The resolver inserts embedding-nearest rows first, dedupes by key,
    then slices — so a too-narrow top_k drops the requested exact-key row."""
    await _ensure_system_tenant(db)
    # Decoy ranks nearest to the query embedding; the requested metric is farthest.
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="zzz_decoy",
            display_name="ZZZ Decoy",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
            params_schema={"period": {"type": "period"}},
            intent_embedding=_NEAR_VEC,
            status="active",
            version=1,
        )
    )
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="net_revenue",
            display_name="Net Revenue",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
            params_schema={"period": {"type": "period"}},
            intent_embedding=_FAR_VEC,
            status="active",
            version=1,
        )
    )
    await db.flush()

    async def _fake_embed(text):
        return list(_QUERY_VEC)

    monkeypatch.setattr("app.services.metrics.metric_resolver.embed_domain_query", _fake_embed)

    async def _fake_scalar(db, tenant_id, metric, coerced, context):
        return 99.0

    monkeypatch.setattr("app.services.metrics.metric_compute._execute_scalar_query", _fake_scalar)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="net_revenue",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )
    assert "error" not in out, out
    assert out["rows"][0][0] == "Net Revenue"
    assert out["rows"][0][1] == 99.0


async def test_expression_leaf_survives_embedding_decoy_eviction(db, tenant_a, monkeypatch):
    """The expression path resolves each depends_on leaf by exact key too; an embedding
    decoy that ranks nearer than the leaf must not produce a false 'missing_dependency'."""
    await _ensure_system_tenant(db)
    # Decoy ranks nearest to every embedded query (same near vector).
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="zzz_decoy",
            display_name="ZZZ Decoy",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
            params_schema={"period": {"type": "period"}},
            intent_embedding=_NEAR_VEC,
            status="active",
            version=1,
        )
    )
    for key in ("net_income", "gross_revenue"):
        db.add(
            MetricDefinition(
                tenant_id=SYSTEM_TENANT_ID,
                key=key,
                display_name=key,
                definition="x",
                unit="currency",
                source_kind="suiteql",
                blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
                params_schema={"period": {"type": "period"}},
                intent_embedding=_FAR_VEC,
                status="active",
                version=1,
            )
        )
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="net_margin",
            display_name="Net Margin",
            definition="x",
            unit="percent",
            source_kind="expression",
            expression="net_income / gross_revenue",
            depends_on=["net_income", "gross_revenue"],
            params_schema={"period": {"type": "period"}},
            intent_embedding=_FAR_VEC,
            status="active",
            version=1,
        )
    )
    await db.flush()

    async def _fake_embed(text):
        return list(_QUERY_VEC)

    monkeypatch.setattr("app.services.metrics.metric_resolver.embed_domain_query", _fake_embed)

    async def _fake_scalar(db, tenant_id, metric, coerced, context):
        return {"net_income": 30.0, "gross_revenue": 120.0}[metric.key]

    monkeypatch.setattr("app.services.metrics.metric_compute._execute_scalar_query", _fake_scalar)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="net_margin",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )
    assert "error" not in out, out
    assert out["rows"][0][0] == "Net Margin"
    assert round(out["rows"][0][1], 4) == 0.25


# --- R2 fail-closed execution: a failed blessed query must NOT return 0.0 ---


async def test_failed_blessed_query_does_not_fabricate_zero(db, tenant_a, monkeypatch):
    """THE anti-hallucination invariant. When the blessed SuiteQL query errors
    (schema drift, NetSuite down, bad credentials), the tool path returns
    {"error": True, "message": ...} — it carries NO 'rows'. The prior code did
    `result.get("rows") or [[0]]` and silently returned a fabricated 0.0 as the
    metric value. compute_metric MUST instead fail closed: a NUMBER-FREE error
    dict, never a value/rows, AND the metric row is flipped to needs_review."""
    await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="gross_revenue",
            display_name="Gross Revenue",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={"query": "SELECT SUM(amount) FROM transactionline", "dialect": "suiteql"},
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    # Stub the REAL boundary (the tool), so _execute_scalar_query runs for real.
    async def _boom_execute(params, context=None, **kwargs):
        return {"error": True, "message": "boom"}

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _boom_execute)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )

    # (a) it is a number-free structured error — NOT a data_table with a value
    assert out.get("error") == "blessed_query_failed", out
    assert out.get("status") == "needs_review"
    assert out.get("key") == "gross_revenue"
    assert "rows" not in out
    assert "value" not in out
    # the fabricated zero must appear NOWHERE in the payload
    assert 0 not in out.values()
    assert 0.0 not in out.values()

    # (b) the metric row is flipped to needs_review and persisted (flush visible in-session)
    row = (
        await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
                MetricDefinition.key == "gross_revenue",
            )
        )
    ).scalar_one()
    assert row.status == "needs_review"

    # (c) the failure is audit-logged
    audit = (await db.execute(select(AuditEvent).where(AuditEvent.action == "metric.compute.failed"))).scalars().all()
    assert len(audit) >= 1


async def test_empty_rows_does_not_fabricate_zero(db, tenant_a, monkeypatch):
    """A successful-but-empty result (no rows / empty list) must also fail closed,
    not coerce to 0.0 via `or [[0]]`. Empty means 'no value to report', not 'zero'."""
    await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="net_revenue",
            display_name="Net Revenue",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={"query": "SELECT SUM(amount) FROM transactionline", "dialect": "suiteql"},
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    async def _empty_execute(params, context=None, **kwargs):
        return {"columns": ["c"], "rows": []}

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _empty_execute)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="net_revenue",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )
    assert out.get("error") == "blessed_query_failed", out
    assert out.get("status") == "needs_review"
    assert "rows" not in out
    assert "value" not in out


async def test_division_by_zero_yields_needs_review_no_number(db, tenant_a, monkeypatch):
    """A division-by-zero in an expression metric (e.g. denominator leaf = 0) must
    NOT throw or fabricate; it returns a number-free error dict and marks the
    expression metric needs_review."""
    await _ensure_system_tenant(db)
    for key in ("net_income", "gross_revenue"):
        db.add(
            MetricDefinition(
                tenant_id=SYSTEM_TENANT_ID,
                key=key,
                display_name=key,
                definition="x",
                unit="currency",
                source_kind="suiteql",
                blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
                params_schema={"period": {"type": "period"}},
                status="active",
                version=1,
            )
        )
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="net_margin",
            display_name="Net Margin",
            definition="x",
            unit="percent",
            source_kind="expression",
            expression="net_income / gross_revenue",
            depends_on=["net_income", "gross_revenue"],
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    # denominator resolves to 0 → div-by-zero in the safe evaluator
    async def _fake_scalar(db, tenant_id, metric, coerced, context):
        return {"net_income": 30.0, "gross_revenue": 0.0}[metric.key]

    monkeypatch.setattr("app.services.metrics.metric_compute._execute_scalar_query", _fake_scalar)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="net_margin",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )
    assert out.get("error") == "division_by_zero", out
    assert out.get("status") == "needs_review"
    assert out.get("key") == "net_margin"
    assert "rows" not in out
    assert "value" not in out

    row = (
        await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
                MetricDefinition.key == "net_margin",
            )
        )
    ).scalar_one()
    assert row.status == "needs_review"
