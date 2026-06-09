import uuid

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition


async def test_metric_definition_roundtrip(db, tenant_a):
    m = MetricDefinition(
        tenant_id=tenant_a.id,
        key="net_margin",
        display_name="Net Margin",
        definition="Net income divided by gross revenue.",
        unit="percent",
        source_kind="expression",
        expression="net_income / gross_revenue",
        depends_on=["net_income", "gross_revenue"],
        params_schema={"period": {"type": "period"}},
        dimensions={"by": ["period", "subsidiary"]},
        synonyms=["net profit margin", "bottom line margin"],
        status="active",
        version=1,
        provenance={"author": "seed"},
    )
    db.add(m)
    await db.flush()
    assert m.id is not None
    assert SYSTEM_TENANT_ID == uuid.UUID("00000000-0000-0000-0000-000000000000")
