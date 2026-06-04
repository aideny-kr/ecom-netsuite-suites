# backend/tests/services/metrics/test_metric_compute_integration.py
from sqlalchemy import select

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
