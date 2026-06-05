# backend/tests/services/metrics/test_metric_compute_integration.py
from datetime import date

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


# --- R4 source-kind routing: a bigquery metric must NOT execute against NetSuite ---


async def test_bigquery_metric_routes_to_bigquery_executor_not_netsuite(db, tenant_a, monkeypatch):
    """THE anti-hallucination invariant for major #6. A metric whose source_kind is
    'bigquery' must execute through the BigQuery executor — NOT netsuite_suiteql.

    The prior _execute_scalar_query hardcoded netsuite_suiteql for every metric, so a
    bigquery metric silently ran its blessed query against the WRONG data source
    (NetSuite SuiteTalk). That surfaces a number computed from the wrong system under
    the catalog's authority — exactly the hallucination this catalog exists to prevent.

    We stub BOTH boundaries with disjoint sentinels: BigQuery returns the real value;
    netsuite_suiteql, if ever called, returns a poison value. The test fails if the
    poison number leaks (= the metric was routed to NetSuite) OR if netsuite_suiteql
    was touched at all."""
    await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="warehouse_gmv",
            display_name="Warehouse GMV",
            definition="x",
            unit="currency",
            source_kind="bigquery",
            blessed_spec={"query": "SELECT SUM(gmv) FROM analytics.orders", "dialect": "bigquery"},
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    bq_calls: list[str] = []
    ns_calls: list[str] = []

    async def _bq_execute(params, context=None, **kwargs):
        bq_calls.append(params.get("query", ""))
        return {"columns": ["gmv"], "rows": [[4242.0]]}

    async def _ns_poison(params, context=None, **kwargs):
        ns_calls.append(params.get("query", ""))
        return {"columns": ["x"], "rows": [[-9999.0]]}  # poison: wrong-source value

    monkeypatch.setattr("app.mcp.tools.bigquery_tools.bigquery_sql_execute", _bq_execute)
    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_poison)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="warehouse_gmv",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )

    # (a) the number came from BigQuery, not the NetSuite poison source
    assert "error" not in out, out
    assert out["row_count"] == 1
    assert out["rows"][0][0] == "Warehouse GMV"
    assert out["rows"][0][1] == 4242.0
    # (b) the NetSuite tool was NEVER touched for a bigquery metric
    assert ns_calls == [], f"bigquery metric leaked into netsuite_suiteql: {ns_calls}"
    # (c) the bigquery executor actually ran the filled blessed query
    assert bq_calls and "analytics.orders" in bq_calls[0]


async def test_filled_suiteql_query_to_disallowed_table_blocked_before_execute(db, tenant_a, monkeypatch):
    """REAL anti-hallucination invariant (major #8, leg c). The metric layer must
    re-run the FULL netsuite_suiteql.validate_query (read-only AND table-allowlist)
    on the FILLED blessed query BEFORE execution — not just is_read_only_sql.

    A blessed metric whose query selects from a table NOT in
    NETSUITE_SUITEQL_ALLOWED_TABLES (here `bank_account`) is read-only-clean but
    table-illegal. The prior _execute_scalar_query only checked is_read_only_sql, so
    such a query sailed past the metric layer's own guard and reached
    netsuite_suiteql.execute — the metric catalog exfiltrating an off-allowlist table
    under its own authority. We poison netsuite_suiteql.execute: if it is EVER called,
    the table-allowlist re-validation did not happen at the metric layer."""
    await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="rogue_balance",
            display_name="Rogue Balance",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            # read-only SELECT, but `bank_account` is NOT in the allowed-tables list
            blessed_spec={"query": "SELECT SUM(balance) FROM bank_account", "dialect": "suiteql"},
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    ns_calls: list[str] = []

    async def _ns_poison(params, context=None, **kwargs):
        ns_calls.append(params.get("query", ""))
        return {"columns": ["x"], "rows": [[-9999.0]]}  # must NEVER be reached

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_poison)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="rogue_balance",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )

    # (a) blocked at the metric layer → number-free needs_review, no fabricated value
    assert out.get("error") == "blessed_query_failed", out
    assert out.get("status") == "needs_review"
    assert "rows" not in out
    assert "value" not in out
    assert -9999.0 not in out.values()
    # (b) the NetSuite execute boundary was NEVER reached — the allowlist re-validation
    #     happened in the metric layer, before execute.
    assert ns_calls == [], f"disallowed-table query reached netsuite_suiteql.execute: {ns_calls}"


async def test_filled_suiteql_query_to_allowed_table_still_executes(db, tenant_a, monkeypatch):
    """Guard against the allowlist re-validation being too strict: a filled query over
    an ALLOWED table (transactionline) must still execute and return its number."""
    await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="gross_revenue",
            display_name="Gross Revenue",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={
                "query": "SELECT SUM(amount) FROM transactionline WHERE trandate>=:period_start",
                "dialect": "suiteql",
            },
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    async def _ns_ok(params, context=None, **kwargs):
        return {"columns": ["s"], "rows": [[777.0]]}

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_ok)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )
    assert "error" not in out, out
    assert out["rows"][0][1] == 777.0


async def test_bigquery_metric_failure_fails_closed_no_fabricated_number(db, tenant_a, monkeypatch):
    """A bigquery metric whose executor errors must fail closed (number-free
    needs_review), same as the suiteql path — never coerce to a fabricated value."""
    await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="warehouse_gmv",
            display_name="Warehouse GMV",
            definition="x",
            unit="currency",
            source_kind="bigquery",
            blessed_spec={"query": "SELECT SUM(gmv) FROM analytics.orders", "dialect": "bigquery"},
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    async def _bq_boom(params, context=None, **kwargs):
        return {"error": True, "message": "dataset not found"}

    monkeypatch.setattr("app.mcp.tools.bigquery_tools.bigquery_sql_execute", _bq_boom)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="warehouse_gmv",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )
    assert out.get("error") == "blessed_query_failed", out
    assert out.get("status") == "needs_review"
    assert "rows" not in out
    assert "value" not in out
    assert 0 not in out.values()
    assert 0.0 not in out.values()


# --- F2 fiscal-window flow through the REAL tool seam ---
#
# Spec acceptance criterion #6: "Period bounds come from the deterministic resolver,
# not the LLM." A non-January fiscal tenant must get FISCAL (not calendar) windows via
# the REAL tool seam the agent invokes (metric_tools.compute -> compute_metric ->
# coerce_params -> resolve_period). The fiscal-aware assertions in test_period_resolver
# bypass the seam by calling resolve_period directly; the seam-level tests all pass the
# January default (calendar==fiscal), so a regression that drops/ignores the context
# fiscal month anywhere in the seam (compute_metric hardcoding fy=1, metric_tools.compute
# failing to forward context, or coerce_params ignoring it) passes 100% of those tests.
# These tests freeze `today` and drive the WHOLE seam, capturing the `coerced`
# period_start/period_end the leaf executor actually receives.


def _freeze_today(monkeypatch, frozen: date):
    """Pin coerce_params' `date.today()` (used by resolve_period) to a fixed date so the
    resolved window is deterministic, while `date(y, m, d)` construction still works."""

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return frozen

    monkeypatch.setattr("app.services.metrics.metric_compute.date", _FrozenDate)


async def test_seam_forwards_fiscal_month_to_resolver_for_fiscal_window(db, tenant_a, monkeypatch):
    """REAL-seam F2 invariant. Driving metric_tools.compute (the exact path the agent
    invokes) with context={'fiscal_year_start_month': 4} and a fiscal-sensitive token
    ('this_year') must resolve to the FISCAL window (Apr 1 2026 -> Mar 31 2027 on a
    frozen today=2026-05-15), NOT the calendar window (Jan 1 -> Dec 31 2026).

    The leaf executor captures the `coerced` params it actually receives, so the
    resolved bounds are proven to flow through metric_tools.compute -> compute_metric ->
    coerce_params -> resolve_period. A regression that hardcodes fy=1, fails to forward
    context, or ignores the fiscal month anywhere in the seam lands the calendar window
    and fails here."""
    from app.mcp.tools import metric_tools

    await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="gross_revenue",
            display_name="Gross Revenue",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    _freeze_today(monkeypatch, date(2026, 5, 15))

    captured: dict = {}

    async def _capture_scalar(db, tenant_id, metric, coerced, context):
        captured.update(coerced)
        return 123.0

    monkeypatch.setattr("app.services.metrics.metric_compute._execute_scalar_query", _capture_scalar)

    out = await metric_tools.compute(
        {"key": "gross_revenue", "params": {"period": "this_year"}},
        {"db": db, "tenant_id": str(tenant_a.id), "fiscal_year_start_month": 4},
    )

    assert "error" not in out, out
    # FISCAL window for fy_start=4 on 2026-05-15 (mirrors resolve_period's this_year branch).
    assert captured.get("period_start") == "2026-04-01", captured
    assert captured.get("period_end") == "2027-03-31", captured
    # ...and explicitly NOT the calendar (fy=1) window — proves the divergence is real,
    # not a tautology that would also pass under a fy=1 regression.
    assert captured.get("period_start") != "2026-01-01", captured
    assert captured.get("period_end") != "2026-12-31", captured


async def test_seam_january_default_yields_calendar_window(db, tenant_a, monkeypatch):
    """Control for the test above: the SAME frozen today + token through the SAME seam,
    but with the January default (fy=1), must yield the CALENDAR window (Jan 1 -> Dec 31
    2026). This proves the fiscal/calendar split is driven by the forwarded context value
    and that the assertions above aren't passing by accident."""
    from app.mcp.tools import metric_tools

    await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="gross_revenue",
            display_name="Gross Revenue",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    _freeze_today(monkeypatch, date(2026, 5, 15))

    captured: dict = {}

    async def _capture_scalar(db, tenant_id, metric, coerced, context):
        captured.update(coerced)
        return 123.0

    monkeypatch.setattr("app.services.metrics.metric_compute._execute_scalar_query", _capture_scalar)

    out = await metric_tools.compute(
        {"key": "gross_revenue", "params": {"period": "this_year"}},
        {"db": db, "tenant_id": str(tenant_a.id), "fiscal_year_start_month": 1},
    )

    assert "error" not in out, out
    assert captured.get("period_start") == "2026-01-01", captured
    assert captured.get("period_end") == "2026-12-31", captured


# --- F2 fiscal-window flow through the *PRODUCTION* governed_execute seam ---
#
# The two seam tests above hand-BUILD the context dict with fiscal_year_start_month
# already in it — so they prove metric_tools.compute -> compute_metric -> resolve_period
# honors a fiscal month *that is already in the context*. But in production the agent
# never builds that context: it calls mcp_server.call_tool -> governance.governed_execute,
# and THAT seam builds the context dict (governance.py ~466-474) WITHOUT
# fiscal_year_start_month. So the value never reaches compute and the period resolver
# always runs calendar-year for every tenant. This test drives the REAL governed_execute
# seam (exactly what the agent invokes) for a tenant whose tenant_configs row carries
# fiscal_year_start_month=4, and asserts the leaf executor receives the FISCAL window
# (Apr 1 2026 -> Mar 31 2027 on a frozen today=2026-05-15), not the calendar window.
# Pre-fix this FAILS — the resolver runs January-default and lands Jan 1 -> Dec 31 2026.


async def test_governed_execute_seam_threads_tenant_fiscal_month(db, tenant_a, monkeypatch):
    """REAL PRODUCTION-seam F2 invariant. Drive mcp.governance.governed_execute (the
    exact path the agent's tool call flows through) for metric.compute, with a tenant
    whose tenant_configs.fiscal_year_start_month=4. governed_execute builds the tool
    context WITHOUT a fiscal month, so the fix must source it from tenant_configs before
    the period resolver runs. With a frozen today=2026-05-15 and token 'this_year', the
    leaf executor must receive the FISCAL window (Apr 1 2026 -> Mar 31 2027), NOT the
    calendar window (Jan 1 -> Dec 31 2026)."""
    from app.mcp.governance import _rate_limits, governed_execute
    from app.mcp.registry import TOOL_REGISTRY
    from app.models.tenant import TenantConfig

    await _ensure_system_tenant(db)

    # The tenant runs an April fiscal year. This is the production source of truth the
    # governed_execute seam must read from — NOT a hand-passed context value.
    cfg = (await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_a.id))).scalar_one()
    cfg.fiscal_year_start_month = 4
    await db.flush()

    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="gross_revenue",
            display_name="Gross Revenue",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={"query": "SELECT 1", "dialect": "suiteql"},
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    _freeze_today(monkeypatch, date(2026, 5, 15))

    captured: dict = {}

    async def _capture_scalar(db, tenant_id, metric, coerced, context):
        captured.update(coerced)
        return 123.0

    monkeypatch.setattr("app.services.metrics.metric_compute._execute_scalar_query", _capture_scalar)

    # Avoid cross-test rate-limit bleed for this tool/tenant.
    _rate_limits.pop(str(tenant_a.id), None)

    out = await governed_execute(
        tool_name="metric.compute",
        params={"key": "gross_revenue", "params": {"period": "this_year"}},
        tenant_id=str(tenant_a.id),
        actor_id=None,
        execute_fn=TOOL_REGISTRY["metric.compute"]["execute"],
        db=db,
    )

    assert "error" not in out, out
    # FISCAL window for fy_start=4 on 2026-05-15 — proves the tenant's fiscal month
    # reached the resolver THROUGH the production governed_execute seam.
    assert captured.get("period_start") == "2026-04-01", captured
    assert captured.get("period_end") == "2027-03-31", captured
    # ...and explicitly NOT the calendar (fy=1) window — proves the divergence is real,
    # so this would fail under the pre-fix calendar-year regression.
    assert captured.get("period_start") != "2026-01-01", captured
    assert captured.get("period_end") != "2026-12-31", captured
