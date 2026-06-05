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
    dict, never a value/rows. D1: compute is READ-ONLY — status is NOT flipped;
    the failure is audit-logged instead."""
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
    assert out.get("key") == "gross_revenue"
    assert "rows" not in out
    assert "value" not in out
    assert "status" not in out  # D1: compute never returns status in error dict
    # the fabricated zero must appear NOWHERE in the payload
    assert 0 not in out.values()
    assert 0.0 not in out.values()

    # (b) D1: the metric row status is NOT mutated — compute is read-only
    row = (
        await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
                MetricDefinition.key == "gross_revenue",
            )
        )
    ).scalar_one()
    assert row.status == "active"

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
    assert "status" not in out  # D1: compute never returns status in error dict
    assert "rows" not in out
    assert "value" not in out


async def test_division_by_zero_yields_no_number(db, tenant_a, monkeypatch):
    """A division-by-zero in an expression metric (e.g. denominator leaf = 0) must
    NOT throw or fabricate; it returns a number-free error dict. D1: compute is
    READ-ONLY — status is NOT flipped, the failure is audit-logged instead."""
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
    assert out.get("key") == "net_margin"
    assert "rows" not in out
    assert "value" not in out
    assert "status" not in out  # D1: compute never returns status in error dict

    # D1: the metric row status is NOT mutated — compute is read-only
    row = (
        await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
                MetricDefinition.key == "net_margin",
            )
        )
    ).scalar_one()
    assert row.status == "active"

    # the failure is audit-logged
    audit = (await db.execute(select(AuditEvent).where(AuditEvent.action == "metric.compute.failed"))).scalars().all()
    assert len(audit) >= 1


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

    # (a) blocked at the metric layer → number-free error, no fabricated value
    # D1: compute is read-only — no status flip, audit-logged instead
    assert out.get("error") == "blessed_query_failed", out
    assert "status" not in out  # D1: compute never returns status in error dict
    assert "rows" not in out
    assert "value" not in out
    assert -9999.0 not in out.values()
    # (b) the NetSuite execute boundary was NEVER reached — the allowlist re-validation
    #     happened in the metric layer, before execute.
    assert ns_calls == [], f"disallowed-table query reached netsuite_suiteql.execute: {ns_calls}"


async def test_filled_bigquery_query_failing_validation_fails_closed_symmetric_to_suiteql(db, tenant_a, monkeypatch):
    """REAL error-symmetry invariant (F4 (d)). The suiteql branch of
    _validate_and_execute_by_source raises ComputeError when the FILLED query fails its
    read-only/allowlist re-validation, so compute_metric returns a NUMBER-FREE error dict.
    The bigquery branch must behave IDENTICALLY: a filled bigquery query that fails
    _validate_read_only must surface as a number-free error, not a ParamError that
    escapes compute_metric's catch (ExpressionError/ComputeError only).

    D1: compute is READ-ONLY — no status flip. The prior bigquery branch raised ParamError
    on filled-query validation failure; compute_metric does NOT catch ParamError, so the
    request 500s instead of failing closed + auditing. We force the bigquery read-only
    validator to reject the filled query; the metric status must REMAIN active and the
    failure must be audit-logged. The bigquery executor must NEVER be reached (validation
    precedes execute). Pre-fix: ParamError propagates uncaught (no audit)."""
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

    # Force the FILLED-query read-only validation to fail (schema-drift / non-read-only).
    def _reject(_query):
        raise ValueError("not read-only")

    monkeypatch.setattr("app.services.bigquery_service._validate_read_only", _reject)

    bq_calls: list[str] = []

    async def _bq_poison(params, context=None, **kwargs):
        bq_calls.append(params.get("query", ""))
        return {"columns": ["gmv"], "rows": [[4242.0]]}  # must NEVER be reached

    monkeypatch.setattr("app.mcp.tools.bigquery_tools.bigquery_sql_execute", _bq_poison)

    # MUST NOT raise (no 500): compute_metric must catch the failure and fail closed.
    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="warehouse_gmv",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )

    # (a) number-free error — symmetric with the suiteql allowlist-fail path
    # D1: compute is read-only — no status flip, audit-logged instead
    assert out.get("error") == "blessed_query_failed", out
    assert out.get("key") == "warehouse_gmv"
    assert "rows" not in out
    assert "value" not in out
    assert "status" not in out  # D1: compute never returns status in error dict

    # (b) the bigquery executor was NEVER reached (validation precedes execute)
    assert bq_calls == [], f"failed-validation bigquery query reached the executor: {bq_calls}"

    # (c) D1: the metric row status is NOT mutated — compute is read-only
    row = (
        await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
                MetricDefinition.key == "warehouse_gmv",
            )
        )
    ).scalar_one()
    assert row.status == "active"
    audit = (await db.execute(select(AuditEvent).where(AuditEvent.action == "metric.compute.failed"))).scalars().all()
    assert len(audit) >= 1


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
    """A bigquery metric whose executor errors must fail closed (number-free error dict),
    same as the suiteql path — never coerce to a fabricated value. D1: compute is
    READ-ONLY — status is NOT flipped, the failure is audit-logged instead."""
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
    assert "status" not in out  # D1: compute never returns status in error dict
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


# --- F3 injection-hardening: the FILLED query reaching the executor is un-alterable ---
#
# Spec acceptance #9: "A crafted param value cannot alter SQL structure." Author-time
# rejects injecty enum values (test_metric_authoring), but compute is the LAST line of
# defense: we seed a metric whose enum carries the classic `x' OR '1'='1` payload
# DIRECTLY (bypassing the author-time guard, which is exactly the defense-in-depth case)
# and capture the EXACT query string compute_metric hands to netsuite_suiteql.execute.
# Pre-fix, fill_query did `f"'{v}'"` with no escaping, so the captured query was
# `...region='x' OR '1'='1'` — a structural break-out (param data became boolean SQL).
# Post-fix the embedded quotes are doubled, so the value is one inert literal and the
# query's structure is identical to a benign value.


async def test_filled_query_handed_to_executor_cannot_break_out_of_literal(db, tenant_a, monkeypatch):
    await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="rev_by_region",
            display_name="Rev By Region",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            # NOTE: this blessed enum value would be REJECTED at author-time (F3 leg b).
            # We seed it directly to prove the compute-path quote-escape (F3 leg a) is an
            # independent second line of defense even if a poison value ever lands in a row.
            blessed_spec={
                "query": "SELECT SUM(amount) FROM transactionline WHERE region=:region",
                "dialect": "suiteql",
            },
            params_schema={"region": {"type": "enum", "values": ["us", "x' OR '1'='1"]}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    captured: list[str] = []

    async def _capture_execute(params, context=None, **kwargs):
        captured.append(params.get("query", ""))
        return {"columns": ["s"], "rows": [[5.0]]}

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _capture_execute)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="rev_by_region",
        params={"region": "x' OR '1'='1"},
        context={"fiscal_year_start_month": 1},
    )

    assert "error" not in out, out
    assert captured, "executor was never reached"
    q = captured[0]
    # (a) the injected value lands as a SINGLE inert literal: embedded quotes doubled.
    assert q == "SELECT SUM(amount) FROM transactionline WHERE region='x'' OR ''1''=''1'", q
    # (b) structurally un-alterable: no un-doubled quote closes the literal early. The
    #     literal's own delimiters are the ONLY single quotes once the '' pairs are removed.
    assert q.replace("''", "").count("'") == 2, q
    # (c) the param data did NOT become SQL control: " OR " never appears OUTSIDE a literal.
    #     (After collapsing the literal to a placeholder, no boolean OR remains.)
    import re as _re

    collapsed = _re.sub(r"'(?:[^']|'')*'", "?", q)
    assert " OR " not in collapsed.upper(), collapsed


async def test_filled_query_with_backslash_value_does_not_inject_or_500(db, tenant_a, monkeypatch):
    """REAL injection + fail-closed invariant (F3, leg a — backslash gap), at the FULL
    compute seam. A blessed enum value carrying a backslash group-ref (`us\\g<0>`) is
    seeded directly (defense-in-depth case: a poison value that bypassed author-time).
    fill_query substitutes :name via re.sub; the SECOND arg is a replacement TEMPLATE
    that interprets `\\g<0>` (re-injects the matched `:region` text) / `\\1` (raises an
    uncaught re.error). compute_metric catches only ExpressionError/ComputeError, so the
    bare re.error would 500 instead of failing closed — and `\\g<0>` would smuggle
    placeholder text into the literal under the catalog's authority.

    Post-fix (callable re.sub replacement) the value lands VERBATIM as one inert literal:
    the executor IS reached, the captured query is exactly `region='us\\g<0>'`, no
    `:region` text was re-injected, and there is NO 500 (no error key)."""
    await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="rev_by_region",
            display_name="Rev By Region",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={
                "query": "SELECT SUM(amount) FROM transactionline WHERE region=:region",
                "dialect": "suiteql",
            },
            # poison value seeded directly (would be rejected at author-time post-fix);
            # this proves the compute-path substitution is backslash-inert on its own.
            params_schema={"region": {"type": "enum", "values": ["us", "us\\g<0>"]}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    captured: list[str] = []

    async def _capture_execute(params, context=None, **kwargs):
        captured.append(params.get("query", ""))
        return {"columns": ["s"], "rows": [[5.0]]}

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _capture_execute)

    # No 500: the backslash group-ref must NOT raise an uncaught re.error out of compute.
    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="rev_by_region",
        params={"region": "us\\g<0>"},
        context={"fiscal_year_start_month": 1},
    )

    assert "error" not in out, out
    assert captured, "executor was never reached"
    q = captured[0]
    # (a) the value landed VERBATIM as one inert literal — `\g<0>` was NOT interpreted.
    assert q == "SELECT SUM(amount) FROM transactionline WHERE region='us\\g<0>'", q
    # (b) the placeholder text `:region` was NOT re-injected back into the SQL.
    assert ":region" not in q, q


# --- G1 param-refusal: a bad param must return the §9 number-free structured refusal,
#     NOT bare-raise out of compute_metric ---
#
# Spec §9 ("Param value fails type/safety check" → "Refuse; no execution") and the §3/§6
# contract that EVERY refusal path returns a number-free {'error': ...} dict. But
# coerce_params runs OUTSIDE compute_metric's try/except (which catches only
# ExpressionError/ComputeError), and the per-leaf coerce_params inside the expression
# path is also uncaught. So a ParamError (unknown/missing param, bad date/enum) or a
# PeriodError (a fabricated period token not in period_resolver.SUPPORTED_TOKENS)
# bare-raises out of compute_metric and 500s the request instead of returning the
# structured refusal. These tests drive a fabricated period token and an unknown param
# key through the real compute seam and assert a number-free {'error':'invalid_params'}
# dict with NO raise. Pre-fix they FAIL (the bare raise escapes compute_metric).


async def test_unsupported_period_token_refuses_no_raise(db, tenant_a, monkeypatch):
    """A fabricated period token ('next_decade' — NOT in period_resolver.SUPPORTED_TOKENS)
    makes resolve_period raise PeriodError INSIDE coerce_params, which runs OUTSIDE
    compute_metric's try/except. Pre-fix this PeriodError bare-raises out of
    compute_metric (500). Post-fix compute_metric wraps the top-level coerce in
    try/except (ParamError, PeriodError) and returns the §9 number-free structured
    refusal {'error': 'invalid_params', 'key': ...} — and does NOT raise."""
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

    # Guard: the executor must NEVER be reached — refusal precedes any execution.
    async def _ns_poison(params, context=None, **kwargs):
        raise AssertionError("executor reached despite invalid period token")

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_poison)

    # MUST NOT raise: compute_metric must catch PeriodError and fail closed.
    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        params={"period": "next_decade"},
        context={"fiscal_year_start_month": 1},
    )

    # (a) structured number-free refusal, NOT a data_table with a value
    assert out.get("error") == "invalid_params", out
    assert out.get("key") == "gross_revenue", out
    assert "rows" not in out
    assert "value" not in out
    # (b) no fabricated number anywhere in the payload
    assert 0 not in out.values()
    assert 0.0 not in out.values()


async def test_unknown_param_key_refuses_no_raise(db, tenant_a, monkeypatch):
    """An unknown param key (in neither params_schema nor dimensions) must return the §9
    number-free structured refusal and must NOT raise. Task 13 adds a guard BEFORE
    coerce_params that fires first when a param is unrecognised by both schemas; it
    returns {'error': 'invalid_dimension'} (more precise than the coerce 'invalid_params'
    that fired before Task 13). The safety invariants — no raise, no rows, no value —
    are unchanged."""
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

    async def _ns_poison(params, context=None, **kwargs):
        raise AssertionError("executor reached despite unknown param key")

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_poison)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        params={"period": "this_month", "evil": "1 OR 1=1"},
        context={"fiscal_year_start_month": 1},
    )

    # Task 13: the pre-coerce guard fires first for params unrecognised by BOTH schemas,
    # returning invalid_dimension (more precise than the old coerce-level invalid_params).
    assert out.get("error") in {"invalid_params", "invalid_dimension"}, out
    assert out.get("key") == "gross_revenue", out
    assert "rows" not in out
    assert "value" not in out


async def test_leaf_param_error_in_expression_path_refuses_no_raise(db, tenant_a, monkeypatch):
    """The per-leaf coerce_params (inside the expression path) is ALSO uncaught by
    compute_metric's ExpressionError/ComputeError handler. If a leaf metric's
    params_schema requires a param that the resolved params can't satisfy (here a
    'date'-typed leaf param with no value), the leaf coerce raises ParamError mid-loop
    and bare-raises out of compute_metric. Post-fix the per-leaf coerce is wrapped too,
    so the structured number-free refusal is returned without raising."""
    await _ensure_system_tenant(db)
    # Leaf with a required 'date' param that the request does NOT supply → ParamError
    # ("missing param") at the per-leaf coerce_params (compute_metric ~line 303).
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="net_income",
            display_name="net_income",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            blessed_spec={"query": "SELECT 1 WHERE d=:as_of", "dialect": "suiteql"},
            params_schema={"as_of": {"type": "date"}},
            status="active",
            version=1,
        )
    )
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="gross_revenue",
            display_name="gross_revenue",
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
            # The top-level metric only declares a period param; the leaf demands 'as_of'.
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    async def _ns_poison(params, context=None, **kwargs):
        raise AssertionError("executor reached despite missing leaf param")

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_poison)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="net_margin",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )

    assert out.get("error") == "invalid_params", out
    assert out.get("key") == "net_margin", out
    assert "rows" not in out
    assert "value" not in out


async def test_undeclared_query_placeholder_refuses_no_raise(db, tenant_a, monkeypatch):
    """A blessed query that references a placeholder (:undeclared) NOT present in
    params_schema slips past coerce_params (which only fills declared params), so
    fill_query finds a residual placeholder and raises ParamError('unfilled
    placeholder remains') deep inside _execute_scalar_query — which runs inside
    compute_metric's outer try that historically caught only ExpressionError/
    ComputeError. Pre-fix that ParamError bare-raises out of compute_metric (500).
    Post-fix the outer try ALSO catches ParamError and returns the §9 number-free
    {'error': 'invalid_params'} refusal, never raising and never reaching the executor."""
    await _ensure_system_tenant(db)
    db.add(
        MetricDefinition(
            tenant_id=SYSTEM_TENANT_ID,
            key="gross_revenue",
            display_name="Gross Revenue",
            definition="x",
            unit="currency",
            source_kind="suiteql",
            # :undeclared is NOT in params_schema → coerce_params never fills it →
            # fill_query raises ParamError on the residual placeholder.
            blessed_spec={"query": "SELECT 1 WHERE x=:undeclared", "dialect": "suiteql"},
            params_schema={"period": {"type": "period"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    # Guard: the executor must NEVER be reached — fill_query fails before execution.
    async def _ns_poison(params, context=None, **kwargs):
        raise AssertionError("executor reached despite unfilled placeholder")

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_poison)

    # MUST NOT raise: compute_metric must catch the fill_query ParamError and fail closed.
    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )

    # (a) structured number-free refusal, NOT a data_table with a value
    assert out.get("error") == "invalid_params", out
    assert out.get("key") == "gross_revenue", out
    assert "rows" not in out
    assert "value" not in out
    # (b) no fabricated number anywhere in the payload
    assert 0 not in out.values()
    assert 0.0 not in out.values()


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


# --- Task 12: definition_version must be cited in the compute payload + failure audit ---
#
# §4/§10 promise: "cite the exact definition version it used". The compute data_table
# payload must carry `definition_version` equal to the metric row's `version` column, so
# downstream consumers (the SSE renderer, the audit trail, the LLM condensed string) can
# attribute a number to the exact definition that produced it. The failure audit log must
# also record the version that was active when the failure occurred.


async def test_successful_compute_payload_carries_definition_version(db, tenant_a, monkeypatch):
    """§10 audit-citation invariant. compute_metric must return a data_table dict whose
    `definition_version` key equals the metric row's `version` column. Pre-fix this key
    is absent — the payload carries no version citation."""
    await _ensure_system_tenant(db)
    known_version = 7  # deliberately non-trivial version to prove it flows through
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
            version=known_version,
        )
    )
    await db.flush()

    async def _fake_scalar(db, tenant_id, metric, coerced, context):
        return 42000.0

    monkeypatch.setattr("app.services.metrics.metric_compute._execute_scalar_query", _fake_scalar)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )

    # (a) successful compute — must be a data_table, not an error
    assert "error" not in out, out
    assert out.get("row_count") == 1
    assert out["rows"][0][1] == 42000.0

    # (b) §10 citation: definition_version must equal the seeded metric's version
    assert "definition_version" in out, f"definition_version missing from payload: {out}"
    assert out["definition_version"] == known_version, (
        f"definition_version={out['definition_version']!r} != seeded version={known_version}"
    )


async def test_failure_audit_carries_version(db, tenant_a, monkeypatch):
    """§10 audit-citation invariant for the failure path. When compute_metric fails and
    audit-logs via _log_compute_failure, the audit payload must include the metric's
    `version` so the failure record cites which definition version was active."""
    await _ensure_system_tenant(db)
    known_version = 3
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
            version=known_version,
        )
    )
    await db.flush()

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

    # (a) compute failed — must be a number-free error dict
    assert out.get("error") == "blessed_query_failed", out
    assert "rows" not in out

    # (b) §10 audit citation: the failure audit log payload must carry the version
    audit_rows = (
        (await db.execute(select(AuditEvent).where(AuditEvent.action == "metric.compute.failed"))).scalars().all()
    )
    assert audit_rows, "no audit row found for the failure"
    audit_payload = audit_rows[-1].payload or {}
    assert "version" in audit_payload, f"version missing from audit payload: {audit_payload}"
    assert audit_payload["version"] == known_version, (
        f"audit version={audit_payload['version']!r} != seeded version={known_version}"
    )
