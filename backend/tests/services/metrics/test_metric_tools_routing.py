# backend/tests/services/metrics/test_metric_tools_routing.py
"""D2: per-match routing_directive in metric_resolve output.

The metric_resolve tool MUST emit a 'routing_directive' string in each matched-metric
dict that instructs the model to call metric_compute instead of authoring ad-hoc SQL.

Isolation note: migration 080 already seeds SYSTEM-tenant rows with well-known keys
(net_income, gross_revenue, net_margin, ...). This test uses a unique per-test key so
the INSERT does not collide with the committed catalog, but then queries with a
display_name ilike match to ensure we get at least one result back from the resolver
(which also unions in ALL active SYSTEM rows). We verify routing_directive on whatever
matches come back.
"""

import uuid

import pytest

from app.mcp.tools import metric_tools
from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition

pytestmark = pytest.mark.asyncio


async def test_resolve_emits_routing_directive_per_match(db):
    # Use a unique key so we don't collide with migration-080's committed net_margin row.
    unique_key = f"t_{uuid.uuid4().hex[:12]}_margin"
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key=unique_key,
            display_name="Net Margin Test",
            definition="net income / revenue",
            unit="percent",
            source_kind="expression",
            expression="net_income / gross_revenue",
            depends_on=["net_income", "gross_revenue"],
            status="active",
            version=1,
            provenance={"author": "t"},
        )
    )
    await db.flush()
    # Query by display_name substring — resolver uses ilike match. Migration-080 SYSTEM rows
    # are always unioned in too, so we are guaranteed at least one match.
    out = await metric_tools.resolve({"query": "Net Margin Test"}, {"db": db, "tenant_id": str(uuid.uuid4())})
    assert out["metrics"], "expected a match"
    assert all("metric_compute" in m["routing_directive"] for m in out["metrics"])
    assert "must" in out["metrics"][0]["routing_directive"].lower()
