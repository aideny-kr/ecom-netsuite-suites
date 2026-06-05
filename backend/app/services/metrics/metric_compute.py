# backend/app/services/metrics/metric_compute.py
"""Deterministic execution of a metric: coerce params, fill blessed query, execute, shape as data_table."""

import re
from datetime import date, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric_definition import SYSTEM_TENANT_ID, MetricDefinition
from app.services.metrics.expression_evaluator import ExpressionError, evaluate_expression
from app.services.metrics.period_resolver import resolve_period


class ParamError(ValueError):
    pass


class ComputeError(RuntimeError):
    """A blessed query failed or returned an unusable result.

    Raised instead of fabricating a value (never `or [[0]]`). compute_metric
    catches this, flips the metric to needs_review, and returns a number-free
    error dict — honoring the anti-hallucination invariant that a failed query
    must NEVER surface a wrong/zero number.
    """

    pass


def coerce_params(
    params_schema: dict,
    params: dict,
    *,
    fiscal_year_start_month: int = 1,
    today: date | None = None,
) -> dict:
    schema = params_schema or {}
    for name in params:
        if name not in schema:
            raise ParamError(f"unknown param: {name}")
    out: dict = {}
    for name, spec in schema.items():
        ptype = spec.get("type")
        if ptype == "period":
            token = params.get(name, "this_month")
            s, e = resolve_period(
                token,
                fiscal_year_start_month=fiscal_year_start_month,
                today=today or date.today(),
            )
            out["period_start"] = s.isoformat()
            out["period_end"] = e.isoformat()
            continue
        if name not in params:
            raise ParamError(f"missing param: {name}")
        val = params[name]
        if ptype == "int":
            try:
                out[name] = int(val)
            except (TypeError, ValueError) as ex:
                raise ParamError(f"{name} must be int") from ex
        elif ptype == "date":
            try:
                out[name] = datetime.strptime(str(val), "%Y-%m-%d").date().isoformat()
            except ValueError as ex:
                raise ParamError(f"{name} must be YYYY-MM-DD") from ex
        elif ptype == "enum":
            if val not in spec.get("values", []):
                raise ParamError(f"{name} not in allowed values")
            out[name] = val
        else:
            raise ParamError(f"unsupported param type: {ptype}")
    return out


def fill_query(query: str, coerced: dict) -> str:
    def _render(v) -> str:
        return str(v) if isinstance(v, int) else f"'{v}'"

    filled = query
    for name, val in coerced.items():
        filled = re.sub(rf":{re.escape(name)}\b", _render(val), filled)
    if re.search(r":[a-zA-Z_]\w*", filled):
        raise ParamError("unfilled placeholder remains")
    return filled


def metric_data_table(display_name: str, value, unit: str, period_label: str, spec) -> dict:
    return {
        "columns": ["Metric", "Value", "Unit", "Period"],
        "rows": [[display_name, value, unit, period_label]],
        "row_count": 1,
        "query": spec,
        "truncated": False,
        # Trust boundary: the whole table is ONE computed number. The orchestrator's
        # data_table interception must render it on the frontend but withhold the
        # value from the LLM-facing condensed string (anti-hallucination invariant).
        "suppress_llm_value": True,
    }


async def _validate_and_execute_by_source(db, tenant_id, source_kind: str, query: str) -> dict:
    """Route the FILLED blessed query to the executor for its source_kind, applying
    THAT tool's own read-only validation before execution. Hardcoding one tool would
    run a bigquery metric's query against NetSuite (wrong data source) — surfacing a
    number from the wrong system under the catalog's authority. Each branch validates
    with the dialect-correct read-only check (SuiteQL vs BigQuery SQL differ).
    """
    if source_kind == "bigquery":
        from app.mcp.tools import bigquery_tools
        from app.services.bigquery_service import _validate_read_only

        try:
            _validate_read_only(query)
        except ValueError as ex:
            raise ParamError("filled query failed read-only validation") from ex
        return await bigquery_tools.bigquery_sql_execute({"query": query}, {"tenant_id": str(tenant_id), "db": db})

    # Default / "suiteql": NetSuite SuiteTalk REST. Expression-leaf metrics are
    # themselves single-source rows (suiteql) and route here.
    from app.mcp.tools import netsuite_suiteql

    if not netsuite_suiteql.is_read_only_sql(query):
        raise ParamError("filled query failed read-only validation")
    return await netsuite_suiteql.execute({"query": query}, {"tenant_id": str(tenant_id), "db": db})


async def _execute_scalar_query(db, tenant_id, metric: MetricDefinition, coerced: dict, context: dict) -> float:
    query = fill_query(metric.blessed_spec["query"], coerced)
    # Branch on source_kind so the number comes from the RIGHT data source.
    result = await _validate_and_execute_by_source(db, tenant_id, metric.source_kind, query)
    # Fail closed: a failed query must NEVER be coerced into a fabricated 0.0.
    if isinstance(result, dict) and result.get("error"):
        raise ComputeError(str(result.get("message") or "blessed query failed"))
    rows = result.get("rows") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ComputeError("blessed query returned no rows")
    first = rows[0]
    if not isinstance(first, (list, tuple)) or len(first) < 1:
        raise ComputeError("blessed query returned no columns")
    cell = first[0]
    if cell is None:
        raise ComputeError("blessed query returned a null value")
    try:
        return float(cell)
    except (TypeError, ValueError) as ex:
        raise ComputeError(f"blessed query value is not numeric: {cell!r}") from ex


async def resolve_metric_by_key(db: AsyncSession, *, tenant_id, key: str) -> MetricDefinition | None:
    """Exact-key lookup with tenant-override-by-key semantics (tenant row wins over SYSTEM).

    Compute requests name a metric by its exact key, so this must NOT route through the
    embedding-similarity resolver: with seeded intent_embeddings a sibling metric whose
    embedding ranks nearer to the key string can evict the requested row out of a narrow
    top_k slice, yielding a false 'no_blessed_definition'/'missing_dependency'. A direct
    keyed query is independent of embeddings and catalog size.
    """
    stmt = select(MetricDefinition).where(
        or_(
            MetricDefinition.tenant_id == tenant_id,
            MetricDefinition.tenant_id == SYSTEM_TENANT_ID,
        ),
        MetricDefinition.status == "active",
        MetricDefinition.key == key,
    )
    rows = list((await db.execute(stmt)).scalars().all())
    # Tenant override wins by key (mirrors resolve_metrics' by_key precedence).
    tenant_row = next((r for r in rows if r.tenant_id == tenant_id), None)
    return tenant_row or next((r for r in rows), None)


async def _mark_needs_review(
    db: AsyncSession, *, tenant_id, metric: MetricDefinition, error_code: str, message: str
) -> dict:
    """Flip the (mis)behaving metric to needs_review, audit-log, and return a
    NUMBER-FREE structured error dict. Never returns a value/rows — a failed
    metric must not surface a number under the catalog's authority."""
    from app.services import audit_service

    metric.status = "needs_review"
    await db.flush()
    await audit_service.log_event(
        db=db,
        tenant_id=tenant_id,
        category="metric",
        action="metric.compute.failed",
        actor_type="system",
        resource_type="metric_definition",
        resource_id=str(metric.id),
        status="failed",
        error_message=message,
        payload={"key": metric.key, "error": error_code},
    )
    return {
        "error": error_code,
        "key": metric.key,
        "message": message,
        "status": "needs_review",
    }


async def compute_metric(db: AsyncSession, *, tenant_id, key: str, params: dict, context: dict) -> dict:
    metric = await resolve_metric_by_key(db, tenant_id=tenant_id, key=key)
    if metric is None:
        return {
            "error": "no_blessed_definition",
            "key": key,
            "message": f"No blessed definition for '{key}'.",
        }
    fy = int(context.get("fiscal_year_start_month", 1) or 1)
    coerced = coerce_params(metric.params_schema or {}, params, fiscal_year_start_month=fy)
    period_label = params.get("period", "")

    try:
        if metric.source_kind == "expression":
            leaves = {}
            for dep in metric.depends_on or []:
                dmatch = await resolve_metric_by_key(db, tenant_id=tenant_id, key=dep)
                if dmatch is None:
                    return {
                        "error": "missing_dependency",
                        "key": dep,
                        "message": f"Missing leaf metric '{dep}'.",
                    }
                leaves[dep] = await _execute_scalar_query(
                    db,
                    tenant_id,
                    dmatch,
                    coerce_params(dmatch.params_schema or {}, params, fiscal_year_start_month=fy),
                    context,
                )
            value = evaluate_expression(metric.expression, leaves)
        else:
            value = await _execute_scalar_query(db, tenant_id, metric, coerced, context)
    except ExpressionError as ex:
        # Runtime evaluator failure → no number. Div-by-zero is the canonical case
        # (missing-dep/cycle are author-time rejections); label it precisely.
        code = "division_by_zero" if "division by zero" in str(ex).lower() else "expression_failed"
        return await _mark_needs_review(db, tenant_id=tenant_id, metric=metric, error_code=code, message=str(ex))
    except ComputeError as ex:
        # Blessed query failed / returned an unusable result → no number.
        return await _mark_needs_review(
            db, tenant_id=tenant_id, metric=metric, error_code="blessed_query_failed", message=str(ex)
        )

    return metric_data_table(
        metric.display_name, value, metric.unit, period_label, metric.blessed_spec or metric.expression
    )
