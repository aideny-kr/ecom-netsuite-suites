# backend/tests/services/metrics/test_metric_compute_integration.py
from sqlalchemy import select

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.models.tenant import Tenant
from app.services.metrics.metric_compute import compute_metric


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
