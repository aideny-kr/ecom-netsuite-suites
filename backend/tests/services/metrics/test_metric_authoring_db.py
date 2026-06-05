# backend/tests/services/metrics/test_metric_authoring_db.py
"""DB-backed author-time validation: (a) leaf-existence over tenant ∪ SYSTEM."""

import pytest
from sqlalchemy import delete, select

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.models.tenant import Tenant
from app.services.metrics.metric_authoring import AuthoringError, create_metric, validate_leaves_exist


async def _ensure_system_tenant(db):
    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()
    await db.execute(delete(MetricDefinition))
    await db.flush()


async def test_create_expression_metric_rejects_phantom_leaves(db, tenant_a, monkeypatch):
    """REAL anti-hallucination invariant (major #8). validate_definition proves
    depends_on matches the expression's references, but NOTHING checks the leaves
    actually exist. Authoring net_margin = net_income / gross_revenue when NEITHER
    leaf is seeded persists a blessed-but-un-computable metric: the catalog
    advertises a named metric that can only ever resolve to missing_dependency.

    A real DB with the SYSTEM tenant present and the leaves ABSENT must make
    create_metric (via the DB-aware leaf check) raise AuthoringError → the API 422s.
    The prior create_metric inserted the row with zero leaf awareness, so this
    genuinely fails before the fix."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    payload = {
        "key": "net_margin",
        "display_name": "Net Margin",
        "definition": "x",
        "unit": "percent",
        "source_kind": "expression",
        "expression": "net_income / gross_revenue",
        "depends_on": ["net_income", "gross_revenue"],  # neither seeded
    }
    with pytest.raises(AuthoringError):
        await create_metric(db, tenant_id=tenant_a.id, payload=payload)

    # And nothing was persisted (fail-loud, not fail-then-row).
    rows = (await db.execute(select(MetricDefinition).where(MetricDefinition.key == "net_margin"))).scalars().all()
    assert rows == []


async def test_create_expression_metric_accepts_existing_leaves(db, tenant_a, monkeypatch):
    """The same authoring SUCCEEDS once the two leaves are seeded (one as a SYSTEM
    default, one as a tenant row) — proving the check resolves leaves over tenant ∪
    SYSTEM, not just one scope, and does not over-reject valid metrics."""
    await _ensure_system_tenant(db)

    async def _fake_embed(_text):
        return None

    monkeypatch.setattr("app.services.metrics.metric_authoring.embed_domain_query", _fake_embed)

    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="net_income",
            display_name="Net Income",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
            params_schema={},
            status="active",
            version=1,
        )
    )
    db.add(
        MetricDefinition(
            tenant_id=tenant_a.id,
            key="gross_revenue",
            display_name="Gross Revenue",
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

    metric = await create_metric(
        db,
        tenant_id=tenant_a.id,
        payload={
            "key": "net_margin",
            "display_name": "Net Margin",
            "definition": "x",
            "unit": "percent",
            "source_kind": "expression",
            "expression": "net_income / gross_revenue",
            "depends_on": ["net_income", "gross_revenue"],
        },
    )
    await db.flush()
    assert metric.key == "net_margin"
    assert metric.tenant_id == tenant_a.id


async def test_validate_leaves_exist_is_noop_for_query_backed(db, tenant_a):
    """A query-backed metric has no leaves; the DB-aware check must be a no-op
    (never spuriously reject a single-source metric)."""
    await _ensure_system_tenant(db)
    await validate_leaves_exist(
        db,
        tenant_id=tenant_a.id,
        d={
            "key": "gross_revenue",
            "source_kind": "suiteql",
            "blessed_spec": {"query": "SELECT 1", "dialect": "suiteql"},
            "depends_on": None,
        },
    )
