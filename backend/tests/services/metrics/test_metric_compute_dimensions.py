# backend/tests/services/metrics/test_metric_compute_dimensions.py
"""Task 13 — compute validates caller params against params_schema + declared dimensions.

A caller param that is in NEITHER params_schema NOR the metric's declared dimensions
must be rejected with {"error": "invalid_dimension", "key": ..., "message": ...} BEFORE
any coerce_params or execution runs.

Background (R3#24): `dimensions` is stored and advertised by metric_resolve but was never
referenced by compute_metric — purely decorative. This guard gives a precise, honest error
for unrecognised params instead of the generic "invalid_params" from coerce_params.

Note: a param that IS a declared dimension but NOT in params_schema will still be rejected
downstream by coerce_params ("unknown param: <name>") — there is no free-dimension SQL
binding yet. That is expected and acceptable; this guard fires only for params that are
unrecognised by BOTH schemas.
"""

from sqlalchemy import delete, select

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.models.tenant import Tenant
from app.services.metrics.metric_compute import compute_metric


async def _ensure_system_tenant(db):
    exists = (await db.execute(select(Tenant.id).where(Tenant.id == SYSTEM_TENANT_ID))).scalar_one_or_none()
    if exists is None:
        db.add(Tenant(id=SYSTEM_TENANT_ID, name="System", slug="system", plan="free", is_active=True))
        await db.flush()
    await db.execute(delete(MetricDefinition))
    await db.flush()


# --- T1: param in NEITHER params_schema NOR dimensions (dict shape) ---


async def test_unrecognised_param_returns_invalid_dimension_dict_dimensions(db, tenant_a, monkeypatch):
    """R3#24 guard. A caller param ('channel') that is neither in params_schema nor in a
    dict-shaped dimensions must return {"error": "invalid_dimension"} and must NOT reach
    the executor or coerce_params (which would produce "invalid_params" instead)."""
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
            # dimensions as dict (the ORM's native storage shape)
            dimensions={"subsidiary": {"type": "string"}, "department": {"type": "string"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    # Guard: executor must NEVER be reached for an invalid param.
    async def _ns_poison(params, context=None, **kwargs):
        raise AssertionError("executor reached despite unrecognised param")

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_poison)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        # 'channel' is not in params_schema AND not in dimensions
        params={"channel": "web"},
        context={"fiscal_year_start_month": 1},
    )

    # (a) precise invalid_dimension error — NOT the generic invalid_params
    assert out.get("error") == "invalid_dimension", out
    assert out.get("key") == "gross_revenue", out
    assert "channel" in out.get("message", ""), out
    # (b) number-free: no rows, no value
    assert "rows" not in out
    assert "value" not in out


# --- T2: param in NEITHER params_schema NOR dimensions (list shape) ---


async def test_unrecognised_param_returns_invalid_dimension_list_dimensions(db, tenant_a, monkeypatch):
    """Same guard but with dimensions stored as a list (the code must handle BOTH shapes)."""
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
            # dimensions as list (handled defensively by the guard)
            dimensions=["subsidiary", "department"],
            status="active",
            version=1,
        )
    )
    await db.flush()

    async def _ns_poison(params, context=None, **kwargs):
        raise AssertionError("executor reached despite unrecognised param")

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_poison)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        params={"channel": "web"},
        context={"fiscal_year_start_month": 1},
    )

    assert out.get("error") == "invalid_dimension", out
    assert out.get("key") == "gross_revenue", out
    assert "channel" in out.get("message", ""), out
    assert "rows" not in out
    assert "value" not in out


# --- T3: param IS a declared dimension (dict shape) — passes THIS guard,
#         rejected downstream by coerce_params (no free-dimension binding yet) ---


async def test_dimension_only_param_passes_guard_and_is_rejected_by_coerce(db, tenant_a, monkeypatch):
    """A param that IS a declared dimension but NOT in params_schema passes the new guard
    (it IS recognised as a dimension) and is then rejected by coerce_params with the
    existing 'invalid_params' error. The guard's job is precision for UNRECOGNISED params;
    a dimension-only caller param is a different (expected) failure mode."""
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
            dimensions={"subsidiary": {"type": "string"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    async def _ns_poison(params, context=None, **kwargs):
        raise AssertionError("executor reached")

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_poison)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        # 'subsidiary' IS in dimensions → passes this guard; rejected by coerce_params
        params={"subsidiary": "UK", "period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )

    # The guard does NOT fire (subsidiary is a known dimension).
    assert out.get("error") != "invalid_dimension", out
    # coerce_params rejects the unknown subsidiary param with invalid_params.
    assert out.get("error") == "invalid_params", out


# --- T4: valid period param (in params_schema) must NOT be caught by the new guard ---


async def test_valid_schema_param_passes_guard_and_executes(db, tenant_a, monkeypatch):
    """Regression guard: a param that IS in params_schema (period) must pass the new guard
    and reach execution as before. The guard must not over-block."""
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
            dimensions={"subsidiary": {"type": "string"}},
            status="active",
            version=1,
        )
    )
    await db.flush()

    async def _fake_scalar(db, tenant_id, metric, coerced, context):
        return 999.0

    monkeypatch.setattr("app.services.metrics.metric_compute._execute_scalar_query", _fake_scalar)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        params={"period": "this_month"},
        context={"fiscal_year_start_month": 1},
    )

    assert "error" not in out, out
    assert out.get("row_count") == 1
    assert out["rows"][0][1] == 999.0


# --- T5: unknown metric KEY still returns no_blessed_definition (not invalid_dimension) ---


async def test_unknown_metric_key_returns_no_blessed_definition_not_invalid_dimension(db, tenant_a):
    """The guard must run AFTER the metric-not-found check. An unknown metric key must
    still return no_blessed_definition, not invalid_dimension."""
    await _ensure_system_tenant(db)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="nonexistent_metric_xyz",
        params={"channel": "web"},
        context={"fiscal_year_start_month": 1},
    )

    assert out.get("error") == "no_blessed_definition", out
    assert out.get("error") != "invalid_dimension", out


# --- T6: no dimensions declared → still rejects unrecognised param ---


async def test_no_dimensions_declared_rejects_unrecognised_param(db, tenant_a, monkeypatch):
    """When dimensions is None, an unrecognised param must still be rejected with
    invalid_dimension (the allowed set is empty on the dimensions side)."""
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
            dimensions=None,  # no dimensions declared
            status="active",
            version=1,
        )
    )
    await db.flush()

    async def _ns_poison(params, context=None, **kwargs):
        raise AssertionError("executor reached despite unrecognised param")

    monkeypatch.setattr("app.mcp.tools.netsuite_suiteql.execute", _ns_poison)

    out = await compute_metric(
        db,
        tenant_id=tenant_a.id,
        key="gross_revenue",
        params={"channel": "web"},
        context={"fiscal_year_start_month": 1},
    )

    assert out.get("error") == "invalid_dimension", out
    assert "channel" in out.get("message", ""), out
